"""The service against the fake Fortnox, in one process. The environment is set before the modules
are imported, since they read it at import.

One service, five keys: hermes and bokforaren are written the old way (name:key) and reach the
default company, exactly as in a single-company deployment. The other three name a company each:
alfa and beta are signed in to two more companies of the fake, gamma never signs in."""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_fortnox  # noqa: E402

FAKE, FAKE_URL = fake_fortnox.start()
DATA = tempfile.mkdtemp(prefix="fortnox-data-")
KEY = "k" * 40
OTHER_KEY = "o" * 40
ALFA_KEY, BETA_KEY, GAMMA_KEY = "a" * 40, "b" * 40, "g" * 40
START = "s" * 40
REDIRECT = "https://fortnox.example.se/oauth/callback"
ALFA_AB = fake_fortnox.Company("Alfa Optik AB", "559900-1111", 5001, vouchers=[
    ("2026-02-03", "Alfa linser", [(5010, 700, 0), (1930, 0, 700)])], invoices=[("2026-02-10", "Alfakund AB", 1250.0)])
BETA_AB = fake_fortnox.Company("Beta Gård AB", "559900-2222", 5002, vouchers=[
    ("2026-02-04", "Beta utsäde", [(5010, 900, 0), (1930, 0, 900)])], invoices=[("2026-02-11", "Betakund AB", 2500.0)])
os.environ.update({
    "DATA_DIR": DATA, "FORTNOX_API_BASE": FAKE_URL, "FORTNOX_AUTH_BASE": FAKE_URL,
    "FORTNOX_CLIENT_ID": "client", "FORTNOX_CLIENT_SECRET": "secret",
    "FORTNOX_REDIRECT_URI": REDIRECT, "FORTNOX_SYNC_FROM": "2025-01-01",
    "MCP_KEYS": f"hermes:{KEY},bokforaren:{OTHER_KEY},"
                f"alfa-agent:{ALFA_KEY}:alfa,beta-agent:{BETA_KEY}:beta,gamma-agent:{GAMMA_KEY}:gamma",
    "OAUTH_START_TOKEN": START,
    "PUBLIC_URL": "https://fortnox.example.se", "LINK_SECRET": "l" * 64, "WRITE_SYNC_SECONDS": "0",
})
for k in ("FORTNOX_READ_ONLY", "STATE_DIR", "FORTNOX_SCOPES", "INDEX_REFERENCES", "INDEX_WORDS", "REPORT_LINK_BASE"):
    os.environ.pop(k, None)

import fortnox_api  # noqa: E402
import mcp_server  # noqa: E402

mcp_server.prepare()
MAIN, ALFA, BETA, GAMMA = (fortnox_api.SLOTS[c] for c in ("default", "alfa", "beta", "gamma"))
_server = ThreadingHTTPServer(("127.0.0.1", 0), mcp_server.Handler)
threading.Thread(target=_server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{_server.server_address[1]}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(method, path, body=None, key=KEY, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", **(headers or {})}
    if key:
        h["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


_ids = iter(range(1, 10 ** 6))


def tool(name, key=KEY, **args):
    """Call a tool over MCP; returns (result, is_error, text)."""
    status, _, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": next(_ids), "method": "tools/call",
                                            "params": {"name": name, "arguments": args}}, key=key)
    assert status == 200, body
    res = json.loads(body)["result"]
    return res.get("structuredContent"), res["isError"], res["content"][0]["text"]


def ok(name, key=KEY, **args):
    result, is_error, text = tool(name, key=key, **args)
    assert not is_error, text
    return result


def start_sign_in(company=None):
    """Open the start link (for a named company: /oauth/start/<token>/<company>); returns what the
    service sends the browser to Fortnox with."""
    status, headers, _ = http("GET", f"/oauth/start/{START}" + (f"/{company}" if company else ""), key=None)
    assert status == 302
    loc = urllib.parse.urlparse(headers["Location"])
    q = {k: v[0] for k, v in urllib.parse.parse_qs(loc.query).items()}
    assert loc.path == "/oauth-v1/auth" and q["code_challenge_method"] == "S256" and q["account_type"] == "service"
    assert q["redirect_uri"] == REDIRECT  # the one registered redirect, whichever company
    return q


def sign_in(company=None, approved_by=None):
    """A sign-in from start link to callback. approved_by: the company in Fortnox whose system
    administrator approves (the fake's main company when not given)."""
    q = start_sign_in(company)
    code = FAKE.issue_code(q["code_challenge"], approved_by)
    status, _, body = http("GET", f"/oauth/callback?code={code}&state={q['state']}", key=None)
    return status, body


def status_of(company):
    with fortnox_api.use(company):
        return fortnox_api.status()


def wait_synced(company=MAIN, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if status_of(company).get("senaste_synk") and not company.sync_lock.locked():
            return
        time.sleep(0.1)
    raise AssertionError(f"the first sync of {company.slug} did not finish")


@pytest.fixture(scope="session")
def signed_in():
    status, body = sign_in()
    assert status == 200, body
    wait_synced()
    return FAKE


@pytest.fixture(scope="session")
def two_more(signed_in):
    """Alfa and Beta signed in next to the default company, through the same redirect URI."""
    for company, books in ((ALFA, ALFA_AB), (BETA, BETA_AB)):
        status, body = sign_in(company.slug, books)
        assert status == 200, body
        wait_synced(company)
    return FAKE
