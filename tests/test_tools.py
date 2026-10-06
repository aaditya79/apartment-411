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
    walk = json.loads(run_tool("night_walk_check", {}, state))  # no line: every walkable station
    assert "error" not in walk and walk["walks"] and all(w["station"]["name"] and w["window"] for w in walk["walks"]), walk
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


def test_new_address_in_message_switches_building_first():
    """Building A is current; the user sends only building B's address. No tool may return A's data."""
    from tools import note_addresses_in_message
    state = {}
    a = json.loads(run_tool("look_up_building", {"address": "350 5th Avenue, Manhattan"}, state))["address"]
    note_addresses_in_message("2053 Frederick Douglass Blvd, Manhattan", state)  # what /chat does before the model
    calls = [fake_call(0, "check_maintenance_record", {}), fake_call(1, "check_pests", {}),
             fake_call(2, "estimate_sunlight", {}), fake_call(3, "look_up_building", {"address": "2053 Frederick Douglass Blvd, Manhattan"})]
    for call, args, result in app.run_round(calls, state):
        r = json.loads(result)
        assert r.get("address") != a, (call.function.name, "returned data for the previous building")
    # An address that can't be resolved blocks address-less tools instead of falling back to A.
    note_addresses_in_message("123 Fake Street", state)
    r = json.loads(run_tool("check_pests", {}, state))
    assert "hasn't been looked up" in r.get("error", ""), r


def test_catalog_matches_tools():
    """The UI's panel and / menu list exactly the tools the model has, each with an example."""
    from fastapi.testclient import TestClient
    from tools import TOOLS, TOOL_CATALOG
    names = [t["function"]["name"] for t in TOOLS]
    assert len(names) == 12 and set(TOOL_CATALOG) == set(names), set(TOOL_CATALOG) ^ set(names)
    served = TestClient(app.app).get("/tools").json()
    assert [t["name"] for t in served] == names
    assert all(t["label"] and t["answers"] and t["data"] and t["example"] for t in served)
    assert sum(t["original"] for t in served) == 6


def test_repair_letter_only_states_what_the_tenant_said():
    state = {"user_messages": ["Look up 155 East 92nd Street, Manhattan", "create a request to submit to my landlord"]}
    run_tool("look_up_building", {"address": "155 East 92nd Street, Manhattan"}, state)

    # No problem stated: no letter, a question, and record-based examples to offer (not to assert).
    r = json.loads(run_tool("draft_repair_request", {"issues": ["water_leak"]}, state))
    assert "hasn't said what needs repairing" in r["error"] and "Ask the user" in r["next_step"], r
    assert r["issues_recorded_in_this_building"], r

    # Conditions taken from building records, not from the tenant: refused.
    r = json.loads(run_tool("draft_repair_request", {"issues": ["water_leak", "safety"],
                   "details": "Water leak at ceiling in the bathroom, and smoke/carbon monoxide detectors needing repair"}, state))
    assert "own description" in r.get("error", ""), r

    # The tenant's own words: the letter states exactly those; violations appear only as supporting records.
    state["user_messages"].append("My bathroom ceiling has been leaking since June")
    r = json.loads(run_tool("draft_repair_request", {"issues": ["water_leak"], "apartment": "18",
                   "details": "the bathroom ceiling has been leaking since June"}, state))
    letter = r["letter_text"]
    assert "conditions in my home: the bathroom ceiling has been leaking since June." in letter, letter
    assert "smoke" not in letter.lower() and "carbon monoxide" not in letter.lower()
    assert "City records support this" in letter and "HPD has no open violation" not in letter

    # A stated problem with no matching violation: says so plainly, no contradiction.
    state["user_messages"].append("the elevator is broken")
    r = json.loads(run_tool("draft_repair_request", {"issues": ["elevator"], "details": "the elevator is broken"}, state))
    assert "HPD has no open violation on record matching these conditions" in r["letter_text"], r["letter_text"]
    assert "City records support this" not in r["letter_text"]


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"All {len(tests)} tool tests passed.")
