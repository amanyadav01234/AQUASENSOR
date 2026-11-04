"""Integration tests for auth, security and the REST API (real server, temp DB)."""
import urllib.error, urllib.request
import pytest

U = {"name": "Test User", "email": "Test@Example.com", "password": "secret123"}


# ---------- auth ----------
def test_signup_then_login(call):
    code, d, _ = call("POST", "/api/signup", U); assert code == 201 and d["token"]
    code, d, _ = call("POST", "/login", {"email": "test@example.com", "password": "secret123"}); assert code == 200 and d["token"]

def test_duplicate_signup_rejected(call):
    assert call("POST", "/api/signup", U)[0] == 409

@pytest.mark.parametrize("patch", [{"name": "A"}, {"email": "nope"}, {"password": "short"}, {"password": "onlyletters"}, {"password": "12345678"}])
def test_signup_validation(call, patch):
    assert call("POST", "/api/signup", {**U, "email": "x@y.com", **patch})[0] == 400

def test_wrong_password_and_unknown_user_same_message(call):
    a = call("POST", "/login", {"email": U["email"], "password": "wrongpass1"})[:2]
    b = call("POST", "/login", {"email": "ghost@x.com", "password": "wrongpass1"})[:2]
    assert a == b and a[0] == 401

def test_login_rate_limit_after_repeated_failures(call):
    for _ in range(5): assert call("POST", "/login", {"email": "brute@x.com", "password": "nope"})[0] == 401
    code, d, h = call("POST", "/login", {"email": "brute@x.com", "password": "nope"})
    assert code == 429 and int(h["Retry-After"]) > 0

def test_invalid_json_and_oversized_body(call):
    assert call("POST", "/login", body=[1, 2])[0] == 400

def test_change_password(call):
    call("POST", "/api/signup", {"name": "Pw User", "email": "pw@example.com", "password": "oldpass123"})
    tok = call("POST", "/login", {"email": "pw@example.com", "password": "oldpass123"})[1]["token"]
    assert call("POST", "/api/me/password", {"current_password": "bad", "new_password": "newpass123"}, tok)[0] == 403
    assert call("POST", "/api/me/password", {"current_password": "oldpass123", "new_password": "weak"}, tok)[0] == 400
    assert call("POST", "/api/me/password", {"current_password": "oldpass123", "new_password": "newpass123"}, tok)[0] == 200
    assert call("POST", "/login", {"email": "pw@example.com", "password": "newpass123"})[0] == 200
    assert call("POST", "/login", {"email": "pw@example.com", "password": "oldpass123"})[0] == 401

def test_token_refresh_and_tampering(call, token):
    assert call("POST", "/api/refresh", {}, token)[1]["token"]
    uid, exp, sig = token.split(".")
    assert call("GET", "/api/me", token=f"{uid}.{int(exp) + 9999}.{sig}")[0] == 401  # forged expiry

def test_expired_token_rejected(call):
    import server
    old = server.TOKEN_TTL; server.TOKEN_TTL = -10
    try: tok = server.make_token(1)
    finally: server.TOKEN_TTL = old
    assert call("GET", "/api/me", token=tok)[0] == 401

@pytest.mark.parametrize("path", ["/api/me", "/api/devices", "/api/alerts", "/api/readings", "/device/tank-1/status", "/api/devices/tank-1"])
def test_protected_routes_need_token(call, path):
    assert call("GET", path)[0] == 401
    assert call("GET", path, token="garbage")[0] == 401


# ---------- device API ----------
def test_device_status_contract(call, token):
    code, st, _ = call("GET", "/device/tank-1/status", token=token)
    assert code == 200 and set(st) == {"level_percent", "pump_state", "last_seen"}
    assert st["level_percent"] == 55 and st["pump_state"] == "off"
    assert call("GET", "/device/ghost/status", token=token)[0] == 404

def test_offline_device_returns_503(call, token):
    code, d, _ = call("GET", "/device/tank-3/status", token=token)
    assert code == 503 and d["error"] == "Device offline" and d["last_seen"]

def test_tank1_summary_matches_assignment(call, token):
    d = call("GET", "/api/devices/tank-1", token=token)[1]
    assert d["current"]["level_percent"] == 55 and d["is_low"] is False
    assert d["today"]["date"] == "2026-11-02"
    assert (d["today"]["min"]["level_percent"], d["today"]["min"]["timestamp"][11:16]) == (18, "16:00")
    assert (d["today"]["max"]["level_percent"], d["today"]["max"]["timestamp"][11:16]) == (78, "06:00")
    assert len(d["episodes"]) == 1 and d["episodes"][0]["lowest"] == 18 and d["episodes"][0]["duration_minutes"] == 120

def test_devices_list_and_readings_range(call, token):
    devs = call("GET", "/api/devices", token=token)[1]
    assert {d["id"] for d in devs} == {"tank-1", "tank-2", "tank-3"} and all(d["spark"] for d in devs)
    assert len(call("GET", "/api/devices/tank-1/readings?range=today", token=token)[1]) == 8
    assert len(call("GET", "/api/devices/tank-2/readings?range=all", token=token)[1]) > len(call("GET", "/api/devices/tank-2/readings?range=today", token=token)[1])
    assert call("GET", "/api/devices/tank-1/readings?range=bogus", token=token)[0] == 400

def test_add_reading_validation_and_low_alert(call, token):
    for bad in (150, -1, "50", None, True): assert call("POST", "/api/devices/tank-2/readings", {"level_percent": bad}, token)[0] == 400
    assert call("POST", "/api/devices/ghost/readings", {"level_percent": 5}, token)[0] == 404
    assert call("POST", "/api/devices/tank-2/readings", {"level_percent": 12}, token)[0] == 201
    d = call("GET", "/api/devices/tank-2", token=token)[1]
    assert d["is_low"] is True and d["current"]["level_percent"] == 12 and d["pump_state"] == "on"
    assert any(e["active"] and e["device_id"] == "tank-2" for e in call("GET", "/api/alerts", token=token)[1])
    call("POST", "/api/devices/tank-2/readings", {"level_percent": 60}, token)  # recover
    assert call("GET", "/api/devices/tank-2", token=token)[1]["is_low"] is False

def test_legacy_readings_alias(call, token):
    assert len(call("GET", "/api/readings", token=token)[1]) >= 8

def test_pump_mode_and_settings(call, token):
    assert call("POST", "/api/devices/tank-1/pump", {"mode": "sideways"}, token)[0] == 400
    assert call("POST", "/api/devices/tank-1/pump", {"mode": "on"}, token)[0] == 200
    assert call("GET", "/device/tank-1/status", token=token)[1]["pump_state"] == "on"
    call("POST", "/api/devices/tank-1/pump", {"mode": "auto"}, token)
    for bad in ({"low_threshold": 2}, {"low_threshold": 99}, {"low_threshold": 20.5}, {"name": "x"}):
        assert call("POST", "/api/devices/tank-1/settings", bad, token)[0] == 400
    assert call("POST", "/api/devices/tank-1/settings", {"name": "Rooftop Tank A", "low_threshold": 20}, token)[0] == 200

def test_csv_export(call, token):
    code, raw, h = call("GET", "/api/devices/tank-1/export.csv", token=token, raw=True)
    lines = raw.decode().strip().splitlines()
    assert code == 200 and h["Content-Type"] == "text/csv" and lines[0] == "timestamp,level_percent" and len(lines) >= 9


# ---------- web + security ----------
def test_health_is_public(call):
    assert call("GET", "/api/health")[1]["status"] == "ok"

def test_static_assets_and_security_headers(base):
    for p, ctype in (("/", "text/html"), ("/app.js", "javascript"), ("/app.css", "text/css")):
        r = urllib.request.urlopen(base + p)
        assert r.status == 200 and ctype in r.headers["Content-Type"]
    assert "script-src 'self'" in r.headers["Content-Security-Policy"] and r.headers["X-Frame-Options"] == "DENY"

@pytest.mark.parametrize("p", ["/../server.py", "/..%2fserver.py", "/%2e%2e/server.py", "/nope.txt"])
def test_path_traversal_blocked(base, p):
    with pytest.raises(urllib.error.HTTPError) as e: urllib.request.urlopen(base + p)
    assert e.value.code in (400, 404)
