"""The tools that write to Fortnox, and the upload store they read files from.

Every call goes through fortnox_api.request with the agent key's name, so it lands in the write log.
After a write the synced copy is brought up to date: a new voucher is fetched at once (the financial
tools see it in the same answer), everything else wakes the background sync (after_write).

Field names in the tools are Swedish, like the read tools; what goes to Fortnox uses Fortnox' own
names. Each create tool takes `extra`: more Fortnox fields for the same object, passed as they are,
for what the tool does not name.

Upload store: POST /upload?namn=<file> with the agent key and the file as the body gives an id;
ladda_upp_fil takes that id, so a file does not have to travel base64 inside a tool call.
"""
import base64
import contextvars
import json
import os
import re
import secrets
import threading
import time
import urllib.parse

import fortnox_api
from fortnox_api import FortnoxError

DATA = os.environ.get("DATA_DIR", "/data")
UPLOADS = os.path.join(fortnox_api.STATE, "uploads")
UPLOAD_MAX = 25 * 1024 * 1024
UPLOAD_HOURS = 24

CALLER = contextvars.ContextVar("caller", default=None)  # set by the MCP handler per call
after_write = lambda: None  # replaced by mcp_server: wakes the background sync
_upload_lock = threading.Lock()


def call(method, path, params=None, body=None, upload=None, raw=False):
    try:
        return fortnox_api.request(method, path, params, body, raw=raw, upload=upload, caller=CALLER.get())
    finally:
        if method.upper() != "GET" or fortnox_api._acting_get(path):
            after_write()


def _clean(d):
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _extra(a):
    x = a.get("extra") or {}
    if not isinstance(x, dict):
        raise ValueError("extra ska vara ett objekt med Fortnox egna fältnamn, t.ex. {\"OurReference\": \"Anna\"}.")
    return x


def _num(x, what):
    try:
        return round(float(x or 0), 2)
    except (TypeError, ValueError):
        raise ValueError(f"{what} ska vara ett tal, inte {x!r}.")


def _nr(x, what):
    s = str(x or "").strip()
    if not s or "/" in s or "?" in s:
        raise ValueError(f"Ange {what}.")
    return urllib.parse.quote(s, safe="")


def _rows(rows, account_key="Account"):
    out = []
    for i, r in enumerate(rows or [], 1):
        if not isinstance(r, dict) or not str(r.get("konto") or "").strip().isdigit():
            raise ValueError(f"Rad {i}: konto saknas eller är inte ett kontonummer.")
        d, c = _num(r.get("debet"), f"Rad {i}: debet"), _num(r.get("kredit"), f"Rad {i}: kredit")
        if d < 0 or c < 0 or (d and c) or not (d or c):
            raise ValueError(f"Rad {i}: ange antingen debet eller kredit, ett positivt belopp.")
        out.append(_clean({account_key: int(r["konto"]), "Debit": d, "Credit": c,
                           "TransactionInformation": r.get("beskrivning"), "Project": r.get("projekt"),
                           "CostCenter": r.get("kostnadsstalle"), "Quantity": r.get("antal")}))
    return out


def _balanced(rows):
    d, c = round(sum(r.get("Debit", 0) for r in rows), 2), round(sum(r.get("Credit", 0) for r in rows), 2)
    if d != c:
        raise ValueError(f"Raderna balanserar inte: debet {d} och kredit {c}. Skillnaden är {round(d - c, 2)}.")
    return d


def _one(result):
    """The object in a Fortnox answer: {"Voucher": {...}} -> {...}."""
    return next(iter(result.values())) if isinstance(result, dict) and len(result) == 1 else result


# --- vouchers -------------------------------------------------------------------------------

def t_skapa_verifikation(a):
    rows = _rows(a.get("rader"))
    if len(rows) < 2:
        raise ValueError("En verifikation behöver minst två rader.")
    total = _balanced(rows)
    if not a.get("datum") or not a.get("beskrivning"):
        raise ValueError("Ange datum (ÅÅÅÅ-MM-DD) och beskrivning.")
    voucher = _clean({"VoucherSeries": a.get("serie") or "A", "TransactionDate": a["datum"],
                      "Description": a["beskrivning"], "Comments": a.get("kommentar"),
                      "Project": a.get("projekt"), "CostCenter": a.get("kostnadsstalle"),
                      "VoucherRows": rows, **_extra(a)})
    v = _one(call("POST", "/3/vouchers", body={"Voucher": voucher}))
    out = {"skapad": True, "serie": v.get("VoucherSeries"), "nummer": v.get("VoucherNumber"),
           "rakenskapsar_id": v.get("Year"), "summa": total}
    try:
        out["verifikation"] = fortnox_api.refresh_voucher(DATA, v.get("VoucherSeries"), v.get("VoucherNumber"), v.get("Year"))
    except Exception as e:  # written in Fortnox all the same; the next sync picks it up
        out["not"] = f"Skapad i Fortnox, men kunde inte hämtas tillbaka direkt ({e}). Nästa synk tar med den."
    out["rattelse"] = ("En verifikation kan inte tas bort i Fortnox. Fel rättas med en ny verifikation som vänder "
                       "raderna (eller med Fortnox rättelse i serien).")
    return out


# --- supplier invoices ----------------------------------------------------------------------

def t_skapa_leverantorsfaktura(a):
    if not a.get("leverantor"):
        raise ValueError("Ange leverantor: leverantörsnumret i Fortnox (se spara_register / fortnox_api GET /3/suppliers).")
    inv = _clean({"SupplierNumber": str(a["leverantor"]), "InvoiceNumber": a.get("fakturanummer"),
                  "InvoiceDate": a.get("fakturadatum"), "DueDate": a.get("forfallodatum"),
                  "Total": a.get("total"), "VAT": a.get("moms"), "Currency": a.get("valuta"), "OCR": a.get("ocr"),
                  "Comments": a.get("kommentar"), "Project": a.get("projekt"), "CostCenter": a.get("kostnadsstalle"),
                  "SupplierInvoiceRows": _rows(a.get("rader"), "Account") if a.get("rader") else None, **_extra(a)})
    s = _one(call("POST", "/3/supplierinvoices", body={"SupplierInvoice": inv}))
    return {"skapad": True, "nummer": s.get("GivenNumber"), "leverantor": s.get("SupplierName") or s.get("SupplierNumber"),
            "total": s.get("Total"), "bokford": s.get("Booked"),
            "nasta_steg": "Bifoga fakturan med ladda_upp_fil (koppla_leverantorsfaktura) och bokför med "
                          "leverantorsfaktura_atgard atgard=bokfor."}


SUPPLIER_ACTIONS = {"bokfor": "bookkeep", "makulera": "cancel", "kreditera": "credit",
                    "attestera_bokforing": "approvalbookkeep", "attestera_betalning": "approvalpayment"}


def t_leverantorsfaktura_atgard(a):
    act = SUPPLIER_ACTIONS.get(a.get("atgard"))
    if not act:
        raise ValueError(f"atgard ska vara en av: {', '.join(SUPPLIER_ACTIONS)}")
    s = _one(call("PUT", f"/3/supplierinvoices/{_nr(a.get('nummer'), 'nummer (GivenNumber)')}/{act}"))
    return {"klart": a["atgard"], "nummer": s.get("GivenNumber"), "bokford": s.get("Booked"),
            "makulerad": s.get("Cancelled"), "kreditfaktura": s.get("Credit") or None}


# --- customer invoices ----------------------------------------------------------------------

def _invoice_rows(rows):
    out = []
    for i, r in enumerate(rows or [], 1):
        if not isinstance(r, dict):
            raise ValueError(f"Rad {i} ska vara ett objekt.")
        row = _clean({"ArticleNumber": r.get("artikel"), "Description": r.get("beskrivning"),
                      "DeliveredQuantity": r.get("antal"), "Price": r.get("pris"), "AccountNumber": r.get("konto"),
                      "VAT": r.get("moms"), "Discount": r.get("rabatt"), "Unit": r.get("enhet"),
                      "Project": r.get("projekt"), "CostCenter": r.get("kostnadsstalle")})
        if not (row.get("ArticleNumber") or row.get("Description")):
            raise ValueError(f"Rad {i}: ange artikel eller beskrivning.")
        out.append(row)
    return out


def t_skapa_kundfaktura(a):
    if not a.get("kund"):
        raise ValueError("Ange kund: kundnumret i Fortnox (se spara_register / fortnox_api GET /3/customers).")
    rows = _invoice_rows(a.get("rader"))
    if not rows:
        raise ValueError("En kundfaktura behöver minst en rad.")
    inv = _clean({"CustomerNumber": str(a["kund"]), "InvoiceDate": a.get("fakturadatum"),
                  "DueDate": a.get("forfallodatum"), "Currency": a.get("valuta"),
                  "OurReference": a.get("var_referens"), "YourReference": a.get("er_referens"),
                  "Remarks": a.get("text_pa_fakturan"), "Comments": a.get("kommentar"),
                  "Project": a.get("projekt"), "CostCenter": a.get("kostnadsstalle"),
                  "InvoiceRows": rows, **_extra(a)})
    i = _one(call("POST", "/3/invoices", body={"Invoice": inv}))
    return {"skapad": True, "nummer": i.get("DocumentNumber"), "kund": i.get("CustomerName"),
            "total": i.get("Total"), "moms": i.get("TotalVAT"), "forfallodatum": i.get("DueDate"),
            "nasta_steg": "Bokför och skicka med kundfaktura_atgard (bokfor, skicka_epost eller skicka_efaktura)."}


# Fortnox sends an invoice with a GET; the rest are PUTs.
INVOICE_ACTIONS = {"bokfor": ("PUT", "bookkeep"), "makulera": ("PUT", "cancel"), "kreditera": ("PUT", "credit"),
                   "skicka_epost": ("GET", "email"), "skicka_efaktura": ("GET", "einvoice"),
                   "markera_skickad": ("PUT", "externalprint")}


def t_kundfaktura_atgard(a):
    act = INVOICE_ACTIONS.get(a.get("atgard"))
    if not act:
        raise ValueError(f"atgard ska vara en av: {', '.join(INVOICE_ACTIONS)}")
    i = _one(call(act[0], f"/3/invoices/{_nr(a.get('nummer'), 'nummer (DocumentNumber)')}/{act[1]}"))
    return {"klart": a["atgard"], "nummer": i.get("DocumentNumber"), "bokford": i.get("Booked"),
            "skickad": i.get("Sent"), "makulerad": i.get("Cancelled"),
            "kreditfaktura": i.get("CreditInvoiceReference") or None}


# --- registers ------------------------------------------------------------------------------

REGISTERS = {"kund": ("/3/customers", "Customer", "CustomerNumber"),
             "leverantor": ("/3/suppliers", "Supplier", "SupplierNumber"),
             "artikel": ("/3/articles", "Article", "ArticleNumber"),
             "projekt": ("/3/projects", "Project", "ProjectNumber")}


def t_spara_register(a):
    reg = REGISTERS.get(a.get("typ"))
    if not reg:
        raise ValueError(f"typ ska vara en av: {', '.join(REGISTERS)}")
    path, key, number = reg
    fields = a.get("falt") or {}
    if not isinstance(fields, dict) or not fields:
        raise ValueError(f"falt: Fortnox egna fält för {key}, t.ex. {{\"Name\": \"Exempel AB\", "
                         "\"OrganisationNumber\": \"556677-8899\"}.")
    if a.get("nummer"):
        r = _one(call("PUT", f"{path}/{_nr(a['nummer'], 'nummer')}", body={key: fields}))
        done = "uppdaterad"
    else:
        r = _one(call("POST", path, body={key: fields}))
        done = "skapad"
    return {done: True, "typ": a["typ"], "nummer": r.get(number), "namn": r.get("Name") or r.get("Description")}


# --- files ----------------------------------------------------------------------------------

def save_upload(name, data):
    """Keep an uploaded file for UPLOAD_HOURS; returns its id."""
    if len(data) > UPLOAD_MAX:
        raise ValueError(f"Filen är större än {UPLOAD_MAX // (1024 * 1024)} MB.")
    os.makedirs(UPLOADS, exist_ok=True)
    now = time.time()
    with _upload_lock:
        for f in os.listdir(UPLOADS):
            if now - os.path.getmtime(os.path.join(UPLOADS, f)) > UPLOAD_HOURS * 3600:
                os.remove(os.path.join(UPLOADS, f))
        uid = secrets.token_hex(12)
        safe = re.sub(r"[^\w.\-]+", "_", name or "fil", flags=re.UNICODE)[:80] or "fil"
        fd = os.open(os.path.join(UPLOADS, f"{uid}__{safe}"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    return uid


def _upload(uid):
    if not re.fullmatch(r"[0-9a-f]{24}", uid or ""):
        raise ValueError("Ogiltigt uppladdnings-id.")
    found = [f for f in (os.listdir(UPLOADS) if os.path.isdir(UPLOADS) else []) if f.startswith(uid + "__")]
    if not found:
        raise ValueError(f"Uppladdningen finns inte (den sparas i {UPLOAD_HOURS} h). Ladda upp igen via POST /upload.")
    with open(os.path.join(UPLOADS, found[0]), "rb") as f:
        return found[0].split("__", 1)[1], f.read()


# The inbox folders in Fortnox' file archive (the path parameter of POST /3/inbox, as Fortnox writes it).
FOLDERS = {"verifikationer": "Inbox_v", "leverantorsfakturor": "Inbox_s", "arkiv": None}


def t_ladda_upp_fil(a):
    if a.get("uppladdning"):
        name, data = _upload(a["uppladdning"])
        name = a.get("namn") or name
    elif a.get("innehall_base64"):
        try:
            data = base64.b64decode(a["innehall_base64"], validate=True)
        except ValueError:
            raise ValueError("innehall_base64 är inte giltig base64.")
        name = a.get("namn") or "fil.pdf"
    else:
        raise ValueError("Ange uppladdning (id från POST /upload) eller innehall_base64.")
    folder = a.get("mapp") or ("leverantorsfakturor" if a.get("koppla_leverantorsfaktura") else "verifikationer")
    if folder not in FOLDERS:
        raise ValueError(f"mapp ska vara en av: {', '.join(FOLDERS)}")
    if FOLDERS[folder]:
        f = _one(call("POST", "/3/inbox", params={"path": FOLDERS[folder]}, upload=(name, data)))
    else:
        f = _one(call("POST", "/3/archive", upload=(name, data)))
    out = {"uppladdad": True, "fil_id": f.get("Id"), "namn": f.get("Name") or name, "bytes": len(data), "mapp": folder}
    v = a.get("koppla_verifikation")
    if v:
        if not isinstance(v, dict) or not v.get("serie") or not v.get("nummer"):
            raise ValueError("koppla_verifikation: {\"serie\": \"A\", \"nummer\": 12, \"rakenskapsar_id\": 3}.")
        conn = _clean({"FileId": f.get("Id"), "VoucherSeries": v["serie"], "VoucherNumber": v["nummer"],
                       "VoucherYear": v.get("rakenskapsar_id")})
        call("POST", "/3/voucherfileconnections", body={"VoucherFileConnection": conn})
        out["kopplad_till"] = f"verifikation {v['serie']}{v['nummer']}"
    elif a.get("koppla_leverantorsfaktura"):
        call("POST", "/3/supplierinvoicefileconnections", body={"SupplierInvoiceFileConnection": {
            "FileId": f.get("Id"), "SupplierInvoiceNumber": str(a["koppla_leverantorsfaktura"])}})
        out["kopplad_till"] = f"leverantörsfaktura {a['koppla_leverantorsfaktura']}"
    return out


# --- payments -------------------------------------------------------------------------------

PAYMENTS = {"kundfaktura": ("/3/invoicepayments", "InvoicePayment"),
            "leverantorsfaktura": ("/3/supplierinvoicepayments", "SupplierInvoicePayment")}


def t_registrera_betalning(a):
    kind = PAYMENTS.get(a.get("typ"))
    if not kind:
        raise ValueError(f"typ ska vara en av: {', '.join(PAYMENTS)}")
    if not a.get("nummer") or not a.get("datum"):
        raise ValueError("Ange nummer (fakturans) och datum (betaldag, ÅÅÅÅ-MM-DD).")
    path, key = kind
    pay = _clean({"InvoiceNumber": str(a["nummer"]), "Amount": _num(a.get("belopp"), "belopp"),
                  "PaymentDate": a["datum"], "ModeOfPayment": a.get("betalsatt"), **_extra(a)})
    p = _one(call("POST", path, body={key: pay}))
    out = {"registrerad": True, "betalning": p.get("Number"), "faktura": p.get("InvoiceNumber"), "belopp": p.get("Amount")}
    if a.get("bokfor", True) and p.get("Number") is not None:
        b = _one(call("PUT", f"{path}/{_nr(p['Number'], 'betalningens nummer')}/bookkeep"))
        out["bokford"] = (b or {}).get("Booked", True)
    return out


# --- anything else --------------------------------------------------------------------------

def t_fortnox_api(a):
    method = (a.get("metod") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "DELETE"):
        raise ValueError("metod ska vara GET, POST, PUT eller DELETE.")
    path = (a.get("sokvag") or "").strip()
    if not re.fullmatch(r"/(3|api)/[\w\-./%]*", path) or ".." in path:
        raise ValueError("sokvag är en sökväg i Fortnox API, t.ex. /3/customers eller /3/vouchers/A/12.")
    params = a.get("parametrar") or None
    if params is not None and not isinstance(params, dict):
        raise ValueError("parametrar ska vara ett objekt, t.ex. {\"financialyear\": 3}.")
    body = a.get("kropp")
    if body is not None and method == "GET":
        raise ValueError("En GET har ingen kropp.")
    if method == "GET" and not fortnox_api._acting_get(path):  # reads are not logged and wake no sync
        answer = fortnox_api.request("GET", path, params, raw=True, caller=CALLER.get())
    else:
        answer = call(method, path, params, body, raw=True)
    try:
        return {"svar": json.loads(answer or b"{}")}
    except ValueError:
        return {"svar": None, "bytes": len(answer),
                "not": "Svaret är inte JSON (t.ex. en PDF). Filer hämtas lättast via las_dokument."}


def t_skrivlogg(a):
    total, rows = fortnox_api.write_log(min(int(a.get("limit", 50)), 500), int(a.get("offset", 0)), a.get("nyckel"))
    return {"antal": total, "skrivningar": rows,
            "not": "Senaste först. Varje anrop som ändrar något i Fortnox, med vilken agentnyckel som gjorde det."}


# --- tool definitions -----------------------------------------------------------------------

S, N, B = {"type": "string"}, {"type": "number"}, {"type": "boolean"}
EXTRA = {"type": "object", "description": "Fler fält med Fortnox egna namn, skickas som de är"}
ROW = {"type": "object", "properties": {
    "konto": {**S, "description": "Kontonummer, t.ex. 1930"}, "debet": N, "kredit": N,
    "beskrivning": {**S, "description": "Radtext (TransactionInformation)"},
    "projekt": S, "kostnadsstalle": S}, "required": ["konto"]}
CREATE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
CHANGE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}

TOOLS = [
    ("skapa_verifikation", t_skapa_verifikation,
     "Bokför en verifikation i Fortnox: datum, beskrivning och minst två rader med konto och debet eller kredit, "
     "som ska balansera. Serie A om inget annat anges. Kontrollera konton med konton/verifikationer först. "
     "Verifikationen syns direkt i verifikationer, konton, resultatrakning och balansrakning. "
     "En verifikation kan inte tas bort: fel rättas med en ny som vänder raderna.",
     {"datum": {**S, "description": "Bokföringsdatum ÅÅÅÅ-MM-DD"}, "beskrivning": S,
      "rader": {"type": "array", "items": ROW}, "serie": {**S, "description": "Verifikationsserie (A)"},
      "kommentar": S, "projekt": S, "kostnadsstalle": S, "extra": EXTRA},
     ["datum", "beskrivning", "rader"], CREATE),
    ("skapa_leverantorsfaktura", t_skapa_leverantorsfaktura,
     "Registrera en leverantörsfaktura i Fortnox. Konteringsrader är valfria (annars leverantörens förval). "
     "Bifoga PDF:en med ladda_upp_fil och bokför med leverantorsfaktura_atgard.",
     {"leverantor": {**S, "description": "Leverantörsnummer i Fortnox"}, "fakturanummer": S,
      "fakturadatum": S, "forfallodatum": S, "total": N, "moms": N, "valuta": S, "ocr": S, "kommentar": S,
      "projekt": S, "kostnadsstalle": S, "rader": {"type": "array", "items": ROW}, "extra": EXTRA},
     ["leverantor"], CREATE),
    ("leverantorsfaktura_atgard", t_leverantorsfaktura_atgard,
     "Gör något med en leverantörsfaktura: bokfor, attestera_bokforing, attestera_betalning, kreditera eller makulera.",
     {"nummer": {**S, "description": "Fakturans löpnummer i Fortnox (GivenNumber, fortnox_nr)"},
      "atgard": {**S, "enum": list(SUPPLIER_ACTIONS)}}, ["nummer", "atgard"], CHANGE),
    ("skapa_kundfaktura", t_skapa_kundfaktura,
     "Skapa en kundfaktura i Fortnox (ej bokförd, ej skickad). Rader med artikel eller beskrivning, antal och pris. "
     "Bokför och skicka sedan med kundfaktura_atgard.",
     {"kund": {**S, "description": "Kundnummer i Fortnox"}, "fakturadatum": S, "forfallodatum": S,
      "rader": {"type": "array", "items": {"type": "object", "properties": {
          "artikel": {**S, "description": "Artikelnummer"}, "beskrivning": S, "antal": N, "pris": N,
          "konto": {**S, "description": "Intäktskonto om det ska avvika från artikelns"}, "moms": N,
          "rabatt": N, "enhet": S, "projekt": S, "kostnadsstalle": S}}},
      "valuta": S, "var_referens": S, "er_referens": S,
      "text_pa_fakturan": {**S, "description": "Text som skrivs ut på fakturan"},
      "kommentar": {**S, "description": "Intern kommentar"}, "projekt": S, "kostnadsstalle": S, "extra": EXTRA},
     ["kund", "rader"], CREATE),
    ("kundfaktura_atgard", t_kundfaktura_atgard,
     "Gör något med en kundfaktura: bokfor, skicka_epost (till kundens e-post i Fortnox), skicka_efaktura, "
     "markera_skickad, kreditera (skapar en kreditfaktura) eller makulera.",
     {"nummer": {**S, "description": "Fakturanummer i Fortnox (DocumentNumber)"},
      "atgard": {**S, "enum": list(INVOICE_ACTIONS)}}, ["nummer", "atgard"], CHANGE),
    ("spara_register", t_spara_register,
     "Skapa eller uppdatera en kund, leverantör, artikel eller ett projekt i Fortnox. falt har Fortnox egna "
     "fältnamn (t.ex. Name, OrganisationNumber, Email, Address1, ZipCode, City). Med nummer uppdateras den som finns.",
     {"typ": {**S, "enum": list(REGISTERS)}, "nummer": {**S, "description": "Finns den redan: dess nummer"},
      "falt": {"type": "object"}}, ["typ", "falt"], CREATE),
    ("ladda_upp_fil", t_ladda_upp_fil,
     "Ladda upp en fil (kvitto, faktura-PDF) till Fortnox och koppla den till en verifikation eller "
     "leverantörsfaktura. Filen: uppladdning (id från POST /upload på den här tjänsten, bäst för PDF:er) "
     "eller innehall_base64 för små filer.",
     {"uppladdning": S, "innehall_base64": S, "namn": {**S, "description": "Filnamn, t.ex. kvitto.pdf"},
      "mapp": {**S, "enum": list(FOLDERS)},
      "koppla_verifikation": {"type": "object", "properties": {"serie": S, "nummer": {"type": "integer"},
                                                               "rakenskapsar_id": {"type": "integer"}}},
      "koppla_leverantorsfaktura": {**S, "description": "Leverantörsfakturans löpnummer"}}, [], CREATE),
    ("registrera_betalning", t_registrera_betalning,
     "Registrera en betalning av en kundfaktura (inbetalning) eller leverantörsfaktura (utbetalning) och bokför "
     "den (bokfor=false låter den vara obokförd).",
     {"typ": {**S, "enum": list(PAYMENTS)}, "nummer": {**S, "description": "Fakturans nummer"},
      "belopp": N, "datum": {**S, "description": "Betaldag ÅÅÅÅ-MM-DD"},
      "betalsatt": {**S, "description": "Betalsätt i Fortnox, t.ex. BG"}, "bokfor": B, "extra": EXTRA},
     ["typ", "nummer", "belopp", "datum"], CREATE),
    ("fortnox_api", t_fortnox_api,
     "Anropa Fortnox API direkt, för allt de andra verktygen inte täcker (se Fortnox API-referens, "
     "https://www.fortnox.se/api). sokvag t.ex. /3/customers, kropp med Fortnox egna fält. "
     "Läsningar loggas inte; allt som ändrar något hamnar i skrivloggen.",
     {"metod": {**S, "enum": ["GET", "POST", "PUT", "DELETE"]}, "sokvag": S,
      "parametrar": {"type": "object"}, "kropp": {"type": "object"}}, ["sokvag"], CHANGE),
    ("skrivlogg", t_skrivlogg,
     "Vad agenterna har skrivit till Fortnox, senaste först: tid, agentnyckel, anrop, vad som skapades och fel.",
     {"limit": {"type": "integer"}, "offset": {"type": "integer"},
      "nyckel": {**S, "description": "Bara en agentnyckels skrivningar"}}, [], {"readOnlyHint": True}),
]
