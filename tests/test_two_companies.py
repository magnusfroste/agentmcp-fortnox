"""Several companies in one service: one integration, one redirect URI, and every key bound to
exactly one company. Alfa and Beta are signed in next to the default company (two_more); Gamma has a
key but never signs in."""
import base64
import json
import os
import urllib.parse

import fortnox_api
import mcp_server
import fake_fortnox
from conftest import (ALFA, ALFA_AB, ALFA_KEY, BETA, BETA_AB, BETA_KEY, DATA, FAKE, GAMMA, GAMMA_KEY, KEY, MAIN,
                      OTHER_KEY, REDIRECT, START, callback, http, ok, sign_in, start_sign_in, started, status_of, tool)

VOUCHER = {"datum": "2026-03-10", "rader": [{"konto": "5010", "debet": 300}, {"konto": "1930", "kredit": 300}]}


def texts(key, **filters):
    return {v["beskrivning"] for v in ok("verifikationer", key=key, **filters)["verifikationer"]}


def test_every_company_signs_in_through_the_same_redirect(two_more):
    q = start_sign_in("alfa")
    assert q["redirect_uri"] == REDIRECT and q["state"].split(".")[0] == "alfa"  # the company travels in the state
    assert start_sign_in()["state"].split(".")[0] == "default"
    for company, name, org in ((MAIN, "Exempel Bygg AB", "556677-8899"), (ALFA, "Alfa Optik AB", "559900-1111"),
                               (BETA, "Beta Gård AB", "559900-2222")):
        st = status_of(company)
        assert st["inloggat"] and st["bolag"] == name and st["organisationsnummer"] == org
        assert os.stat(company.token).st_mode & 0o077 == 0
    assert ALFA.token == os.path.join(DATA, "companies", "alfa", ".state", "token.json")
    assert BETA.manifest == os.path.join(DATA, "companies", "beta", "manifest.jsonl")
    tokens = {fortnox_api._load(c.token, {})["refresh_token"] for c in (MAIN, ALFA, BETA)}
    assert len(tokens) == 3  # Fortnox gave each company its own


def test_each_key_sees_only_its_own_books(two_more):
    assert texts(ALFA_KEY) == {"Alfa linser"}
    assert texts(BETA_KEY) == {"Beta utsäde"}
    mine = texts(KEY, limit=500)
    assert "Hyra februari" in mine and not mine & {"Alfa linser", "Beta utsäde"}
    assert ok("verifikationer", key=ALFA_KEY, text="Beta")["antal"] == 0
    for key, name in ((KEY, "Exempel Bygg AB"), (ALFA_KEY, "Alfa Optik AB"), (BETA_KEY, "Beta Gård AB")):
        assert ok("kalla_status", key=key)["bolag"] == name
        _, _, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, key=key)
        said = json.loads(body)["result"]["instructions"]
        assert name in said and sum(n in said for n in ("Exempel Bygg", "Alfa Optik", "Beta Gård")) == 1
        assert ok("fortnox_api", key=key, sokvag="/3/companyinformation")["svar"]["CompanyInformation"]["CompanyName"] == name
    r = ok("resultatrakning", key=ALFA_KEY, fran="2026-01-01", till="2026-02-28")
    assert {row["rad"]: row["totalt"] for row in r["resultat"]}["Årets resultat"] == -700.0


def test_a_write_lands_in_the_keys_company_and_nowhere_else(two_more):
    before = {c: len(c.writes()) for c in (FAKE.main, ALFA_AB, BETA_AB)}
    out = ok("skapa_verifikation", key=ALFA_KEY, beskrivning="Alfa skriver", **VOUCHER)
    assert out["skapad"]
    assert len(ALFA_AB.writes()) == before[ALFA_AB] + 1
    assert len(BETA_AB.writes()) == before[BETA_AB] and len(FAKE.main.writes()) == before[FAKE.main]
    assert "Alfa skriver" in texts(ALFA_KEY)
    assert "Alfa skriver" not in texts(BETA_KEY) | texts(KEY, limit=500)
    log = ok("skrivlogg", key=ALFA_KEY)
    assert log["antal"] == 1 and log["skrivningar"][0]["nyckel"] == "alfa-agent"
    assert ok("skrivlogg", key=BETA_KEY)["antal"] == 0  # each company has its own log
    assert all(r["nyckel"] in ("hermes", "bokforaren") for r in ok("skrivlogg", key=KEY, limit=500)["skrivningar"])
    assert ok("skrivlogg", key=KEY, nyckel="alfa-agent")["antal"] == 0
    assert os.path.exists(ALFA.writes) and not os.path.exists(BETA.writes)
    assert os.stat(ALFA.writes).st_mode & 0o077 == 0


def test_no_argument_reaches_another_company(two_more):
    for extra in ({"company": "beta"}, {"foretag": "beta"}, {"bolag": "Beta Gård AB"}, {"slug": "beta"}):
        assert not texts(ALFA_KEY, **extra) & {"Beta utsäde", "Hyra februari"}
    answer = ok("fortnox_api", key=ALFA_KEY, sokvag="/3/companyinformation", parametrar={"company": "beta"})
    assert answer["svar"]["CompanyInformation"]["CompanyName"] == "Alfa Optik AB"
    for path in ("/3/../companies/beta", "/3/companyinformation?company=beta"):
        assert tool("fortnox_api", key=ALFA_KEY, sokvag=path)[1]


def test_documents_and_pdf_links_stay_in_their_company(two_more):
    a = ok("lista_dokument", key=ALFA_KEY)["dokument"]
    b = ok("lista_dokument", key=BETA_KEY)["dokument"]
    assert [d["motpart"] for d in a] == ["Alfakund AB"] and [d["motpart"] for d in b] == ["Betakund AB"]
    aid, bid = a[0]["id"], b[0]["id"]
    assert aid != bid and not {aid, bid} & {d["id"] for d in ok("lista_dokument", key=KEY, limit=200)["dokument"]}
    for key, other in ((BETA_KEY, aid), (ALFA_KEY, bid), (KEY, aid), (GAMMA_KEY, aid)):
        _, is_error, text = tool("las_dokument", key=key, id=other)
        assert is_error and "Inget dokument" in text
    assert all("Alfakund" not in json.dumps(r) for r in ok("exportera_index", key=BETA_KEY)["rader"])
    assert ok("motparter", key=BETA_KEY)["antal_motparter"] == 1

    link = urllib.parse.urlparse(ok("las_dokument", key=ALFA_KEY, id=aid)["pdf_lank"]).path
    status, _, pdf = http("GET", link, key=None)  # opened without a key: the link itself says whose it is
    assert status == 200 and pdf == ALFA_AB.pdf and pdf != BETA_AB.pdf
    body, mac = link[len("/pdf/"):].split(".")
    slug, sha, exp = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode().split("|")
    assert slug == "alfa"
    for forged in (f"beta|{sha}|{exp}", f"default|{sha}|{exp}", f"{sha}|{exp}"):  # the last: a link from 2.0.0
        moved = base64.urlsafe_b64encode(forged.encode()).decode().rstrip("=")
        assert http("GET", f"/pdf/{moved}.{mac}", key=None)[0] == 403


def test_an_upload_is_only_there_for_the_company_that_sent_it(two_more):
    status, _, body = http("POST", "/upload?namn=alfa-kvitto.pdf", raw=b"%PDF-1.4 alfa", key=ALFA_KEY)
    assert status == 200
    uid = json.loads(body)["uppladdning"]
    assert [f for f in os.listdir(ALFA.uploads) if f.startswith(uid)] and not os.path.isdir(BETA.uploads)
    for key in (BETA_KEY, KEY):
        _, is_error, text = tool("ladda_upp_fil", key=key, uppladdning=uid)
        assert is_error and "finns inte" in text
    assert not BETA_AB.files
    f = ok("ladda_upp_fil", key=ALFA_KEY, uppladdning=uid)
    assert ALFA_AB.files[f["fil_id"]]["name"] == "alfa-kvitto.pdf" and f["fil_id"] not in FAKE.main.files


def test_one_fortnox_company_goes_in_one_place_only(two_more):
    status, body = sign_in("beta", ALFA_AB)  # beta's place is Beta Gård's
    assert status == 400 and "redan kopplad till Beta Gård AB" in body.decode()
    status, body = sign_in("gamma", ALFA_AB)  # Alfa Optik is already here, as alfa
    assert status == 400 and "redan kopplat till företagsplatsen alfa i" in body.decode()
    status, body = sign_in("gamma", FAKE.main)  # and the default company's is taken too
    assert status == 400 and "redan kopplat till tjänsten" in body.decode()
    assert not os.path.exists(GAMMA.token)
    assert status_of(BETA)["bolag"] == "Beta Gård AB" and status_of(ALFA)["bolag"] == "Alfa Optik AB"
    assert sign_in("alfa", ALFA_AB)[0] == 200  # the same company again is a normal re-login


def test_a_sign_in_cannot_be_moved_or_replayed(two_more):
    q = start_sign_in("gamma")
    slug, nonce, mac = q["state"].split(".")
    for forged in (f"alfa.{nonce}.{mac}", f"gamma.{nonce}.{'0' * len(mac)}", f"gamma.{nonce}", nonce, ""):
        status, body = callback(q, ALFA_AB, state=forged)
        assert status == 400 and "Okänd eller för gammal" in body, forged
    q = start_sign_in("alfa")
    assert callback(q, ALFA_AB)[0] == 200
    assert callback(q, ALFA_AB)[0] == 400  # a state works once
    assert not os.path.exists(GAMMA.token)


def test_the_approval_must_come_from_the_browser_that_opened_the_address(two_more):
    """Whoever has the address of an empty place could otherwise send the Fortnox link on to another
    company's administrator, and get that company's books in their own place."""
    victim = fake_fortnox.Company("Offer AB", "559900-4444", 5004, vouchers=[
        ("2026-02-08", "Offrets hyra", [(5010, 1, 0), (1930, 0, 1)])])
    q = start_sign_in("gamma")           # gamma's holder opens the address and passes the Fortnox link on
    other = start_sign_in("gamma")       # (and has a cookie of their own, from another start)
    for cookie in (None, other["cookie"], q["cookie"].split("=")[0] + "=guess"):
        status, body = callback(q, victim, cookie=cookie)  # the victim approves in their own browser
        assert status == 400 and "samma webbläsare" in body
    assert not os.path.exists(GAMMA.token)
    assert ok("kalla_status", key=GAMMA_KEY)["inloggat"] is False
    assert not [c for c in victim.calls]  # nothing was fetched from the victim's Fortnox
    q = start_sign_in("alfa")            # the right browser is not locked out by someone else's attempt
    assert callback(q, ALFA_AB, cookie=None)[0] == 400
    assert callback(q, ALFA_AB)[0] == 200


def test_a_started_sign_in_runs_out(two_more):
    q = start_sign_in("gamma")
    nonce = q["state"].split(".")[1]
    verifier, created, slug, browser = fortnox_api._pending[nonce]
    fortnox_api._pending[nonce] = (verifier, created - fortnox_api.PENDING_SECONDS - 1, slug, browser)
    status, body = callback(q, ALFA_AB)
    assert status == 400 and "för gammal" in body and nonce not in fortnox_api._pending


def test_a_place_cannot_change_company_by_dropping_its_token(two_more):
    """What the refusal tells the operator to do for a named company: the whole folder, not the token."""
    status, body = sign_in("beta", ALFA_AB)
    assert status == 400 and f"ta bort hela mappen {BETA.root}" in body.decode()
    unknown = fake_fortnox.Company("Okänt AB", "", None)  # Fortnox says nothing to tell it by: not let in
    status, body = sign_in("beta", unknown)
    assert status == 400 and "redan kopplad till Beta Gård AB" in body.decode()


def test_a_companys_own_sign_in_address_opens_nothing_else(two_more):
    """The address its administrator is sent: good for that company, not for the others or the overview."""
    view = {c["company"]: c for c in json.loads(http("GET", f"/health/{START}", key=None)[2])["companies"]}
    link = urllib.parse.urlparse(view["alfa"]["sign_in_link"])
    assert f"{link.scheme}://{link.netloc}" == "https://fortnox.example.se" and START not in link.path
    code = link.path.split("/")[3]
    assert link.path == f"/oauth/start/{code}/alfa" and len(code) == 40
    q = started(http("GET", link.path, key=None))
    assert q["state"].split(".")[0] == "alfa" and q["redirect_uri"] == REDIRECT
    assert callback(q, ALFA_AB)[0] == 200
    for path in (f"/oauth/start/{code}/beta", f"/oauth/start/{code}/gamma", f"/oauth/start/{code}",
                 f"/oauth/start/{code}/default", f"/oauth/start/{code}/nosuch", f"/health/{code}"):
        assert http("GET", path, key=None)[0] == 404, path
    assert len({c["sign_in_link"] for c in view.values()}) == 4
    default = urllib.parse.urlparse(view["default"]["sign_in_link"]).path
    assert default.endswith("/default") and http("GET", default, key=None)[0] == 302


def test_only_a_company_some_key_reaches_can_be_signed_in():
    status, _, body = http("GET", f"/oauth/start/{START}/alpha", key=None)  # a typo makes no new company
    assert status == 404 and "alfa" in body.decode()
    assert not os.path.exists(os.path.join(DATA, "companies", "alpha"))
    for path in (f"/oauth/start/{'x' * 40}/alfa", "/oauth/start/alfa", f"/oauth/start/{START}/Alfa",
                 f"/oauth/start/{START}/alfa/x", f"/oauth/start/{START}/..%2Falfa"):
        assert http("GET", path, key=None)[0] == 404, path


def test_a_company_that_has_not_signed_in_says_so_and_stops_nobody(two_more):
    st = ok("kalla_status", key=GAMMA_KEY)
    assert st["inloggat"] is False and st["bolag"] is None and st["verifikationer_med_rader"] == 0
    assert ok("sammanfattning", key=GAMMA_KEY)["dokument"] == 0
    _, is_error, text = tool("fortnox_api", key=GAMMA_KEY, sokvag="/3/companyinformation")
    assert is_error and "/oauth/start/<OAUTH_START_TOKEN>/gamma" in text
    _, is_error, text = tool("verifikationer", key=GAMMA_KEY)
    assert is_error and "inte synkad" in text
    assert texts(ALFA_KEY) and texts(BETA_KEY)


def test_one_companys_sync_failing_leaves_the_others(two_more, capsys):
    FAKE.revoke(BETA_AB)  # Beta Gård withdrew the integration in Fortnox
    fortnox_api._save(BETA.token, {**fortnox_api._load(BETA.token, {}), "expires_at": 0})
    before = status_of(ALFA)["senaste_synk"], status_of(BETA)["senaste_synk"]
    try:
        mcp_server.sync_once(BETA)
        assert "fortnox sync beta skipped" in capsys.readouterr().out
        mcp_server.sync_once(ALFA)
        mcp_server.sync_once(MAIN)
        assert isinstance(status_of(ALFA)["senaste_synk_resultat"]["bokforing"], dict)
        assert status_of(ALFA)["senaste_synk"] >= before[0] and status_of(BETA)["senaste_synk"] == before[1]
        assert texts(BETA_KEY) == {"Beta utsäde"}  # what was synced can still be read
        _, is_error, text = tool("skapa_verifikation", key=BETA_KEY, beskrivning="Beta skriver", **VOUCHER)
        assert is_error and "Logga in igen" in text
        assert ok("skapa_verifikation", key=ALFA_KEY, beskrivning="Alfa skriver igen", **VOUCHER)["skapad"]
        assert ok("skapa_verifikation", key=OTHER_KEY, beskrivning="Bygg skriver", **VOUCHER)["skapad"]
    finally:
        assert sign_in("beta", BETA_AB)[0] == 200  # approved again: back in its own place
    assert ok("fortnox_api", key=BETA_KEY, sokvag="/3/financialyears")["svar"]["FinancialYears"]


def test_the_default_company_never_indexes_the_others_folders(two_more):
    mcp_server.sync_once(MAIN)
    with fortnox_api.use(MAIN):
        mcp_server.reindex()
    for company in (MAIN, ALFA, BETA):
        with open(company.manifest) as f:
            files = [json.loads(l)["file"] for l in f if l.strip()]
        assert files and all(x.startswith("fortnox-api/") for x in files), company.slug
    assert os.path.isdir(os.path.join(DATA, "companies", "alfa", "fortnox-api"))


def test_nothing_runs_without_a_company_bound():
    for fn in (fortnox_api.status, fortnox_api.company, fortnox_api.sync, mcp_server.store, mcp_server.books,
               lambda: fortnox_api.request("GET", "/3/companyinformation"), lambda: fortnox_api.write_log()):
        try:
            fn()
        except RuntimeError as e:
            assert "no company is bound" in str(e)
        else:
            raise AssertionError("ran without a company")


def test_the_operator_sees_every_company_and_no_secrets(two_more):
    assert http("GET", "/health", key=None)[2] == b'{"ok":true}'  # the public answer says nothing more than before
    assert http("GET", "/health/" + "x" * 40, key=None)[0] == 404
    assert http("GET", "/health/" + ALFA_KEY, key=None)[0] == 404  # an agent key is not the operator's
    status, _, body = http("GET", f"/health/{START}", key=None)
    assert status == 200
    view = json.loads(body)
    by = {c["company"]: c for c in view["companies"]}
    assert sorted(by) == ["alfa", "beta", "default", "gamma"] and view["version"] == mcp_server.SERVER_VERSION
    assert by["alfa"]["signed_in"] and by["alfa"]["name"] == "Alfa Optik AB" and by["alfa"]["documents"] == 1
    assert by["alfa"]["organisation_number"] == "559900-1111" and by["alfa"]["keys"] == ["alfa-agent"]
    assert by["default"]["name"] == "Exempel Bygg AB" and by["default"]["keys"] == ["bokforaren", "hermes"]
    assert by["gamma"] == {**by["gamma"], "signed_in": False, "name": None, "documents": 0,
                           "sign_in": "/oauth/start/<OAUTH_START_TOKEN>/gamma"}
    assert by["default"]["sign_in"] == "/oauth/start/<OAUTH_START_TOKEN>"
    secrets_ = [KEY, OTHER_KEY, ALFA_KEY, BETA_KEY, GAMMA_KEY, START, os.environ["LINK_SECRET"]]
    for company in (MAIN, ALFA, BETA):
        t = fortnox_api._load(company.token, {})
        secrets_ += [t["access_token"], t["refresh_token"]]
    assert not [s for s in secrets_ if s in body.decode()]


def test_a_key_entry_is_bound_as_written_or_not_at_all(monkeypatch, capsys):
    k = [c * 40 for c in "1234567890"]
    monkeypatch.setenv("MCP_KEYS", ",".join([
        f"old:{k[0]}", f" named : {k[1]} : alfa ", f"upper:{k[2]}:Beta", f"default-by-name:{k[3]}:default",
        f"bad-company:{k[4]}:not a slug", f"too-many:{k[5]}:a:b", f"short:{'x' * 31}:alfa", f"empty-company:{k[6]}:",
        f"twice:{k[7]}:alfa", f"twice-again:{k[7]}:beta", f"same-place:{k[8]}:alfa", f"same-place-2:{k[8]}:alfa"]))
    keys = mcp_server.load_keys()
    assert keys == {k[0]: ("old", "default"), k[1]: ("named", "alfa"), k[2]: ("upper", "beta"),
                    k[3]: ("default-by-name", "default"), k[8]: ("same-place-2", "alfa")}
    out = capsys.readouterr().out
    for name in ("bad-company", "too-many", "short", "empty-company", "twice-again"):
        assert f"key for {name!r} ignored" in out
    assert "and for 'twice'" in out
    assert not [x for x in k if x in out] and "not a slug" not in out  # nothing of an entry's value is logged
