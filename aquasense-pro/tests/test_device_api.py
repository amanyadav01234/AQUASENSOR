"""Automated API tests (pytest). All HTTP is mocked via FakeSession; no network needed.
Run:  pip install pytest && pytest -v
"""
from datetime import datetime, timezone
import pytest
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import device_client as dc

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


class FakeResp:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code, self._body, self._bad = status, body, bad_json
    def json(self):
        if self._bad: raise ValueError("not json")
        return self._body


class FakeSession:
    """Returns a canned response and records the last request."""
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.last = resp, exc, None
    def _do(self, method, url, **kw):
        self.last = (method, url, kw)
        if self.exc: raise self.exc
        return self.resp
    def post(self, url, **kw): return self._do("POST", url, **kw)
    def get(self, url, **kw): return self._do("GET", url, **kw)


def ok_status(**over):
    body = {"level_percent": 55, "pump_state": "off", "last_seen": "2026-11-04T11:58:00Z"}
    body.update(over)
    return FakeResp(200, body)


# TC-01 positive: login returns token
def test_login_success():
    s = FakeSession(FakeResp(200, {"token": "abc123"}))
    assert dc.login(s, "u", "p") == "abc123"
    assert s.last[0] == "POST" and s.last[1].endswith("/login")


# TC-02 negative: wrong credentials
@pytest.mark.parametrize("code", [401, 403])
def test_login_wrong_password(code):
    with pytest.raises(dc.AuthError):
        dc.login(FakeSession(FakeResp(code, {"error": "nope"})), "u", "bad")


# TC-03 negative: login 200 but token missing/empty
@pytest.mark.parametrize("body", [{}, {"token": ""}, {"token": None}, {"token": 123}])
def test_login_missing_token(body):
    with pytest.raises(dc.MalformedResponse):
        dc.login(FakeSession(FakeResp(200, body)), "u", "p")


# TC-04 positive: valid status, auth header sent
def test_status_success_sends_bearer_token():
    s = FakeSession(ok_status())
    st = dc.get_status(s, "dev1", "tok", now=NOW)
    assert (st["level_percent"], st["pump_state"], st["stale"], st["low_level"]) == (55, "off", False, False)
    assert s.last[2]["headers"]["Authorization"] == "Bearer tok"


# TC-05 negative: invalid/expired token, and no token at all
def test_status_invalid_token_401():
    with pytest.raises(dc.AuthError):
        dc.get_status(FakeSession(FakeResp(401)), "dev1", "expired", now=NOW)

def test_status_missing_token_never_calls_api():
    s = FakeSession(ok_status())
    with pytest.raises(dc.AuthError):
        dc.get_status(s, "dev1", "", now=NOW)
    assert s.last is None


# TC-06 negative: unknown device -> 404, offline device -> 503
def test_status_unknown_device():
    with pytest.raises(dc.DeviceNotFound):
        dc.get_status(FakeSession(FakeResp(404)), "ghost", "tok", now=NOW)

def test_status_device_offline():
    with pytest.raises(dc.DeviceOffline):
        dc.get_status(FakeSession(FakeResp(503)), "dev1", "tok", now=NOW)


# TC-07 negative: malformed responses
@pytest.mark.parametrize("resp", [
    FakeResp(200, bad_json=True),                                   # not JSON
    FakeResp(200, ["list", "not", "object"]),                       # wrong shape
    FakeResp(200, {"pump_state": "on", "last_seen": "2026-11-04T11:58:00Z"}),   # missing level
    ok_status(level_percent="high"),                                # wrong type
    ok_status(level_percent=True),                                  # bool is not a number
    ok_status(pump_state="maybe"),                                  # not on/off
    ok_status(last_seen="yesterday-ish"),                           # unparseable time
])
def test_status_malformed(resp):
    with pytest.raises(dc.MalformedResponse):
        dc.get_status(FakeSession(resp), "dev1", "tok", now=NOW)


# TC-08 edge: level boundaries (0, 100 valid; -1, 100.1 invalid)
@pytest.mark.parametrize("lvl", [0, 100, 0.0, 99.9])
def test_level_boundaries_valid(lvl):
    assert dc.get_status(FakeSession(ok_status(level_percent=lvl)), "d", "t", now=NOW)["level_percent"] == lvl

@pytest.mark.parametrize("lvl", [-1, 100.1, 150])
def test_level_out_of_range_rejected(lvl):
    with pytest.raises(dc.MalformedResponse):
        dc.get_status(FakeSession(ok_status(level_percent=lvl)), "d", "t", now=NOW)


# TC-09 edge: low-level threshold is strictly < 20
@pytest.mark.parametrize("lvl,expected", [(19.9, True), (20, False), (0, True), (21, False)])
def test_low_level_flag(lvl, expected):
    assert dc.get_status(FakeSession(ok_status(level_percent=lvl)), "d", "t", now=NOW)["low_level"] is expected


# TC-10 edge: stale last_seen (15-minute boundary)
@pytest.mark.parametrize("seen,stale", [
    ("2026-11-04T11:58:00Z", False),   # 2 min old
    ("2026-11-04T11:45:00Z", False),   # exactly 15 min -> not stale
    ("2026-11-04T11:44:59Z", True),    # just over
    ("2026-11-03T12:00:00Z", True),    # a day old
])
def test_stale_last_seen(seen, stale):
    st = dc.get_status(FakeSession(ok_status(last_seen=seen)), "d", "t", now=NOW)
    assert st["stale"] is stale


# TC-11 negative: network timeout propagates (app must handle it, not hide it)
def test_network_timeout_propagates():
    with pytest.raises(TimeoutError):
        dc.get_status(FakeSession(exc=TimeoutError("timed out")), "d", "t", now=NOW)
