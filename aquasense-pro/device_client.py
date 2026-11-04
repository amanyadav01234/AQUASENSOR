"""Tiny client for the AquaSense device API, with strict response validation.

The HTTP session is injected, so tests can mock responses without any network.
"""
from datetime import datetime, timedelta, timezone

BASE_URL = "https://api.aquasense.example"
STALE_AFTER = timedelta(minutes=15)


class ApiError(Exception): pass
class AuthError(ApiError): pass
class DeviceNotFound(ApiError): pass
class DeviceOffline(ApiError): pass
class MalformedResponse(ApiError): pass


def _json(resp):
    try:
        return resp.json()
    except ValueError as e:
        raise MalformedResponse("body is not valid JSON") from e


def login(session, username, password):
    resp = session.post(f"{BASE_URL}/login", json={"username": username, "password": password}, timeout=5)
    if resp.status_code in (401, 403):
        raise AuthError("invalid credentials")
    if resp.status_code != 200:
        raise ApiError(f"login failed: {resp.status_code}")
    token = _json(resp).get("token")
    if not isinstance(token, str) or not token:
        raise MalformedResponse("token missing")
    return token


def get_status(session, device_id, token, now=None):
    if not token:
        raise AuthError("missing token")
    resp = session.get(f"{BASE_URL}/device/{device_id}/status",
                       headers={"Authorization": f"Bearer {token}"}, timeout=5)
    if resp.status_code == 401:
        raise AuthError("invalid or expired token")
    if resp.status_code == 404:
        raise DeviceNotFound(device_id)
    if resp.status_code == 503:
        raise DeviceOffline(device_id)
    if resp.status_code != 200:
        raise ApiError(f"unexpected status {resp.status_code}")

    body = _json(resp)
    if not isinstance(body, dict):
        raise MalformedResponse("body is not an object")
    level, pump, seen = body.get("level_percent"), body.get("pump_state"), body.get("last_seen")
    if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 <= level <= 100:
        raise MalformedResponse(f"bad level_percent: {level!r}")
    if pump not in ("on", "off"):
        raise MalformedResponse(f"bad pump_state: {pump!r}")
    try:
        seen_dt = datetime.fromisoformat(str(seen).replace("Z", "+00:00"))
    except ValueError as e:
        raise MalformedResponse(f"bad last_seen: {seen!r}") from e
    if seen_dt.tzinfo is None:
        seen_dt = seen_dt.replace(tzinfo=timezone.utc)

    now = now or datetime.now(timezone.utc)
    return {"level_percent": level, "pump_state": pump, "last_seen": seen_dt,
            "stale": now - seen_dt > STALE_AFTER, "low_level": level < 20}
