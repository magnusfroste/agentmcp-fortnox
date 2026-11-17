"""A deployment from before companies (2.0.0): tokens in DATA_DIR/.state, the books and documents
straight in DATA_DIR, keys written name:key. The service is started on such a folder as its own
process, the way a redeploy does it: no configuration change, no new sign-in, nothing moved. Then a
second company is added next to it."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

import fake_fortnox
import index
from conftest import FAKE, FAKE_URL, KEY, START, NoRedirect, started

OLD_AB = fake_fortnox.Company("Gamla Bolaget AB", "556100-0001", 6001, vouchers=[
    ("2026-02-05", "Gammal hyra", [(5010, 800, 0), (1930, 0, 800)])], invoices=[("2026-02-20", "Gammal Kund AB", 100.0)])
NEW_AB = fake_fortnox.Company("Nya Bolaget AB", "559900-3333", 7001, vouchers=[
    ("2026-02-07", "Nytt bolag hyra", [(5010, 400, 0), (1930, 0, 400)])], invoices=[("2026-02-12", "Nykund AB", 500.0)])
NEW_KEY = "n" * 40
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(obj, bytes) else "w") as f:
        f.write(obj if isinstance(obj, (bytes, str)) else json.dumps(obj, ensure_ascii=False))


def folder_from_2_0_0(data):
    """What 2.0.0 leaves on disk after a sign-in, a sync and one write (file names and fields as it
    writes them), with an access token that has run out since."""
    FAKE.refresh["refresh-from-2.0.0"] = OLD_AB
    write(f"{data}/.state/token.json", {
        "access_token": "access-from-2.0.0", "refresh_token": "refresh-from-2.0.0", "expires_in": 3600,
        "token_type": "bearer", "scope": "x", "expires_at": 0, "refreshed_at": "2026-03-01T06:00:00+00:00",
        "company": "Gamla Bolaget AB", "database_number": 6001, "organisation_number": "556100-0001",
        "own_vat": "SE556100000101"})
    nr = next(iter(OLD_AB.invoices))
    pdf = f"fortnox-api/2026/02/Kundfakturor/{nr}_preview_faktura_{nr}.pdf"
    write(f"{data}/.state/sync.json", {
        "done": [], "invoices": {f"ci:{nr}": {"fp": "0" * 16, "content": "0" * 16, "path": pdf}},
        "books_since": "2026-03-01", "last_run": "2026-03-01T06:00:00+00:00",
        "last_result": {"rakenskapsar": ["2025", "2026"], "bokforing": {"nya": 1, "andrade": 0, "totalt": 1}}})
    write(f"{data}/.state/writes.jsonl", json.dumps({
        "tid": "2026-03-01T07:00:00+00:00", "nyckel": "hermes", "metod": "POST", "sokvag": "/3/customers",
        "parametrar": None, "status": 201, "skapat": {"Customer": {"CustomerNumber": "7"}}, "fel": None}) + "\n")
    books = f"{data}/fortnox-api/bokforing"
    write(f"{books}/verifikationer.json", {"2026:A1": {
        "id": "2026:A1", "rakenskapsar": "2026", "serie": "A", "nummer": 1, "datum": "2026-02-05",
        "beskrivning": "Gammal hyra", "kommentar": None, "referenstyp": None, "referensnummer": None,
        "projekt": None, "kostnadsstalle": None, "rader": [
            {"konto": 5010, "debet": 800, "kredit": 0, "beskrivning": None, "info": None, "projekt": None,
             "kostnadsstalle": None, "antal": None, "borttagen": False},
            {"konto": 1930, "debet": 0, "kredit": 800, "beskrivning": None, "info": None, "projekt": None,
             "kostnadsstalle": None, "antal": None, "borttagen": False}]}})
    write(f"{books}/konton.json", {"2026": {"1930": "Företagskonto", "5010": "Lokalhyra"}})
    write(f"{books}/ingaende.json", {"2026": {}})
    write(f"{books}/rakenskapsar.json", [{"rakenskapsar": "2026", "fran": "2026-01-01", "till": "2026-12-31", "id": 2}])
    write(f"{data}/{pdf}", OLD_AB.pdf)
    write(f"{data}/{pdf}.json", {"date": "2026-02-20", "counterpart_name": "Gammal Kund AB", "amount": 100.0,
                                  "currency": "SEK", "fortnox_nr": str(nr), "fortnox": OLD_AB.invoices[nr]})
    index.build(data, own={"own_vat": "SE556100000101", "company": "Gamla Bolaget AB"})  # manifest.jsonl and .text
    return tree(data)


def tree(data):
    return sorted(os.path.relpath(os.path.join(d, f), data) for d, _, files in os.walk(data) for f in files)


class Service:
    """mcp_server.py as its own process on a data folder, as a deploy runs it."""

    def __init__(self, data, keys, **more):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.base, self.data = f"http://127.0.0.1:{port}", data
        env = {**os.environ, "DATA_DIR": str(data), "PORT": str(port), "MCP_KEYS": keys,
               "FORTNOX_API_BASE": FAKE_URL, "FORTNOX_AUTH_BASE": FAKE_URL, "PYTHONDONTWRITEBYTECODE": "1", **more}
        self.out = open(os.path.join(data, "..", f"service-{port}.log"), "w+")
        self.process = subprocess.Popen([sys.executable, "-u", os.path.join(APP, "mcp_server.py")], env=env,
                                        stdout=self.out, stderr=subprocess.STDOUT)
        self.wait(lambda: self.http("GET", "/health", key=None)[0] == 200, "start")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.process.terminate()
        self.process.wait(10)
        self.out.close()

    def log(self):
        self.out.flush()
        self.out.seek(0)
        return self.out.read()

    def wait(self, check, what, seconds=30):
        end = time.time() + seconds
        while time.time() < end:
            try:
                if check():
                    return
            except OSError:
                pass
            time.sleep(0.1)
        raise AssertionError(f"waited {seconds} s for {what}:\n{self.log()}")

    def http(self, method, path, body=None, key=KEY, cookie=None):
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {}),
                   **({"Cookie": cookie} if cookie else {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def tool(self, name, key=KEY, **args):
        _, _, body = self.http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                "params": {"name": name, "arguments": args}}, key=key)
        result = json.loads(body)["result"]
        assert not result["isError"], result["content"][0]["text"]
        return result["structuredContent"]

    def overview(self):
        return {c["company"]: c for c in json.loads(self.http("GET", f"/health/{START}", key=None)[2])["companies"]}

    def synced_since(self, company, before):
        return lambda: (self.overview()[company]["last_sync"] or "") > before


@pytest.fixture(scope="module")
def old_folder(tmp_path_factory):
    data = tmp_path_factory.mktemp("from-2.0.0") / "data"
    return str(data), folder_from_2_0_0(str(data))


def test_a_single_company_deployment_starts_as_it_was(old_folder):
    data, files = old_folder
    grants = len([c for c in FAKE.calls if c[1] == "/oauth-v1/token"])
    with Service(data, f"hermes:{KEY}") as s:  # the key as 2.0.0 had it: no company part
        s.wait(s.synced_since("default", "2026-03-01T06:00:00+00:00"), "the first sync")
        st = s.tool("kalla_status")
        assert st["inloggat"] and st["bolag"] == "Gamla Bolaget AB" and st["organisationsnummer"] == "556100-0001"
        assert [v["beskrivning"] for v in s.tool("verifikationer")["verifikationer"]] == ["Gammal hyra"]
        assert s.tool("skrivlogg")["skrivningar"][0]["sokvag"] == "/3/customers"  # the log it had
        docs = s.tool("lista_dokument")["dokument"]
        assert [d["motpart"] for d in docs] == ["Gammal Kund AB"]
        link = urllib.parse.urlparse(s.tool("las_dokument", id=docs[0]["id"])["pdf_lank"]).path
        assert s.http("GET", link, key=None)[2] == OLD_AB.pdf
        out = s.tool("skapa_verifikation", datum="2026-03-02", beskrivning="Efter uppgraderingen",
                     rader=[{"konto": "5010", "debet": 20}, {"konto": "1930", "kredit": 20}])
        assert out["skapad"] and s.tool("skrivlogg")["antal"] == 2
        banner = s.log()
    # No sign-in happened: the token from 2.0.0 was refreshed where it lay, and nothing moved.
    new = [c[3] for c in FAKE.calls if c[1] == "/oauth-v1/token"][grants:]
    assert new and all(b"grant_type=refresh_token" in g for g in new)
    with open(os.path.join(data, ".state", "token.json")) as f:
        token = json.load(f)
    assert token["refresh_token"] != "refresh-from-2.0.0" and "refresh-from-2.0.0" in FAKE.dead_refresh
    assert token["database_number"] == 6001 and token["own_vat"] == "SE556100000101"
    assert tree(data) == files and not os.path.exists(os.path.join(data, "companies"))
    assert "One company's Fortnox for agents" in banner
    assert "default: signed in to Gamla Bolaget AB (556100-0001)" in banner
    assert not [x for x in (KEY, START, "refresh-from-2.0.0", token["refresh_token"], token["access_token"]) if x in banner]


def test_a_second_company_is_added_next_to_it(old_folder):
    data, files = old_folder
    keys = f"hermes:{KEY},ny:{NEW_KEY}:nytt"
    with Service(data, keys) as s:
        view = s.overview()
        assert view["default"]["signed_in"] and view["default"]["name"] == "Gamla Bolaget AB"
        assert not view["nytt"]["signed_in"] and view["nytt"]["sign_in"] == "/oauth/start/<OAUTH_START_TOKEN>/nytt"
        q = started(s.http("GET", f"/oauth/start/{START}/nytt", key=None))
        code = FAKE.issue_code(q["code_challenge"], NEW_AB)
        assert s.http("GET", f"/oauth/callback?code={code}&state={q['state']}", key=None, cookie=q["cookie"])[0] == 200
        s.wait(lambda: s.overview()["nytt"]["documents"] == 1, "the new company's first sync")
        assert s.tool("kalla_status", key=NEW_KEY)["bolag"] == "Nya Bolaget AB"
        assert [v["beskrivning"] for v in s.tool("verifikationer", key=NEW_KEY)["verifikationer"]] == ["Nytt bolag hyra"]
        assert "Nytt bolag hyra" not in [v["beskrivning"] for v in s.tool("verifikationer")["verifikationer"]]
        assert s.tool("skrivlogg", key=NEW_KEY)["antal"] == 0
    with Service(data, keys) as s:  # a restart indexes every folder again: the old company's is still only its own
        s.wait(lambda: "index default:" in s.log() and "index nytt:" in s.log(), "the index at start")
        assert [d["motpart"] for d in s.tool("lista_dokument")["dokument"]] == ["Gammal Kund AB"]
        assert [d["motpart"] for d in s.tool("lista_dokument", key=NEW_KEY)["dokument"]] == ["Nykund AB"]
        assert "Fortnox for agents, 2 companies" in s.log()
    with open(os.path.join(data, "manifest.jsonl")) as f:
        assert all(json.loads(l)["file"].startswith("fortnox-api/") for l in f if l.strip())
    added = sorted(set(tree(data)) - set(files))
    assert added and all(a.startswith("companies/nytt/") for a in added)
    assert os.path.exists(os.path.join(data, "companies", "nytt", ".state", "token.json"))
    with Service(data, f"hermes:{KEY}") as s:  # its key taken away again: kept on disk, served to nobody
        assert sorted(s.overview()) == ["default"]
        assert json.loads(s.http("GET", f"/health/{START}", key=None)[2])["not_served"] == ["nytt"]
        assert s.http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key=NEW_KEY)[0] == 401
        assert s.http("GET", f"/oauth/start/{START}/nytt", key=None)[0] == 404
        s.wait(lambda: "index default:" in s.log(), "the index at start")
        assert [d["motpart"] for d in s.tool("lista_dokument")["dokument"]] == ["Gammal Kund AB"]
        assert "unused" in s.log() and "nytt" in s.log()


def test_a_state_folder_of_ones_own_choosing_holds_only_the_default_companys(tmp_path):
    """STATE_DIR moves the default company's state, as in 2.0.0, and may well be a plain folder in
    DATA_DIR that its index walks. A named company's state stays in its own folder, out of that walk."""
    data = tmp_path / "data"
    data.mkdir()
    env = {"STATE_DIR": str(data / "state")}
    keys = f"hermes:{KEY},ny:{NEW_KEY}:nytt"
    with Service(str(data), keys, **env) as s:
        status, _, body = s.http("POST", "/upload?namn=kvitto-nytt.pdf", {"kvitto": "nytt bolag"}, key=NEW_KEY)
        assert status == 200
        assert s.http("POST", "/upload?namn=kvitto-eget.pdf", {"kvitto": "eget"})[0] == 200
    uploads = data / "companies" / "nytt" / ".state" / "uploads"
    assert [f.name.split("__")[1] for f in uploads.iterdir()] == ["kvitto-nytt.pdf"]
    assert [f.name.split("__")[1] for f in (data / "state" / "uploads").iterdir()] == ["kvitto-eget.pdf"]
    assert not (data / "state" / "companies").exists()
    with Service(str(data), keys, **env) as s:  # the start indexes each company's folder
        s.wait(lambda: "index default:" in s.log() and "index nytt:" in s.log(), "the index at start")
        mine = json.dumps(s.tool("exportera_index")["rader"])
        assert "kvitto-nytt" not in mine and "companies" not in mine
        assert s.tool("sammanfattning", key=NEW_KEY)["dokument"] == 0
