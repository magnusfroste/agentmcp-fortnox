"""The service against the fake Fortnox, in one process. The environment is set before the modules
are imported, since they read it at import."""
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
START = "s" * 40
os.environ.update({
    "DATA_DIR": DATA, "FORTNOX_API_BASE": FAKE_URL, "FORTNOX_AUTH_BASE": FAKE_URL,
    "FORTNOX_CLIENT_ID": "client", "FORTNOX_CLIENT_SECRET": "secret",
    "FORTNOX_REDIRECT_URI": "https://fortnox.example.se/oauth/callback", "FORTNOX_SYNC_FROM": "2025-01-01",
    "MCP_KEYS": f"hermes:{KEY},bokforaren:{OTHER_KEY}", "OAUTH_START_TOKEN": START,
    "PUBLIC_URL": "https://fortnox.example.se", "LINK_SECRET": "l" * 64, "WRITE_SYNC_SECONDS": "0",
})
for k in ("FORTNOX_READ_ONLY", "STATE_DIR", "FORTNOX_SCOPES", "INDEX_REFERENCES", "INDEX_WORDS", "REPORT_LINK_BASE"):
    os.environ.pop(k, None)

import fortnox_api  # noqa: E402
import mcp_server  # noqa: E402

open(mcp_server.MANIFEST, "a").close()
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


def ok(name, **args):
    result, is_error, text = tool(name, **args)
    assert not is_error, text
    return result


def sign_in():
    status, headers, _ = http("GET", f"/oauth/start/{START}", key=None)
    assert status == 302
    loc = urllib.parse.urlparse(headers["Location"])
    q = {k: v[0] for k, v in urllib.parse.parse_qs(loc.query).items()}
    assert loc.path == "/oauth-v1/auth" and q["code_challenge_method"] == "S256" and q["account_type"] == "service"
    assert q["redirect_uri"] == "https://fortnox.example.se/oauth/callback"
    code = FAKE.issue_code(q["code_challenge"])
    status, _, body = http("GET", f"/oauth/callback?code={code}&state={q['state']}", key=None)
    return status, body


def wait_synced(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if fortnox_api.status().get("senaste_synk") and not mcp_server._sync_lock.locked():
            return
        time.sleep(0.1)
    raise AssertionError("the first sync did not finish")


@pytest.fixture(scope="session")
def signed_in():
    status, body = sign_in()
    assert status == 200, body
    wait_synced()
    return FAKE
