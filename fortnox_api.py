"""Fortnox API: sign-in per company, a sync of documents and bookkeeping into the company's
folder, and the calls agents make to read and write.

One integration in Fortnox' developer portal and one redirect URI serve several companies: each
company's system administrator authorizes separately and Fortnox hands out separate tokens. Here a
company is a Slot: a short name (slug) with its own folder, tokens, synced books, write log and
rate-limit clock. Which company a call belongs to is never an argument: the HTTP layer binds the
slot of the agent key (use), and everything below reads it with cur(). Outside a bound slot nothing
works, so a forgotten binding fails instead of reaching some other company.

One connection per company, shared by the agents bound to it: by default it is authorized as a
Fortnox service account (account_type=service), so it is tied to no person's user or licence and
keeps working when someone leaves. Only a system administrator of the company can authorize it.
Tokens come from the OAuth authorization-code flow (PKCE).

Agents read and write (request). Every call that is not a plain GET is appended to the company's
write log (writes.jsonl in its state folder) with the agent key's name, so a person can see what
was done. Fortnox scopes always grant read AND write; FORTNOX_READ_ONLY=true refuses everything but GET.

Environment:
  FORTNOX_CLIENT_ID, FORTNOX_CLIENT_SECRET    from the integration in Fortnox' developer portal
  FORTNOX_REDIRECT_URI    must match the integration exactly, e.g. https://fortnox.example.se/oauth/callback
  FORTNOX_SCOPES          default: see SCOPES below; must be a subset of what the integration allows
  FORTNOX_ACCOUNT_TYPE    default "service" (the integration must allow service accounts); empty = the user who signs in
  FORTNOX_SYNC_FROM       first date to sync, default January 1st of last year
  FORTNOX_READ_ONLY       "true": only GET reaches Fortnox, write tools are not offered
  FORTNOX_API_BASE / FORTNOX_AUTH_BASE    default https://api.fortnox.se / https://apps.fortnox.se
  STATE_DIR               default DATA_DIR/.state  (token.json, sync.json, writes.jsonl; mode 600)

Folders. The default company (keys without a company part) is a single-company deployment as it
always was: <root> = DATA_DIR, state in STATE_DIR. A named company has the same layout one level
down: <root> = DATA_DIR/companies/<slug>, state in <root>/.state (STATE_DIR/companies/<slug> when
STATE_DIR is set). The default company's index never walks DATA_DIR/companies.

Layout written: <root>/fortnox-api/<year>/<month>/<Leverantörsfakturor|Kundfakturor|Verifikationer>/
<nr>_<fileid>_<name>.pdf, each with <file>.json holding date, counterpart, amount, currency and the
raw Fortnox record -- the indexer prefers those fields over what it reads out of the PDF.

The bookkeeping itself goes to <root>/fortnox-api/bokforing/: verifikationer.json (every voucher
with its rows, keyed "<year>:<series><number>"), konton.json (account names per financial year) and
rakenskapsar.json. A financial year is labelled by its first year ("2026", or "2025/26" when it does
not follow the calendar year). Vouchers changed since the previous sync are fetched again.
"""
import base64
import contextlib
import contextvars
import hashlib
import hmac
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

import index

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
DATA = os.environ.get("DATA_DIR", "/data")
STATE = os.environ.get("STATE_DIR") or os.path.join(DATA, ".state")  # the default company's
MIN_GAP = 0.22          # 25 requests per 5 s per token, with margin
DEFAULT = "default"     # the company of a key without a company part: the single-company layout
COMPANIES = index.COMPANIES  # DATA_DIR/companies/<slug>: the named companies; the index skips it
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_signin_lock = threading.Lock()
_pending = {}           # nonce -> (code_verifier, created, slug)
_state_key = secrets.token_bytes(32)  # signs the OAuth state; pending sign-ins die with the process anyway


class FortnoxError(ValueError):
    """Fortnox refused a call; the message is Fortnox' own, for the agent to act on."""


# --- companies ------------------------------------------------------------------------------

class Slot:
    """One company: where its files are, and the locks, caches and rate-limit clock that are its own."""

    def __init__(self, slug):
        if not SLUG.fullmatch(slug or ""):
            raise ValueError(f"{slug!r} is not a company name: a-z, 0-9 and -, at most 32 characters")
        self.slug = slug
        if slug == DEFAULT:
            self.root, self.state = DATA, STATE
        else:
            self.root = os.path.join(DATA, COMPANIES, slug)
            self.state = (os.path.join(os.environ["STATE_DIR"], COMPANIES, slug) if os.environ.get("STATE_DIR")
                          else os.path.join(self.root, ".state"))
        self.token = os.path.join(self.state, "token.json")
        self.synced = os.path.join(self.state, "sync.json")
        self.writes = os.path.join(self.state, "writes.jsonl")
        self.uploads = os.path.join(self.state, "uploads")
        self.manifest = os.path.join(self.root, "manifest.jsonl")
        self.lock = threading.Lock()        # token, write log, the voucher file
        self.call_lock = threading.Lock()   # Fortnox' rate limit is per token, so per company
        self.last_call = 0.0
        self.sync_lock = threading.Lock()
        self.wake = threading.Event()       # set after a write: sync this company soon
        self.cache_lock = threading.Lock()
        self.cache = {}                     # the parsed index and books (mcp_server)

    @property
    def start_path(self):
        return "/oauth/start/<OAUTH_START_TOKEN>" + ("" if self.slug == DEFAULT else f"/{self.slug}")

    def __repr__(self):
        return f"<company {self.slug}>"


SLOTS = {}  # slug -> Slot: the companies this service serves, one per company named in MCP_KEYS
_current = contextvars.ContextVar("company")  # no default on purpose


def slot(slug):
    """The served company with this slug, created on first use."""
    if slug not in SLOTS:
        SLOTS[slug] = Slot(slug)
    return SLOTS[slug]


@contextlib.contextmanager
def use(s):
    """Bind a company for the calls made inside: a request with that company's key, or its sync."""
    mark = _current.set(s)
    try:
        yield s
    finally:
        _current.reset(mark)


def cur():
    try:
        return _current.get()
    except LookupError:
        raise RuntimeError("no company is bound to this call") from None


def configured():
    return bool(CLIENT_ID and CLIENT_SECRET and REDIRECT_URI)


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
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

def _state_mac(slug, nonce):
    return hmac.new(_state_key, f"{slug}.{nonce}".encode(), hashlib.sha256).hexdigest()[:24]


def authorize_url(s):
    """Where to send the person who signs in for company s. The redirect URI is the one registered
    in Fortnox for every company; which company comes back is in the signed state. PKCE verifier
    and state are kept in memory for 10 minutes."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    nonce = secrets.token_urlsafe(16).replace(".", "-")
    state = f"{s.slug}.{nonce}.{_state_mac(s.slug, nonce)}"
    now = time.time()
    for k in [k for k, (_, t, _) in list(_pending.items()) if now - t > 600]:
        _pending.pop(k, None)
    _pending[nonce] = (verifier, now, s.slug)
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


def _same_company(a, b):
    """Whether two sign-ins are the same Fortnox company: True or False, or None when Fortnox gave
    nothing to compare. The database number is the company in Fortnox; the organisation number
    stands in when one side lacks it."""
    da, db = a.get("database_number"), b.get("database_number")
    if da and db:
        return str(da) == str(db)
    oa, ob = (re.sub(r"\D", "", x.get("organisation_number") or "") for x in (a, b))
    return oa == ob if oa and ob else None


def _slugs_on_disk():
    """Every company with a folder here, served now or left from an earlier configuration."""
    found = {DEFAULT, *SLOTS}
    for base in {os.path.join(DATA, COMPANIES), os.path.join(os.environ.get("STATE_DIR") or DATA, COMPANIES)}:
        if os.path.isdir(base):
            found.update(d for d in os.listdir(base) if SLUG.fullmatch(d))
    return sorted(found)


def dormant():
    """Companies signed in here earlier that no key reaches now: kept on disk, not served."""
    return [slug for slug in _slugs_on_disk() if slug not in SLOTS and os.path.exists(Slot(slug).token)]


def finish(code, state):
    """Exchange the code from the callback and keep the tokens in the company the state names.
    Returns (slot, company name) for the page."""
    parts = (state or "").split(".")
    slug, nonce, mac = parts if len(parts) == 3 else ("", "", "")
    signed = hmac.compare_digest(mac.encode(), _state_mac(slug, nonce).encode())
    verifier, _, started_for = _pending.pop(nonce, (None, None, None)) if signed else (None, None, None)
    s = SLOTS.get(slug)
    if not (verifier and s and started_for == slug):
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
    with _signin_lock:
        old = _load(s.token, None)
        # The start link is a secret, but whoever holds it must not be able to swap in another company:
        # its books would land in this company's data next to the real one's.
        if old and _same_company(old, t) is False:
            raise ValueError(f"{_place(s)} är redan kopplad till {old.get('company')}. En inloggning mot "
                             f"{t['company'] or 'ett annat företag'} sparas inte. Byte av företag görs genom att "
                             f"ta bort token.json i {s.state} först.")
        # And one Fortnox company lives in one place: two copies of the same books, each with its own
        # refresh token and write log, would drift apart.
        for other in _slugs_on_disk():
            if other != s.slug and _same_company(_load(Slot(other).token, None) or {}, t):
                raise ValueError(f"{t['company'] or 'Företaget'} är redan kopplat till {_place(Slot(other)).lower()} "
                                 f"i den här tjänsten. Ett företag i Fortnox kan bara ligga på en plats; "
                                 f"inloggningen till {_place(s).lower()} sparas inte.")
        with s.lock:
            _save(s.token, t)
    return s, t.get("company")


def _place(s):
    return "Tjänsten" if s.slug == DEFAULT else f"Företagsplatsen {s.slug}"


def _access_token():
    s = cur()
    with s.lock:
        t = _load(s.token, None)
        if not t:
            raise PermissionError(f"Fortnox är inte inloggat. Öppna {s.start_path} och logga in.")
        if time.time() < t.get("expires_at", 0):
            return t["access_token"]
        try:  # refresh tokens rotate: the old one is dead the moment this succeeds
            new = _token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
        except urllib.error.HTTPError as e:
            raise PermissionError(f"Fortnox avvisade refresh-token ({e.code}). Logga in igen via /oauth/start.")
        new.setdefault("refresh_token", t["refresh_token"])
        new.update({k: t.get(k) for k in COMPANY_KEYS})
        _save(s.token, new)
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
    t = _load(cur().token, None) or {}
    return {k: t.get(k) for k in COMPANY_KEYS}


def status():
    t = _load(cur().token, None)
    s = _load(cur().synced, {})
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
    s = cur()
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
        with s.call_lock:
            gap = MIN_GAP - (time.time() - s.last_call)
            if gap > 0:
                time.sleep(gap)
            s.last_call = time.time()
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
    s = cur()
    os.makedirs(s.state, exist_ok=True)
    with s.lock:
        fd = os.open(s.writes, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def write_log(limit=50, offset=0, key=None):
    """The bound company's write log, newest first."""
    try:
        with open(cur().writes) as f:
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


def refresh_voucher(series, number, year_id=None):
    """Fetch one voucher again and put it in the synced bookkeeping, so a voucher an agent just
    wrote shows in the financial tools at once instead of after the next sync."""
    data_dir = cur().root
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
    with cur().lock:
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


def sync():
    """Fetch what is new or changed in the bound company since the last run. Returns counts and the
    files (relative to its folder) whose PDF or .json was rewritten, for the index. The sync itself
    only reads."""
    data_dir, synced = cur().root, cur().synced
    s = _load(synced, {})
    done = set(s.get("done", []))
    seen = s.get("invoices", {})
    touched = []
    today = date.today().isoformat()
    since = (s.get("books_since") or "")[:10] or None
    result, years = {}, []

    def save(**extra):
        _save(synced, {**s, "done": sorted(done), "invoices": seen, **extra})

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
