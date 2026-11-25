"""2.2.0: who knocks is logged, wrong keys are braked, a company can be read-only on its own, what an
agent creates is marked in Fortnox, a financial year can be the period, and a period that starts
before the synced bookkeeping says so."""
import json
import os
import shutil

import fortnox_api
import mcp_server
import fake_fortnox
from conftest import (ALFA, ALFA_AB, ALFA_KEY, BETA, BETA_AB, BETA_KEY, DATA, FAKE, KEY, START, callback, http, ok,
                      start_sign_in, status_of, tool, wait_synced)

VOUCHER = {"datum": "2026-03-12", "rader": [{"konto": "5010", "debet": 120}, {"konto": "1930", "kredit": 120}]}


def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(mcp_server, "log", lambda msg: lines.append(msg))
    return lines


def tool_names(key):
    _, _, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key=key)
    return {t["name"] for t in json.loads(body)["result"]["tools"]}


def test_wrong_keys_are_logged_and_braked_but_a_right_key_always_passes(signed_in, monkeypatch):
    lines = logged(monkeypatch)
    monkeypatch.setattr(mcp_server, "FAIL_LIMIT", 3)
    monkeypatch.setattr(mcp_server.time, "sleep", lambda s: None)
    monkeypatch.setattr(mcp_server, "_fails", {})
    knock = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert [http("POST", "/mcp", ping, key="x" * 40, headers=knock)[0] for _ in range(4)] == [401, 401, 401, 429]
    assert http("POST", "/mcp", ping, key=KEY, headers=knock)[0] == 200  # agents behind the same address
    assert http("POST", "/mcp", ping, key="x" * 40, headers={"CF-Connecting-IP": "198.51.100.7"})[0] == 401
    assert any("refused: wrong key on /mcp from 203.0.113.9 (1 in 10 min" in l for l in lines)
    assert any("from 203.0.113.9 (4 in 10 min, now answered 429" in l for l in lines)
    assert any("from 198.51.100.7 (1 in" in l for l in lines)
    assert http("GET", "/health/" + "w" * 40, key=None, headers=knock)[0] == 429
    assert any("operator token on /health from 203.0.113.9" in l for l in lines)
    assert not any("x" * 40 in l or KEY in l for l in lines)  # what was tried is never written down


def test_sign_ins_are_logged_from_start_to_how_they_ended(signed_in, monkeypatch):
    lines = logged(monkeypatch)
    monkeypatch.setattr(mcp_server, "_fails", {})
    q = start_sign_in("alfa")
    assert any(l.startswith("sign-in started for alfa (operator link) from 127.0.0.1") for l in lines)
    status, _, _ = http("GET", "/oauth/callback?error=access_denied&error_description=Resource+owner+does+not+have"
                               f"+admin+right.&state={q['state']}", key=None)
    assert status == 400
    assert any("sign-in for alfa stopped by Fortnox: access_denied: Resource owner does not have admin right."
               in l for l in lines)
    callback(q, ALFA_AB, cookie=None)  # Fortnox' page forwarded to another browser
    assert any(l.startswith("sign-in for alfa failed:") and "without the browser's cookie" in l for l in lines)
    assert http("GET", f"/oauth/start/{'c' * 40}/alfa", key=None)[0] == 404
    assert any("refused: sign-in link for 'alfa'" in l for l in lines)
    assert not any(START in l for l in lines)


def test_one_company_read_only_while_the_next_writes(two_more, monkeypatch):
    monkeypatch.setattr(fortnox_api, "READ_ONLY_COMPANIES", {"alfa"})
    alfa, beta = tool_names(ALFA_KEY), tool_names(BETA_KEY)
    assert {"fortnox_api", "skrivlogg", "verifikationer"} <= alfa and not alfa & mcp_server.WRITE_ONLY
    assert mcp_server.WRITE_ONLY <= beta
    _, _, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, key=ALFA_KEY)
    assert "skrivskyddat" in json.loads(body)["result"]["instructions"]
    before = len(ALFA_AB.writes())
    _, is_error, text = tool("skapa_verifikation", key=ALFA_KEY, beskrivning="Får inte", **VOUCHER)
    assert is_error and "skrivskyddat" in text
    _, is_error, text = tool("fortnox_api", key=ALFA_KEY, metod="POST", sokvag="/3/vouchers", kropp={"Voucher": {}})
    assert is_error and "skrivskyddat" in text  # the general tool too: only its GETs reach Fortnox
    assert ok("fortnox_api", key=ALFA_KEY, sokvag="/3/companyinformation")["svar"]
    assert http("POST", "/upload?namn=a.pdf", raw=b"%PDF", key=ALFA_KEY)[0] == 403
    assert len(ALFA_AB.writes()) == before
    assert ok("kalla_status", key=ALFA_KEY)["skrivskyddat"] and not ok("kalla_status", key=BETA_KEY)["skrivskyddat"]
    assert ok("skapa_verifikation", key=BETA_KEY, beskrivning="Beta skriver", **VOUCHER)["skapad"]


def test_what_an_agent_creates_says_who_asked_in_fortnox(two_more):
    ok("skapa_verifikation", key=BETA_KEY, beskrivning="Märkt", kommentar="Kvitto 12", **VOUCHER)
    sent = [c for c in BETA_AB.writes() if c[1] == "/3/vouchers"][-1][3]["Voucher"]
    assert sent["Comments"] == "Kvitto 12 – Via agent, på uppdrag av beta-agent"
    ok("skapa_verifikation", key=BETA_KEY, beskrivning="Utan kommentar", **VOUCHER)
    assert [c for c in BETA_AB.writes() if c[1] == "/3/vouchers"][-1][3]["Voucher"]["Comments"] == \
        "Via agent, på uppdrag av beta-agent"
    ok("skapa_leverantorsfaktura", key=BETA_KEY, leverantor="1", total=100)
    assert [c for c in BETA_AB.writes() if c[1] == "/3/supplierinvoices"][-1][3]["SupplierInvoice"]["Comments"] == \
        "Via agent, på uppdrag av beta-agent"


def test_a_financial_year_is_a_period_and_a_short_period_says_so(signed_in, monkeypatch):
    by_year = ok("resultatrakning", rakenskapsar="2026")
    by_dates = ok("resultatrakning", fran="2026-01-01", till="2026-12-31")
    assert by_year["resultat"] == by_dates["resultat"] and by_year["period"]["fran"] == "2026-01-01"
    assert ok("verifikationer", rakenskapsar="2026")["antal"] == ok("verifikationer", fran="2026-01-01",
                                                                     till="2026-12-31")["antal"]
    _, is_error, text = tool("resultatrakning", rakenskapsar="2019")
    assert is_error and "2025" in text and "2026" in text  # the years there are
    assert "varning" not in by_year
    # The company's first year began 16 Dec 2024 but the books were synced from 1 Jan 2025.
    monkeypatch.setattr(fortnox_api, "synced_from", lambda: ("2025-01-01", "2024-12-16"))
    short = ok("resultatrakning", fran="2024-12-16", till="2025-12-31")
    assert "2024-12-16–2024-12-31 saknas" in short["varning"] and "FORTNOX_SYNC_FROM=2024-12-16" in short["varning"]
    assert "varning" in ok("verifikationer", fran="2024-01-01", konto="5")
    assert "2024-12-16–2024-12-31" in ok("konton", fran="2024-01-01", till="2025-03-31")["varning"]
    assert "varning" not in ok("resultatrakning", fran="2025-01-01", till="2025-12-31")
    monkeypatch.setattr(fortnox_api, "synced_from", lambda: ("2025-01-01", "2025-01-01"))
    assert "varning" not in ok("resultatrakning", fran="2024-01-01", till="2025-12-31")  # nothing older exists


def test_the_sync_starts_where_the_financial_year_does(monkeypatch):
    broken = [{"Id": 1, "FromDate": "2024-12-16", "ToDate": "2025-12-31"},
              {"Id": 2, "FromDate": "2026-01-01", "ToDate": "2026-12-31"}]
    monkeypatch.setattr(fortnox_api, "SYNC_FROM", "2025-01-01")
    monkeypatch.setattr(fortnox_api, "SYNC_FROM_SET", False)
    assert fortnox_api.sync_start(broken) == "2024-12-16"
    assert fortnox_api.sync_start(FAKE.main.years) == "2025-01-01"
    assert fortnox_api.sync_start([]) == "2025-01-01"
    monkeypatch.setattr(fortnox_api, "SYNC_FROM_SET", True)  # set by hand: taken as it is
    assert fortnox_api.sync_start(broken) == "2025-01-01"


def test_a_first_year_from_december_is_synced_whole(two_more, monkeypatch):
    monkeypatch.setattr(fortnox_api, "SYNC_FROM", "2025-01-01")
    monkeypatch.setattr(fortnox_api, "SYNC_FROM_SET", False)
    delta_ab = fake_fortnox.Company("Delta Tunnel AB", "559900-4444", 5004)
    delta_ab.years = [{"Id": 1, "FromDate": "2024-12-16", "ToDate": "2025-12-31"},
                      {"Id": 2, "FromDate": "2026-01-01", "ToDate": "2026-12-31"}]
    delta_ab._add_voucher(1, "A", "2024-12-20", "Startkapital", [(1930, 25000, 0), (2081, 0, 25000)])
    delta_ab._add_voucher(1, "A", "2025-03-01", "Mars", [(5010, 500, 0), (1930, 0, 500)])
    delta = fortnox_api.Slot("delta")
    monkeypatch.setitem(fortnox_api.SLOTS, "delta", delta)
    try:
        status, body = callback(start_sign_in("delta"), delta_ab)
        assert status == 200, body
        wait_synced(delta)
        st = status_of(delta)
        assert st["synkar_fran"] == "2024-12-16"
        with fortnox_api.use(delta):
            days = {v["datum"] for v in mcp_server.books()["vouchers"]}
            assert {"2024-12-20", "2025-03-01"} <= days
            assert "varning" not in mcp_server.t_verifikationer({"rakenskapsar": "2024/25"})
    finally:
        shutil.rmtree(os.path.join(DATA, "companies", "delta"), ignore_errors=True)


def test_a_year_inside_one_calendar_year_is_named_by_it():
    fy = lambda i, a, b: {"Id": i, "FromDate": a, "ToDate": b}
    assert fortnox_api.fy_label(fy(1, "2026-01-01", "2026-12-31")) == "2026"
    assert fortnox_api.fy_label(fy(1, "2025-03-01", "2025-12-31")) == "2025"  # was "2025/25"
    assert fortnox_api.fy_label(fy(1, "2024-12-16", "2025-12-31")) == "2024/25"
    assert fortnox_api.fy_label(fy(1, "2025-07-01", "2026-06-30")) == "2025/26"
    changed = [fy(1, "2025-01-01", "2025-04-30"), fy(2, "2025-05-01", "2025-12-31"), fy(3, "2026-01-01", "2026-12-31")]
    assert [fortnox_api.fy_label(f, changed) for f in changed] == ["2025 (01–04)", "2025 (05–12)", "2026"]


def test_books_stored_under_the_old_name_move_without_a_second_copy(two_more, monkeypatch):
    monkeypatch.setattr(fortnox_api, "SYNC_FROM", "2025-01-01")
    monkeypatch.setattr(fortnox_api, "SYNC_FROM_SET", True)
    eps = fake_fortnox.Company("Epsilon Nät AB", "559900-5555", 5005)
    eps.years = [{"Id": 1, "FromDate": "2025-03-01", "ToDate": "2025-12-31"},
                 {"Id": 2, "FromDate": "2026-01-01", "ToDate": "2026-12-31"}]
    eps._add_voucher(1, "A", "2025-03-05", "Första", [(5010, 300, 0), (1930, 0, 300)])
    eps._add_voucher(2, "A", "2026-02-02", "Andra", [(5010, 400, 0), (1930, 0, 400)])
    slot = fortnox_api.Slot("epsilon")
    monkeypatch.setitem(fortnox_api.SLOTS, "epsilon", slot)
    root = os.path.join(DATA, "companies", "epsilon")
    try:
        # synced by 2.2.0, which named the first year "2025/25"
        monkeypatch.setattr(fortnox_api, "fy_label", lambda f, fys=(): fortnox_api._old_label(f))
        status, body = callback(start_sign_in("epsilon"), eps)
        assert status == 200, body
        wait_synced(slot)
        with fortnox_api.use(slot):
            assert {v["id"] for v in mcp_server.books()["vouchers"]} == {"2025/25:A1", "2026:A1"}
        side = fortnox_api._path(root, "Verifikationer", "2025-03-05", "A1", "f1", "kvitto.pdf")  # where the sync puts it
        fortnox_api._write(side, b"%PDF-1.4\n", {"verifikation": "2025/25:A1", "date": "2025-03-05"})
        with open(slot.synced) as f:
            state = json.load(f)
        state["done"] = state.get("done", []) + ["vo:2025/25:A1:f1"]
        fortnox_api._save(slot.synced, state)
        monkeypatch.undo()  # this version
        monkeypatch.setitem(fortnox_api.SLOTS, "epsilon", slot)
        mcp_server.sync_once(slot)
        with fortnox_api.use(slot):
            b = mcp_server.books()
            assert {v["id"] for v in b["vouchers"]} == {"2025:A1", "2026:A1"}  # moved, not fetched twice
            assert {y["rakenskapsar"] for y in b["years"]} == {"2025", "2026"} and set(b["accounts"]) == {"2025", "2026"}
            assert fortnox_api.status()["senaste_synk_resultat"]["nya_namn_pa_rakenskapsar"] == {"2025/25": "2025"}
            assert ok_in(slot, "verifikationer", rakenskapsar="2025")["antal"] == 1
        assert "vo:2025:A1:f1" in fortnox_api._load(slot.synced, {})["done"]
        assert fortnox_api._load(side + ".json", {})["verifikation"] == "2025:A1"
        mcp_server.sync_once(slot)  # and the next sync finds nothing to move
        with fortnox_api.use(slot):
            assert "nya_namn_pa_rakenskapsar" not in fortnox_api.status()["senaste_synk_resultat"]
            assert len(mcp_server.books()["vouchers"]) == 2
    finally:
        shutil.rmtree(root, ignore_errors=True)


def ok_in(slot, name, **args):
    return mcp_server.BY_NAME[name](args)
