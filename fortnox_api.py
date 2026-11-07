"""Fortnox API: sign-in for the company, a sync of documents and bookkeeping into
DATA_DIR/fortnox-api, and the calls agents make to read and write.

One connection for the company, shared by every agent that uses this service: by default it is
authorized as a Fortnox service account (account_type=service), so it is tied to no person's user
or licence and keeps working when someone leaves. Only a system administrator of the company can
authorize it. Tokens come from the OAuth authorization-code flow (PKCE). The company creates its own
integration in its own Fortnox; this service is one company's.

Agents read and write (request). Every call that is not a plain GET is appended to the write log
(STATE_DIR/writes.jsonl) with the agent key's name, so a person can see what was done. Fortnox
scopes always grant read AND write; FORTNOX_READ_ONLY=true refuses everything but GET.

Environment:
  FORTNOX_CLIENT_ID, FORTNOX_CLIENT_SECRET    from the integration in Fortnox' developer portal
  FORTNOX_REDIRECT_URI    must match the integration exactly, e.g. https://fortnox.example.se/oauth/callback
  FORTNOX_SCOPES          default: see SCOPES below; must be a subset of what the integration allows
  FORTNOX_ACCOUNT_TYPE    default "service" (the integration must allow service accounts); empty = the user who signs in
  FORTNOX_SYNC_FROM       first date to sync, default January 1st of last year
  FORTNOX_READ_ONLY       "true": only GET reaches Fortnox, write tools are not offered
  FORTNOX_API_BASE / FORTNOX_AUTH_BASE    default https://api.fortnox.se / https://apps.fortnox.se
  STATE_DIR               default DATA_DIR/.state  (token.json, sync.json, writes.jsonl; mode 600)

Layout written: DATA_DIR/fortnox-api/<year>/<month>/<Leverantörsfakturor|Kundfakturor|Verifikationer>/
<nr>_<fileid>_<name>.pdf, each with <file>.json holding date, counterpart, amount, currency and the
raw Fortnox record -- the indexer prefers those fields over what it reads out of the PDF.

The bookkeeping itself goes to DATA_DIR/fortnox-api/bokforing/: verifikationer.json (every voucher
with its rows, keyed "<year>:<series><number>"), konton.json (account names per financial year) and
rakenskapsar.json. A financial year is labelled by its first year ("2026", or "2025/26" when it does
not follow the calendar year). Vouchers changed since the previous sync are fetched again.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

API = os.environ.get("FORTNOX_API_BASE", "https://api.fortnox.se").rstrip("/")
AUTH = os.environ.get("FORTNOX_AUTH_BASE", "https://apps.fortnox.se").rstrip("/")
CLIENT_ID = os.environ.get("FORTNOX_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("FORTNOX_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("FORTNOX_REDIRECT_URI", "")
SCOPES = os.environ.get("FORTNOX_SCOPES") or (
    "companyinformation bookkeeping invoice supplierinvoice customer supplier article payment "
    "archive connectfile inbox settings")
ACCOUNT_TYPE = os.environ.get("FORTNOX_ACCOUNT_TYPE", "service")
SYNC_FROM = os.environ.get("FORTNOX_SYNC_FROM") or f"{date.today().year - 1}-01-01"
READ_ONLY = os.environ.get("FORTNOX_READ_ONLY", "").strip().lower() in ("1", "true", "yes")
STATE = os.environ.get("STATE_DIR") or os.path.join(os.environ.get("DATA_DIR", "/data"), ".state")
TOKEN = os.path.join(STATE, "token.json")
SYNCED = os.path.join(STATE, "sync.json")
WRITES = os.path.join(STATE, "writes.jsonl")
MIN_GAP = 0.22          # 25 requests per 5 s per token, with margin
_lock = threading.Lock()
_call_lock = threading.Lock()
_pending = {}           # state -> (code_verifier, created)
_last_call = [0.0]


class FortnoxError(ValueError):
    """Fortnox refused a call; the message is Fortnox' own, for the agent to act on."""


def configured():
    return bool(CLIENT_ID and CLIENT_SECRET and REDIRECT_URI)


def _save(path, obj):
    os.makedirs(STATE, exist_ok=True)
    tmp = path + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# --- sign-in --------------------------------------------------------------------------------

def authorize_url():
    """Where to send the person who signs in. PKCE + state, kept in memory for 10 minutes."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    now = time.time()
    for k in [k for k, (_, t) in _pending.items() if now - t > 600]:
        _pending.pop(k, None)
    _pending[state] = (verifier, now)
    q = {"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "scope": SCOPES, "state": state,
         "access_type": "offline", "response_type": "code",
         "code_challenge": challenge, "code_challenge_method": "S256"}
    if ACCOUNT_TYPE:
        q["account_type"] = ACCOUNT_TYPE
    return f"{AUTH}/oauth-v1/auth?{urllib.parse.urlencode(q, quote_via=urllib.parse.quote)}"


def _token_request(form):
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(f"{AUTH}/oauth-v1/token", data=urllib.parse.urlencode(form).encode(),
                                 headers={"Authorization": f"Basic {basic}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        t = json.load(r)
    t["expires_at"] = time.time() + int(t.get("expires_in", 3600)) - 60
    t["refreshed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return t


def finish(code, state):
    """Exchange the code from the callback. Returns who signed in (company) for the page."""
    verifier, _ = _pending.pop(state, (None, None))
    if not verifier:
        raise ValueError("Okänd eller för gammal inloggning. Börja om från /oauth/start.")
    t = _token_request({"grant_type": "authorization_code", "code": code,
                        "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
    if not t.get("refresh_token"):
        raise ValueError("Fortnox gav ingen refresh-token. Kontrollera att access_type=offline accepteras av appen.")
    req = urllib.request.Request(f"{API}/3/companyinformation", headers={
        "Authorization": f"Bearer {t['access_token']}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        info = json.load(r).get("CompanyInformation", {})
    t.update(_company_fields(info))
    with _lock:
        old = _load(TOKEN, None)
        # The start link is a secret, but whoever holds it must not be able to swap in another company:
        # its books would land in this service's data next to the real one's.
        if old and old.get("database_number") and old["database_number"] != t["database_number"]:
            raise ValueError(f"Tjänsten är redan kopplad till {old.get('company')}. En inloggning mot "
                             f"{t['company'] or 'ett annat företag'} sparas inte. Byte av företag görs genom att "
                             "ta bort token.json i tjänstens state-mapp först.")
        _save(TOKEN, t)
    return t.get("company")


def _access_token():
    with _lock:
        t = _load(TOKEN, None)
        if not t:
            raise PermissionError("Fortnox är inte inloggat. Öppna /oauth/start/<nyckel> och logga in.")
        if time.time() < t.get("expires_at", 0):
            return t["access_token"]
        try:  # refresh tokens rotate: the old one is dead the moment this succeeds
            new = _token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
        except urllib.error.HTTPError as e:
            raise PermissionError(f"Fortnox avvisade refresh-token ({e.code}). Logga in igen via /oauth/start.")
        new.setdefault("refresh_token", t["refresh_token"])
        new.update({k: t.get(k) for k in COMPANY_KEYS})
        _save(TOKEN, new)
        return new["access_token"]


COMPANY_KEYS = ("company", "database_number", "organisation_number", "own_vat")


def _company_fields(info):
    """Who the books belong to, from /3/companyinformation. A Swedish VAT number is SE, the
    organisation number's ten digits and 01; the indexer needs it to tell the counterpart from us."""
    org = info.get("OrganizationNumber") or ""
    digits = re.sub(r"\D", "", org)
    return {"company": info.get("CompanyName"), "database_number": info.get("DatabaseNumber"),
            "organisation_number": org or None, "own_vat": f"SE{digits}01" if len(digits) == 10 else None}


def company():
    """The signed-in company's name, organisation number and VAT number (empty before sign-in)."""
    t = _load(TOKEN, None) or {}
    return {k: t.get(k) for k in COMPANY_KEYS}


def status():
    t = _load(TOKEN, None)
    s = _load(SYNCED, {})
    return {"api_konfigurerat": configured(), "inloggat": bool(t),
            "bolag": (t or {}).get("company"), "organisationsnummer": (t or {}).get("organisation_number"),
            "token_fornyad": (t or {}).get("refreshed_at"),
            "senaste_synk": s.get("last_run"), "senaste_synk_resultat": s.get("last_result"),
            "synkar_fran": SYNC_FROM, "skrivskyddat": READ_ONLY}


# --- API calls ------------------------------------------------------------------------------

def _error_text(e):
    """Fortnox' own message from an error answer: {"ErrorInformation": {"message": ..., "code": ...}}."""
    try:
        body = e.read().decode(errors="replace")
    except Exception:
        body = ""
    try:
        info = json.loads(body)
        info = info.get("ErrorInformation") or info.get("errorInformation") or info
        msg = info.get("message") or info.get("Message") or body
        code = info.get("code") or info.get("Code")
        return f"{msg} (Fortnox {e.code}{f', kod {code}' if code else ''})"
    except ValueError:
        return f"Fortnox svarade {e.code}: {body[:300] or e.reason}"


def _multipart(name, data):
    boundary = secrets.token_hex(16)
    safe = re.sub(r'["\r\n]', "_", name)
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def request(method, path, params=None, body=None, raw=False, upload=None, caller=None):
    """Any call to Fortnox. body is JSON; upload is (filename, bytes) sent as multipart. 429 is
    waited out. A GET that fails raises urllib's HTTPError (the sync handles those); anything else
    that fails raises FortnoxError with Fortnox' message. Every call but a plain GET is logged."""
    method = method.upper()
    if not path.startswith("/"):
        path = "/" + path
    url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    logged = method != "GET" or _acting_get(path)  # some GETs act: they send an invoice
    if logged and READ_ONLY:
        raise FortnoxError("Tjänsten är skrivskyddad (FORTNOX_READ_ONLY=true): bara läsning når Fortnox.")
    data, ctype = None, None
    if upload is not None:
        data, ctype = _multipart(*upload)
    elif body is not None:
        data, ctype = json.dumps(body, ensure_ascii=False).encode(), "application/json"
    for attempt in range(6):
        with _call_lock:
            gap = MIN_GAP - (time.time() - _last_call[0])
            if gap > 0:
                time.sleep(gap)
            _last_call[0] = time.time()
        headers = {"Authorization": f"Bearer {_access_token()}", "Accept": "*/*" if raw else "application/json"}
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                answer, code = r.read(), r.status
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 + attempt * 2)
                continue
            if method == "GET" and not caller:
                raise
            text = _error_text(e)
            if logged:
                _log_write(caller, method, path, params, body, upload, e.code, None, text)
            raise FortnoxError(text)
        result = answer if raw else json.loads(answer or b"{}")
        if logged:
            _log_write(caller, method, path, params, body, upload, code, None if raw else result, None)
        return result
    raise RuntimeError(f"Fortnox svarar 429 för länge på {path}")


def _get(path, params=None, raw=False):
    """GET for the sync. 429 is waited out; everything else surfaces as HTTPError."""
    return request("GET", path, params, raw=raw)


# GETs that do something in Fortnox: they send an invoice or an offer.
ACTING_GET = re.compile(r"/(email|einvoice|eprint|print|externalprint)$")


def _acting_get(path):
    return bool(ACTING_GET.search(path.split("?")[0]))


def _created_ids(result):
    """The numbers Fortnox gave what was written, for the log: {"Voucher": {"VoucherNumber": 12}} etc."""
    if not isinstance(result, dict) or len(result) != 1:
        return None
    obj = next(iter(result.values()))
    if not isinstance(obj, dict):
        return None
    keys = ("VoucherSeries", "VoucherNumber", "Year", "DocumentNumber", "GivenNumber", "CustomerNumber",
            "SupplierNumber", "ArticleNumber", "Number", "Id", "FileId", "InvoiceNumber")
    ids = {k: obj[k] for k in keys if obj.get(k) not in (None, "")}
    return {next(iter(result)): ids} if ids else None


def _log_write(caller, method, path, params, body, upload, code, result, error):
    entry = {"tid": datetime.now(timezone.utc).isoformat(timespec="seconds"), "nyckel": caller or "tjänsten",
             "metod": method, "sokvag": path, "parametrar": params or None, "status": code,
             "skapat": _created_ids(result), "fel": error}
    if body is not None:
        entry["kropp"] = body
    if upload is not None:
        entry["fil"] = {"namn": upload[0], "bytes": len(upload[1])}
    os.makedirs(STATE, exist_ok=True)
    with _lock:
        fd = os.open(WRITES, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def write_log(limit=50, offset=0, key=None):
    """The write log, newest first."""
    try:
        with open(WRITES) as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except OSError:
        rows = []
    if key:
        rows = [r for r in rows if r.get("nyckel") == key]
    rows.reverse()
    return len(rows), rows[offset:offset + limit]


def _pages(path, key, params):
    page = 1
    while True:
        d = _get(path, {**params, "page": page, "limit": 500})
        yield from d.get(key, [])
        meta = d.get("MetaInformation", {})
        if page >= int(meta.get("@TotalPages", 1) or 1):
            return
        page += 1


# --- sync -----------------------------------------------------------------------------------

def _safe(s, n=60):
    return (re.sub(r"[^\w.\-]+", "_", s or "", flags=re.UNICODE).strip("._") or "x")[:n]


def _path(data_dir, category, day, nr, fileid, name):
    y, m = (day or "0000-00")[:4], (day or "0000-00")[5:7]
    path = os.path.join(data_dir, "fortnox-api", y, m, category,
                        f"{_safe(str(nr), 20)}_{_safe(str(fileid), 40)}_{_safe(name)}")
    return path if path.lower().endswith(".pdf") else path + ".pdf"


def _write(path, pdf, meta):
    """The PDF (None: keep the one there) and its .json with Fortnox' own fields."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    files = ([(path, pdf)] if pdf is not None else []) + [
        (path + ".json", json.dumps(meta, ensure_ascii=False, indent=1).encode())]
    for p, payload in files:
        with open(p + ".part", "wb") as f:
            f.write(payload)
        os.replace(p + ".part", p)
    return path


def _fingerprint(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _keep(data_dir, seen, touched, key, inv, path, content, fetch, meta):
    """One invoice file, kept current. An invoice changes after we first fetched it -- it is paid,
    approved, booked, sent or cancelled -- and the list call already carries its current fields, so a
    changed fingerprint costs no extra call. The PDF is fetched again only when what it shows
    (content) changed; otherwise only the .json is rewritten. Returns 1 for a new file, else 0."""
    old = seen.get(key)
    fp = _fingerprint(inv)
    if old and old.get("fp") == fp:
        return 0
    if old is None and os.path.exists(path):  # synced before this bookkeeping existed: keep the PDF
        old = {"path": os.path.relpath(path, data_dir), "content": content}
    if old:
        path = os.path.join(data_dir, old["path"])  # where it was first written, even if its date moved
    refetch = not old or old.get("content") != content or not os.path.exists(path)
    _write(path, fetch() if refetch else None, meta)
    seen[key] = {"fp": fp, "content": content, "path": os.path.relpath(path, data_dir)}
    touched.append(os.path.relpath(path, data_dir))
    return 0 if old else 1


def books_path(data_dir, name):
    return os.path.join(data_dir, "fortnox-api", "bokforing", name)


def _save_data(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(path + ".part", path)


def fy_label(fy):
    a, b = fy.get("FromDate", ""), fy.get("ToDate", "")
    return a[:4] if a[5:] == "01-01" and b[:4] == a[:4] else f"{a[:4]}/{b[2:4]}"


def _financial_years(today):
    fys = _get("/3/financialyears").get("FinancialYears", [])
    return [fy for fy in fys if fy.get("ToDate", "") >= SYNC_FROM and fy.get("FromDate", "") <= today]


def _voucher(v, label):
    return {"id": f"{label}:{v.get('VoucherSeries')}{v.get('VoucherNumber')}", "rakenskapsar": label,
            "serie": v.get("VoucherSeries"), "nummer": v.get("VoucherNumber"), "datum": v.get("TransactionDate"),
            "beskrivning": v.get("Description") or None, "kommentar": v.get("Comments") or None,
            "referenstyp": v.get("ReferenceType") or None, "referensnummer": str(v.get("ReferenceNumber") or "") or None,
            "projekt": v.get("Project") or None, "kostnadsstalle": v.get("CostCenter") or None,
            "rader": [{"konto": r.get("Account"), "debet": r.get("Debit") or 0, "kredit": r.get("Credit") or 0,
                       "beskrivning": r.get("Description") or None, "info": r.get("TransactionInformation") or None,
                       "projekt": r.get("Project") or None, "kostnadsstalle": r.get("CostCenter") or None,
                       "antal": r.get("Quantity") or None, "borttagen": bool(r.get("Removed"))}
                      for r in v.get("VoucherRows") or []]}


def _bookkeeping(data_dir, years, since):
    """Every voucher with its rows, and the chart of accounts, per financial year."""
    path = books_path(data_dir, "verifikationer.json")
    vouchers = _load(path, {})
    accounts = _load(books_path(data_dir, "konton.json"), {})
    opening = _load(books_path(data_dir, "ingaende.json"), {})
    new = changed = 0
    for fy in years:
        label = fy_label(fy)
        base = {"financialyear": fy["Id"]}
        modified = set()
        if since:  # corrections and approvals change a voucher after we first fetched it
            modified = {f"{v.get('VoucherSeries')}{v.get('VoucherNumber')}"
                        for v in _pages("/3/vouchers", "Vouchers", {**base, "lastmodified": since})}
        for v in _pages("/3/vouchers", "Vouchers", {**base, "fromdate": max(SYNC_FROM, fy["FromDate"])}):
            ref = f"{v.get('VoucherSeries')}{v.get('VoucherNumber')}"
            key = f"{label}:{ref}"
            if key in vouchers and ref not in modified:
                continue
            series = urllib.parse.quote(str(v.get("VoucherSeries")), safe="")
            full = _get(f"/3/vouchers/{series}/{v.get('VoucherNumber')}", base).get("Voucher") or v
            if key in vouchers:
                changed += 1
            else:
                new += 1
            vouchers[key] = _voucher(full, label)
            if (new + changed) % 200 == 0:
                _save_data(path, vouchers)  # a first sync of a whole year takes a while; keep what we have
        names, ib = {}, {}
        for a in _pages("/3/accounts", "Accounts", base):
            names[str(a.get("Number"))] = a.get("Description")
            if a.get("BalanceBroughtForward"):  # opening balance of the year, assets positive
                ib[str(a.get("Number"))] = a["BalanceBroughtForward"]
        accounts[label], opening[label] = names, ib
    _save_data(path, vouchers)
    _save_data(books_path(data_dir, "konton.json"), accounts)
    _save_data(books_path(data_dir, "ingaende.json"), opening)
    _save_data(books_path(data_dir, "rakenskapsar.json"),
               [{"rakenskapsar": fy_label(fy), "fran": fy.get("FromDate"), "till": fy.get("ToDate"), "id": fy.get("Id")}
                for fy in years])
    return {"nya": new, "andrade": changed, "totalt": len(vouchers)}


def refresh_voucher(data_dir, series, number, year_id=None):
    """Fetch one voucher again and put it in the synced bookkeeping, so a voucher an agent just
    wrote shows in the financial tools at once instead of after the next sync."""
    fys = _get("/3/financialyears").get("FinancialYears", [])
    fy = next((f for f in fys if year_id and f.get("Id") == year_id), None) or max(
        fys, key=lambda f: f.get("FromDate", ""), default=None)
    if not fy:
        return None
    params = {"financialyear": fy["Id"]}
    v = _get(f"/3/vouchers/{urllib.parse.quote(str(series), safe='')}/{number}", params).get("Voucher")
    if not v:
        return None
    label = fy_label(fy)
    path = books_path(data_dir, "verifikationer.json")
    with _lock:
        vouchers = _load(path, {})
        entry = _voucher(v, label)
        vouchers[entry["id"]] = entry
        _save_data(path, vouchers)
        years = _load(books_path(data_dir, "rakenskapsar.json"), [])
        if not any(y.get("rakenskapsar") == label for y in years):
            years.append({"rakenskapsar": label, "fran": fy.get("FromDate"), "till": fy.get("ToDate"), "id": fy.get("Id")})
            _save_data(books_path(data_dir, "rakenskapsar.json"), years)
    return entry


def _supplier_invoices(data_dir, seen, touched, today):
    n = 0
    for inv in _pages("/3/supplierinvoices", "SupplierInvoices", {"fromdate": SYNC_FROM, "todate": today}):
        gn = inv.get("GivenNumber")
        mine = [k for k in seen if k.startswith(f"si:{gn}:")]
        if mine and all(seen[k].get("fp") == _fingerprint(inv) for k in mine):
            continue  # nothing changed: no need to ask for its files again
        conns = _get("/3/supplierinvoicefileconnections", {"supplierinvoicenumber": gn}).get(
            "SupplierInvoiceFileConnections", [])
        for c in conns:
            # the scanned original never changes, so content is just the file: only the .json follows the invoice
            n += _keep(data_dir, seen, touched, f"si:{gn}:{c.get('FileId')}", inv,
                       _path(data_dir, "Leverantörsfakturor", inv.get("InvoiceDate"), gn, c["FileId"],
                             c.get("Name") or "faktura.pdf"),
                       c.get("FileId"), lambda c=c: _get(f"/3/archive/{c['FileId']}", raw=True), {
                           "date": inv.get("InvoiceDate"), "counterpart_name": inv.get("SupplierName"),
                           "amount": inv.get("Total"), "currency": inv.get("Currency"), "fortnox_nr": str(gn),
                           "fortnox": inv})
    return n


def _customer_invoices(data_dir, seen, touched, today):
    n = 0
    for inv in _pages("/3/invoices", "Invoices", {"fromdate": SYNC_FROM, "todate": today}):
        dn = inv.get("DocumentNumber")
        key = f"ci:{dn}"
        if inv.get("Cancelled") and key not in seen:
            continue  # cancelled before we ever saw it; one we have keeps its file, marked Cancelled
        # the PDF is Fortnox' rendering of the invoice, so it is fetched again when what it shows changes
        content = _fingerprint(inv.get("Total"), inv.get("Currency"), inv.get("CustomerName"), inv.get("InvoiceDate"))
        n += _keep(data_dir, seen, touched, key, inv,
                   _path(data_dir, "Kundfakturor", inv.get("InvoiceDate"), dn, "preview", f"faktura_{dn}.pdf"),
                   content, lambda dn=dn: _get(f"/3/invoices/{dn}/preview", raw=True), {
                       "date": inv.get("InvoiceDate"), "counterpart_name": inv.get("CustomerName"),
                       "amount": inv.get("Total"), "currency": inv.get("Currency"), "fortnox_nr": str(dn),
                       "fortnox": inv})
    return n


def _vouchers(data_dir, done, years):
    """The documents attached to vouchers. VoucherYear is Fortnox' id of the financial year."""
    n = 0
    booked = _load(books_path(data_dir, "verifikationer.json"), {})
    for fy in years:
        label = fy_label(fy)
        for c in _pages("/3/voucherfileconnections", "VoucherFileConnections", {"voucheryear": fy["Id"]}):
            ref = f"{c.get('VoucherSeries')}{c.get('VoucherNumber')}"
            key = f"vo:{label}:{ref}:{c.get('FileId')}"
            if key in done:
                continue
            pdf = _get(f"/3/archive/{c['FileId']}", raw=True)
            day = (booked.get(f"{label}:{ref}") or {}).get("datum")
            _write(_path(data_dir, "Verifikationer", day or fy["FromDate"], ref, c["FileId"],
                         c.get("Name") or "verifikation.pdf"), pdf, {
                       "date": day, "counterpart_name": None, "fortnox_nr": ref,
                       "verifikation": f"{label}:{ref}", "fortnox": c})
            done.add(key)
            n += 1
    return n


def sync(data_dir):
    """Fetch what is new or changed since the last run. Returns counts and the files (relative to
    data_dir) whose PDF or .json was rewritten, for the index. The sync itself only reads."""
    s = _load(SYNCED, {})
    done = set(s.get("done", []))
    seen = s.get("invoices", {})
    touched = []
    today = date.today().isoformat()
    since = (s.get("books_since") or "")[:10] or None
    result, years = {}, []

    def save(**extra):
        _save(SYNCED, {**s, "done": sorted(done), "invoices": seen, **extra})

    def years_():
        years.extend(_financial_years(today))
        return [fy_label(fy) for fy in years]

    for name, fn in (("rakenskapsar", years_),
                     ("bokforing", lambda: _bookkeeping(data_dir, years, since)),
                     ("leverantorsfakturor", lambda: _supplier_invoices(data_dir, seen, touched, today)),
                     ("kundfakturor", lambda: _customer_invoices(data_dir, seen, touched, today)),
                     ("verifikationer", lambda: _vouchers(data_dir, done, years))):
        try:
            result[name] = fn()
        except urllib.error.HTTPError as e:
            result[name] = f"fel {e.code}" + (" (saknar behörighet eller licens)" if e.code in (401, 403) else "")
        finally:
            save()
    if isinstance(result.get("bokforing"), dict):
        s["books_since"] = today  # next run re-reads what changed from today on
    result["uppdaterade_fakturor"] = len(touched) - sum(v for v in (result.get("leverantorsfakturor"),
                                                                    result.get("kundfakturor")) if isinstance(v, int))
    save(last_run=datetime.now(timezone.utc).isoformat(timespec="seconds"), last_result=result)
    return result, touched
