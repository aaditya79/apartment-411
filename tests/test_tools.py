"""Tool and harness behavior without calling the model. Run: uv run python -m tests.test_tools"""

import json
from types import SimpleNamespace

import app
from tools import run_tool

NYCHA = "483 2nd Ave, Manhattan"  # NYC Housing Authority: real building, not HPD-registered


def fake_call(i: int, name: str, args: dict):
    return SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def test_unregistered_building_stays_selected():
    """Look up a building that isn't HPD-registered, then use tools with no address."""
    state = {}
    looked_up = json.loads(run_tool("look_up_building", {"address": NYCHA}, state))
    assert looked_up["hpd_registered"] is False and state["current_bbl"], looked_up
    for name, args in [("check_maintenance_record", {}), ("get_tenant_complaints", {})]:
        r = json.loads(run_tool(name, args, state))
        assert "No building selected" not in r.get("error", ""), (name, r)
        assert "NYCHA" in r.get("error", "") and "HPD" in r["error"], (name, r)  # its own, specific message
    pests = json.loads(run_tool("check_pests", {}, state))
    assert "error" not in pests and "Not HPD-registered" in pests["bedbug_filings"], pests
    assert pests["rodent_inspections_since_2023"]["inspections"] >= 0  # Health Dept. data still applies
    sun = json.loads(run_tool("estimate_sunlight", {}, state))
    assert "error" not in sun and sun["winter_sun_by_floor"], sun
    walk = json.loads(run_tool("night_walk_check", {}, state))
    assert "error" not in walk and walk["station"]["name"], walk
    area = json.loads(run_tool("get_neighborhood_context", {}, state))
    assert "error" not in area and area["open_violations_per_apartment"]["this_building"] is None, area
    assert area["open_violations_per_apartment"]["this_building_vs_area"].startswith("not compared"), area


def test_lookup_and_follow_ups_in_the_same_round():
    """The model often asks for the lookup and the follow-ups (no address) at once: the lookup must run first."""
    state = {}
    calls = [fake_call(0, "check_maintenance_record", {}), fake_call(1, "estimate_sunlight", {}),
             fake_call(2, "look_up_building", {"address": NYCHA}), fake_call(3, "check_pests", {}),
             fake_call(4, "night_walk_check", {})]
    results = app.run_round(calls, state)
    assert [c.id for c, _, _ in results] == [c.id for c in calls], "results keep the model's order"
    for call, args, result in results:
        assert "No building selected" not in result, (call.function.name, result[:200])
    sun = json.loads(dict((c.function.name, r) for c, _, r in results)["estimate_sunlight"])
    assert "error" not in sun, sun


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"All {len(tests)} tool tests passed.")
