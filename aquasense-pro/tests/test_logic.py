"""Unit tests for the pure business logic (no HTTP)."""
import server

def dev(thr=20): return {"id": "t", "name": "T", "location": "x", "capacity_litres": 1000, "low_threshold": thr, "pump_mode": "auto", "online": 1, "last_seen": "x"}
def rs(*levels): return [{"timestamp": f"2026-11-02T{6 + 2 * i:02d}:00:00", "level_percent": v} for i, v in enumerate(levels)]

def test_threshold_is_strictly_below():
    assert server.alert_episodes(dev(), rs(20, 20, 20)) == []          # exactly 20% is not an alert
    assert len(server.alert_episodes(dev(), rs(20, 19.9, 20))) == 1    # 19.9% is

def test_episode_grouping_and_open_episode():
    eps = server.alert_episodes(dev(), rs(50, 15, 10, 40, 18))
    assert [(e["lowest"], e["active"]) for e in eps] == [(10, False), (18, True)]
    assert eps[0]["duration_minutes"] == 240 and eps[0]["readings"] == 2

def test_summary_min_max_tie_takes_earliest_and_uses_latest_day():
    rows = rs(40, 10, 10, 90) + [{"timestamp": "2026-11-03T00:00:00", "level_percent": 55}]
    s = server.summarize(dev(), rows)
    assert s["today"]["date"] == "2026-11-03" and s["today"]["min"]["level_percent"] == 55
    s = server.summarize(dev(), rows[:4])
    assert s["today"]["min"]["timestamp"].endswith("08:00:00") and s["today"]["max"]["level_percent"] == 90

def test_trend_and_time_to_low():
    s = server.summarize(dev(), rs(70, 60))  # -5 %/h from 60% -> 40 points above the limit
    assert s["trend_per_hour"] == -5.0 and s["hours_to_low"] == 8.0

def test_summary_without_readings_does_not_crash():
    assert server.summarize(dev(), [])["current"] is None

def test_pump_auto_and_manual():
    assert server.pump_state(dev(), 29) == "on" and server.pump_state(dev(), 30) == "off"
    assert server.pump_state({**dev(), "pump_mode": "off"}, 5) == "off"

def test_level_validation():
    assert all(server.valid_level(v) for v in (0, 100, 55.5))
    assert not any(server.valid_level(v) for v in (-0.1, 100.1, "5", None, True))
