import base64
import json
import os

import fortnox_api
import mcp_server
from conftest import FAKE, KEY, OTHER_KEY, http, ok, sign_in, tool


def test_without_a_key_nothing_answers():
    assert http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key=None)[0] == 401
    assert http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="x" * 40)[0] == 401
    assert http("POST", "/upload?namn=a.pdf", raw=b"%PDF", key=None)[0] == 401
    assert http("GET", "/oauth/start/" + "x" * 40, key=None)[0] == 404


def test_sign_in_names_the_company_and_the_first_sync_only_reads(signed_in):
    st = ok("kalla_status")
    assert st["bolag"] == "Exempel Bygg AB" and st["organisationsnummer"] == "556677-8899"
    assert st["verifikationer_med_rader"] == 2
    assert fortnox_api.company()["own_vat"] == "SE556677889901"
    sync_calls = [c for c in FAKE.calls if not c[1].startswith("/oauth")]
    assert sync_calls and all(c[0] == "GET" for c in sync_calls)


def test_instructions_and_tools_name_no_other_company(signed_in):
    _, _, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    init = json.loads(body)["result"]
    assert "Exempel Bygg AB" in init["instructions"]
    _, _, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = json.loads(body)["result"]["tools"]
    text = json.dumps(tools, ensure_ascii=False) + init["instructions"]
    assert "Resize" not in text and "WO" not in text
    names = {t["name"] for t in tools}
    assert {"skapa_verifikation", "skapa_kundfaktura", "fortnox_api", "skrivlogg", "resultatrakning"} <= names
    hints = {t["name"]: t["annotations"] for t in tools}
    assert hints["resultatrakning"]["readOnlyHint"] and not hints["skapa_verifikation"]["readOnlyHint"]


def test_a_second_company_cannot_take_over(signed_in):
    FAKE.company = {**FAKE.company, "CompanyName": "Annat AB", "DatabaseNumber": 9999}
    try:
        status, body = sign_in()
        assert status == 400 and "redan kopplad" in body.decode()
        assert fortnox_api.company()["company"] == "Exempel Bygg AB"
    finally:
        FAKE.company = {**FAKE.company, "CompanyName": "Exempel Bygg AB", "DatabaseNumber": 4711}


def test_the_books_add_up(signed_in):
    r = ok("resultatrakning", fran="2026-01-01", till="2026-12-31")
    result = {row["rad"]: row["totalt"] for row in r["resultat"]}
    assert result["Årets resultat"] == 10000.0  # 20000 sales - 10000 rent
    b = ok("balansrakning", datum="2026-12-31")
    assert b["kontroll"] == 0


def test_a_voucher_shows_at_once_and_is_logged_with_the_key(signed_in):
    out = ok("skapa_verifikation", datum="2026-03-01", beskrivning="Hyra mars",
             rader=[{"konto": "5010", "debet": 12000}, {"konto": "1930", "kredit": 12000}])
    assert out["skapad"] and out["serie"] == "A" and out["verifikation"]["id"] == f"2026:A{out['nummer']}"
    hits = ok("verifikationer", text="Hyra mars")
    assert hits["antal"] == 1
    r = ok("resultatrakning", fran="2026-03-01", till="2026-03-31")
    assert {row["rad"]: row["totalt"] for row in r["resultat"]}["Årets resultat"] == -12000.0
    log = ok("skrivlogg")["skrivningar"][0]
    assert log["nyckel"] == "hermes" and log["metod"] == "POST" and log["sokvag"] == "/3/vouchers"
    assert log["skapat"]["Voucher"]["VoucherNumber"] == out["nummer"] and log["fel"] is None
    st = os.stat(fortnox_api.WRITES)
    assert st.st_mode & 0o077 == 0


def test_an_unbalanced_voucher_never_reaches_fortnox(signed_in):
    before = len(FAKE.writes())
    _, is_error, text = tool("skapa_verifikation", datum="2026-03-02", beskrivning="Fel",
                             rader=[{"konto": "5010", "debet": 100}, {"konto": "1930", "kredit": 90}])
    assert is_error and "balanserar inte" in text
    _, is_error, text = tool("skapa_verifikation", datum="2026-03-02", beskrivning="Fel",
                             rader=[{"konto": "5010", "debet": 100, "kredit": 100}, {"konto": "1930", "kredit": 0}])
    assert is_error and "antingen debet eller kredit" in text
    assert len(FAKE.writes()) == before


def test_fortnox_own_refusal_reaches_the_agent_and_the_log(signed_in):
    _, is_error, text = tool("skapa_verifikation", key=OTHER_KEY, datum="2025-06-01", beskrivning="Gammal",
                             rader=[{"konto": "5010", "debet": 1}, {"konto": "1930", "kredit": 1}])
    assert is_error and "låst period" in text and "2000716" in text
    log = ok("skrivlogg", nyckel="bokforaren")["skrivningar"][0]
    assert log["status"] == 400 and "låst period" in log["fel"]


def test_supplier_invoice_with_its_pdf_uploaded_outside_the_tool_call(signed_in):
    inv = ok("skapa_leverantorsfaktura", leverantor="12", fakturanummer="F-881", fakturadatum="2026-03-05",
             forfallodatum="2026-04-04", total=12500, moms=2500,
             rader=[{"konto": "5010", "debet": 10000}, {"konto": "2640", "debet": 2500},
                    {"konto": "2440", "kredit": 12500}])
    status, _, body = http("POST", "/upload?namn=faktura-881.pdf", raw=b"%PDF-1.4 fake invoice",
                           headers={"Content-Type": "application/pdf"})
    assert status == 200
    uid = json.loads(body)["uppladdning"]
    f = ok("ladda_upp_fil", uppladdning=uid, koppla_leverantorsfaktura=str(inv["nummer"]))
    stored = FAKE.files[f["fil_id"]]
    assert stored["name"] == "faktura-881.pdf" and stored["folder"] == "Inbox_s" and stored["multipart"]
    assert ("supplierinvoice", {"FileId": f["fil_id"], "SupplierInvoiceNumber": str(inv["nummer"])}) in FAKE.file_connections
    booked = ok("leverantorsfaktura_atgard", nummer=str(inv["nummer"]), atgard="bokfor")
    assert booked["bokford"] is True


def test_a_small_file_can_come_base64_and_go_on_a_voucher(signed_in):
    v = ok("skapa_verifikation", datum="2026-03-06", beskrivning="Kvitto",
           rader=[{"konto": "5010", "debet": 50}, {"konto": "1930", "kredit": 50}])
    f = ok("ladda_upp_fil", innehall_base64=base64.b64encode(b"receipt").decode(), namn="kvitto.jpg",
           koppla_verifikation={"serie": "A", "nummer": v["nummer"], "rakenskapsar_id": v["rakenskapsar_id"]})
    assert FAKE.files[f["fil_id"]]["folder"] == "Inbox_v"
    assert f["kopplad_till"] == f"verifikation A{v['nummer']}"


def test_customer_invoice_created_booked_and_e_mailed_all_logged(signed_in):
    c = ok("spara_register", typ="kund", falt={"Name": "Kund AB", "Email": "ekonomi@kund.example"})
    inv = ok("skapa_kundfaktura", kund=c["nummer"], rader=[{"beskrivning": "Konsulttimmar", "antal": 10, "pris": 1000}])
    assert inv["skapad"] and inv["total"] == 12500
    assert ok("kundfaktura_atgard", nummer=str(inv["nummer"]), atgard="bokfor")["bokford"] is True
    assert ok("kundfaktura_atgard", nummer=str(inv["nummer"]), atgard="skicka_epost")["skickad"] is True
    log = [(r["metod"], r["sokvag"]) for r in ok("skrivlogg", limit=5)["skrivningar"]]
    assert ("GET", f"/3/invoices/{inv['nummer']}/email") in log  # sending is a GET in Fortnox, still a write


def test_a_payment_is_registered_and_booked(signed_in):
    inv = ok("skapa_kundfaktura", kund="1", rader=[{"beskrivning": "x", "antal": 1, "pris": 800}])
    p = ok("registrera_betalning", typ="kundfaktura", nummer=str(inv["nummer"]), belopp=1000, datum="2026-03-20")
    assert p["registrerad"] and p["bokford"] is True


def test_the_general_tool_reads_without_logging_and_refuses_odd_paths(signed_in):
    before = ok("skrivlogg")["antal"]
    out = ok("fortnox_api", sokvag="/3/financialyears")
    assert out["svar"]["FinancialYears"][0]["Id"] == 1
    assert ok("skrivlogg")["antal"] == before
    for bad in ("https://evil.example/3/x", "/3/../oauth-v1/token", "3/customers", "/oauth-v1/token"):
        _, is_error, _ = tool("fortnox_api", sokvag=bad)
        assert is_error, bad
    _, is_error, text = tool("fortnox_api", sokvag="/3/nothing")
    assert is_error and "Ingen sådan resurs" in text


def test_an_expired_token_is_refreshed_and_the_old_refresh_token_is_dead(signed_in):
    old = fortnox_api._load(fortnox_api.TOKEN, {})
    fortnox_api._save(fortnox_api.TOKEN, {**old, "expires_at": 0})
    FAKE.expire_access()
    ok("fortnox_api", sokvag="/3/companyinformation")
    new = fortnox_api._load(fortnox_api.TOKEN, {})
    assert new["refresh_token"] != old["refresh_token"] and old["refresh_token"] in FAKE.dead_refresh
    assert new["company"] == "Exempel Bygg AB" and new["own_vat"] == "SE556677889901"
    assert os.stat(fortnox_api.TOKEN).st_mode & 0o077 == 0


def test_rate_limit_answers_are_waited_out(signed_in, monkeypatch):
    monkeypatch.setattr(fortnox_api.time, "sleep", lambda s: None)
    FAKE.throttle = 2
    assert ok("fortnox_api", sokvag="/3/financialyears")["svar"]["FinancialYears"]


def test_read_only_refuses_every_write_before_fortnox(signed_in, monkeypatch):
    monkeypatch.setattr(fortnox_api, "READ_ONLY", True)
    before = len(FAKE.writes())
    _, is_error, text = tool("skapa_verifikation", datum="2026-03-01", beskrivning="x",
                             rader=[{"konto": "5010", "debet": 1}, {"konto": "1930", "kredit": 1}])
    assert is_error and "skrivskyddad" in text
    _, is_error, _ = tool("kundfaktura_atgard", nummer="101", atgard="skicka_epost")  # an acting GET too
    assert is_error
    assert ok("fortnox_api", sokvag="/3/financialyears")["svar"]
    assert len(FAKE.writes()) == before
    assert http("POST", "/upload?namn=a.pdf", raw=b"%PDF")[0] == 403


def test_a_later_sync_keeps_what_was_written_and_still_only_reads(signed_in):
    n = len(FAKE.calls)
    mcp_server.sync_once()
    assert all(c[0] == "GET" for c in FAKE.calls[n:] if not c[1].startswith("/oauth"))
    assert ok("verifikationer", text="Hyra mars")["antal"] == 1
    assert ok("kalla_status")["senaste_synk_resultat"]["bokforing"]["totalt"] >= 4


def test_health_and_unknown_routes():
    assert http("GET", "/health", key=None)[0] == 200
    assert http("GET", "/mcp", key=None)[0] == 405
    assert http("GET", "/nothing", key=None)[0] == 404
