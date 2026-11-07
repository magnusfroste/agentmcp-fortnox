"""A small Fortnox for the tests: OAuth with PKCE and rotating refresh tokens, and the parts of
API v3 the service uses. Every request is recorded in .calls as (method, path, query, body)."""
import base64
import hashlib
import json
import re
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class Fake:
    def __init__(self):
        self.calls = []
        self.codes = {}            # code -> PKCE challenge
        self.access = set()
        self.refresh = set()
        self.dead_refresh = set()
        self.throttle = 0          # answer 429 this many times
        self.locked_until = "2025-12-31"  # vouchers on or before this date are refused
        self.company = {"CompanyName": "Exempel Bygg AB", "OrganizationNumber": "556677-8899", "DatabaseNumber": 4711}
        self.years = [{"Id": 1, "FromDate": "2025-01-01", "ToDate": "2025-12-31"},
                      {"Id": 2, "FromDate": "2026-01-01", "ToDate": "2026-12-31"}]
        self.accounts = {1930: "Företagskonto", 2440: "Leverantörsskulder", 2640: "Ingående moms",
                         3001: "Försäljning", 5010: "Lokalhyra", 1510: "Kundfordringar", 2611: "Utgående moms"}
        self.opening = {2: {1930: 100000.0, 2440: -100000.0}}
        self.vouchers = {}         # (year id, series, number) -> voucher
        self.supplier_invoices = {}
        self.invoices = {}
        self.payments = {}
        self.files = {}
        self.file_connections = []
        self.customers = {}
        self._n = 100
        self._add_voucher(2, "A", "2026-02-01", "Hyra februari", [(5010, 10000, 0), (1930, 0, 10000)])
        self._add_voucher(2, "A", "2026-02-15", "Försäljning", [(1930, 25000, 0), (3001, 0, 20000), (2611, 0, 5000)])

    def next(self):
        self._n += 1
        return self._n

    def _add_voucher(self, year, series, day, text, rows):
        n = 1 + max((k[2] for k in self.vouchers if k[:2] == (year, series)), default=0)
        v = {"VoucherSeries": series, "VoucherNumber": n, "Year": year, "TransactionDate": day,
             "Description": text, "VoucherRows": [{"Account": a, "Debit": d, "Credit": c} for a, d, c in rows]}
        self.vouchers[(year, series, n)] = v
        return v

    def issue_code(self, challenge):
        code = secrets.token_hex(8)
        self.codes[code] = challenge
        return code

    def expire_access(self):
        self.access.clear()

    def writes(self):
        return [c for c in self.calls if c[0] != "GET" and not c[1].startswith("/oauth")]

    def year_of(self, day):
        return next(y["Id"] for y in self.years if y["FromDate"] <= day <= y["ToDate"])


class Handler(BaseHTTPRequestHandler):
    fake = None

    def log_message(self, *_):
        pass

    def answer(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, message, ecode=2000000):
        return self.answer(code, {"ErrorInformation": {"error": 1, "message": message, "code": ecode}})

    def go(self, method):
        f = self.fake
        path, _, query = self.path.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        ctype = self.headers.get("Content-Type", "")
        body = json.loads(raw) if raw and ctype.startswith("application/json") else None
        f.calls.append((method, path, q, body if body is not None else (raw if raw else None)))
        if path == "/oauth-v1/token":
            return self.token(urllib.parse.parse_qs(raw.decode()))
        if f.throttle:
            f.throttle -= 1
            return self.error(429, "Too many requests")
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in f.access:
            return self.error(401, "Unauthorized", 2000311)
        return self.api(method, path, q, body, raw, ctype)

    def token(self, form):
        f = self.fake
        form = {k: v[0] for k, v in form.items()}
        if form.get("grant_type") == "authorization_code":
            challenge = f.codes.pop(form.get("code"), None)
            verifier = form.get("code_verifier", "")
            calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            if not challenge or calc != challenge:
                return self.answer(400, {"error": "invalid_grant"})
        elif form.get("grant_type") == "refresh_token":
            old = form.get("refresh_token")
            if old not in f.refresh:
                return self.answer(400, {"error": "invalid_grant"})
            f.refresh.discard(old)
            f.dead_refresh.add(old)
        else:
            return self.answer(400, {"error": "unsupported_grant_type"})
        access, refresh = secrets.token_hex(16), secrets.token_hex(16)
        f.access.add(access)
        f.refresh.add(refresh)
        return self.answer(200, {"access_token": access, "refresh_token": refresh, "expires_in": 3600,
                                 "token_type": "bearer", "scope": "x"})

    def pages(self, key, items, q):
        limit, page = int(q.get("limit", 100)), int(q.get("page", 1))
        total = max(1, -(-len(items) // limit))
        return self.answer(200, {"MetaInformation": {"@TotalResources": len(items), "@TotalPages": total,
                                                     "@CurrentPage": page},
                                 key: items[(page - 1) * limit:page * limit]})

    def api(self, method, path, q, body, raw, ctype):
        f = self.fake
        if method == "GET" and path == "/3/companyinformation":
            return self.answer(200, {"CompanyInformation": f.company})
        if method == "GET" and path == "/3/financialyears":
            return self.answer(200, {"FinancialYears": f.years})
        if method == "GET" and path == "/3/accounts":
            year = int(q["financialyear"])
            return self.pages("Accounts", [{"Number": n, "Description": d,
                                            "BalanceBroughtForward": f.opening.get(year, {}).get(n, 0)}
                                           for n, d in f.accounts.items()], q)
        if method == "GET" and path == "/3/vouchers":
            year = int(q["financialyear"])
            items = [{k: v[k] for k in ("VoucherSeries", "VoucherNumber", "Year", "TransactionDate")}
                     for (y, _, _), v in sorted(f.vouchers.items()) if y == year
                     and v["TransactionDate"] >= q.get("fromdate", "")]
            return self.pages("Vouchers", items, q)
        m = re.fullmatch(r"/3/vouchers/(\w+)/(\d+)", path)
        if method == "GET" and m:
            v = f.vouchers.get((int(q.get("financialyear", 0)), m.group(1), int(m.group(2))))
            return self.answer(200, {"Voucher": v}) if v else self.error(404, "Kan inte hitta verifikationen.")
        if method == "POST" and path == "/3/vouchers":
            v = body["Voucher"]
            if v["TransactionDate"] <= f.locked_until:
                return self.error(400, "Bokföringsdatumet ligger i en låst period.", 2000716)
            rows = [(r["Account"], r.get("Debit", 0), r.get("Credit", 0)) for r in v["VoucherRows"]]
            if round(sum(r[1] for r in rows), 2) != round(sum(r[2] for r in rows), 2):
                return self.error(400, "Verifikationen balanserar inte.")
            new = f._add_voucher(f.year_of(v["TransactionDate"]), v["VoucherSeries"], v["TransactionDate"],
                                 v["Description"], rows)
            return self.answer(201, {"Voucher": new})
        if method == "GET" and path == "/3/voucherfileconnections":
            return self.pages("VoucherFileConnections", [], q)
        if method == "POST" and path == "/3/voucherfileconnections":
            f.file_connections.append(("voucher", body["VoucherFileConnection"]))
            return self.answer(201, {"VoucherFileConnection": body["VoucherFileConnection"]})
        if method == "GET" and path == "/3/supplierinvoices":
            return self.pages("SupplierInvoices", list(f.supplier_invoices.values()), q)
        if method == "POST" and path == "/3/supplierinvoices":
            n = f.next()
            inv = {**body["SupplierInvoice"], "GivenNumber": n, "Booked": False, "SupplierName": "Hyresvärden AB"}
            f.supplier_invoices[n] = inv
            return self.answer(201, {"SupplierInvoice": inv})
        m = re.fullmatch(r"/3/supplierinvoices/(\d+)/(bookkeep|cancel|credit|approvalbookkeep|approvalpayment)", path)
        if method == "PUT" and m:
            inv = f.supplier_invoices[int(m.group(1))]
            inv["Booked"] = inv["Booked"] or m.group(2) == "bookkeep"
            return self.answer(200, {"SupplierInvoice": inv})
        if method == "GET" and path == "/3/supplierinvoicefileconnections":
            return self.answer(200, {"SupplierInvoiceFileConnections": []})
        if method == "POST" and path == "/3/supplierinvoicefileconnections":
            f.file_connections.append(("supplierinvoice", body["SupplierInvoiceFileConnection"]))
            return self.answer(201, {"SupplierInvoiceFileConnection": body["SupplierInvoiceFileConnection"]})
        if method == "GET" and path == "/3/invoices":
            return self.pages("Invoices", list(f.invoices.values()), q)
        if method == "POST" and path == "/3/invoices":
            n = f.next()
            total = sum(r.get("DeliveredQuantity", 1) * r.get("Price", 0) for r in body["Invoice"]["InvoiceRows"])
            inv = {**body["Invoice"], "DocumentNumber": n, "Total": total * 1.25, "TotalVAT": total * 0.25,
                   "CustomerName": "Kund AB", "Booked": False, "Sent": False, "Cancelled": False, "Currency": "SEK"}
            f.invoices[n] = inv
            return self.answer(201, {"Invoice": inv})
        m = re.fullmatch(r"/3/invoices/(\d+)/(\w+)", path)
        if m and int(m.group(1)) in f.invoices:
            inv = f.invoices[int(m.group(1))]
            if method == "GET" and m.group(2) == "preview":
                return self.answer(200, raw=PDF, ctype="application/pdf")
            if method == "PUT" and m.group(2) == "bookkeep":
                inv["Booked"] = True
            elif method == "GET" and m.group(2) == "email":
                inv["Sent"] = True
            else:
                return self.error(400, "Okänd åtgärd")
            return self.answer(200, {"Invoice": inv})
        if method == "POST" and path in ("/3/invoicepayments", "/3/supplierinvoicepayments"):
            key = "InvoicePayment" if path == "/3/invoicepayments" else "SupplierInvoicePayment"
            n = f.next()
            p = {**body[key], "Number": n, "Booked": False}
            f.payments[(path, n)] = p
            return self.answer(201, {key: p})
        m = re.fullmatch(r"(/3/(?:supplier)?invoicepayments)/(\d+)/bookkeep", path)
        if method == "PUT" and m:
            p = f.payments[(m.group(1), int(m.group(2)))]
            p["Booked"] = True
            return self.answer(200, {"InvoicePayment" if "supplier" not in m.group(1) else "SupplierInvoicePayment": p})
        if method == "POST" and path in ("/3/inbox", "/3/archive"):
            m = re.search(rb'filename="([^"]+)"', raw)
            fid = secrets.token_hex(8)
            f.files[fid] = {"name": m.group(1).decode() if m else None, "folder": q.get("path"),
                            "multipart": ctype.startswith("multipart/form-data"), "size": len(raw)}
            return self.answer(201, {"File": {"Id": fid, "Name": f.files[fid]["name"]}})
        m = re.fullmatch(r"/3/archive/(\w+)", path)
        if method == "GET" and m:
            return self.answer(200, raw=PDF, ctype="application/pdf")
        if method == "POST" and path == "/3/customers":
            n = str(f.next())
            f.customers[n] = {**body["Customer"], "CustomerNumber": n}
            return self.answer(201, {"Customer": f.customers[n]})
        if method == "GET" and path == "/3/customers":
            return self.pages("Customers", list(f.customers.values()), q)
        return self.error(404, f"Ingen sådan resurs: {method} {path}")

    def do_GET(self):
        self.go("GET")

    def do_POST(self):
        self.go("POST")

    def do_PUT(self):
        self.go("PUT")

    def do_DELETE(self):
        self.go("DELETE")


def start():
    fake = Fake()
    handler = type("H", (Handler,), {"fake": fake})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return fake, f"http://127.0.0.1:{server.server_address[1]}"
