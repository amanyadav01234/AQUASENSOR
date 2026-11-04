"""Shared fixtures: a real AquaSense server on a random port, backed by a throw-away database."""
import json, os, pathlib, sys, tempfile, threading, urllib.error, urllib.request

os.environ["AQUA_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["AQUA_QUIET"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import pytest, server  # noqa: E402


@pytest.fixture(scope="session")
def base():
    srv = server.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="session")
def call(base):
    def _call(method, path, body=None, token=None, raw=False):
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
        req = urllib.request.Request(base + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                data = r.read()
                return r.status, (data if raw else json.loads(data)), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers
    return _call


@pytest.fixture(scope="session")
def token(call):
    code, d, _ = call("POST", "/api/signup", {"name": "Fixture User", "email": "fixture@example.com", "password": "fixture123"})
    assert code == 201
    return d["token"]
