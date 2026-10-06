"""Phase 3 end-to-end tests against a running server (uv run app.py).

Run: uv run python -m tests.test_conversation [base_url]
Prints every turn's tools and answer, checks the /chat shape, session separation,
forged session IDs, and the lease upload paths.
"""

import json
import sys
import time
import uuid
from pathlib import Path

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SAMPLE = Path(__file__).parent.parent / "data" / "sample_lease.txt"
problems: list[str] = []


def check(ok: bool, message: str) -> None:
    if not ok:
        problems.append(message)
        print(f"   !! {message}")


def chat(message: str, session_id: str | None, label: str, http=requests) -> dict:
    started = time.time()
    r = http.post(f"{BASE}/chat", json={"message": message, "session_id": session_id}, timeout=300)
    elapsed = time.time() - started
    body = r.json()
    print(f"\n=== {label} ({elapsed:.1f}s) session {body.get('session_id', '?')[:8]}")
    print(f">>> {message[:160]}")
    print("tools:", [(c["name"], c["args"]) for c in body.get("tool_calls", [])])
    print(body.get("response", "")[:1800])
    # The starter's /chat shape, unchanged.
    check(r.status_code == 200, f"{label}: HTTP {r.status_code}")
    check(set(body) == {"response", "session_id", "tool_calls"}, f"{label}: response keys {set(body)}")
    for c in body.get("tool_calls", []):
        check(set(c) == {"name", "args", "result"} and isinstance(c["result"], str), f"{label}: tool_call shape {set(c)}")
    check(not body.get("response", "").startswith("Model call failed"), f"{label}: model call failed")
    return body


def tools_used(body: dict) -> list[str]:
    return [c["name"] for c in body["tool_calls"]]


# --- Session 1: the README queries and follow-ups ---
s1 = chat("I'm thinking of renting at 184 Claremont Ave in Manhattan. Should I worry about anything?", None, "1 README q1")
sid1 = s1["session_id"]
check("look_up_building" in tools_used(s1), "q1 should look up the building")
check({"check_maintenance_record", "get_landlord_portfolio"} <= set(tools_used(s1)), "q1 should run the report tools")

s = chat("Who owns this building, and how do they treat tenants in their other buildings?", sid1, "2 README q2")
check(s["session_id"] == sid1, "q2 should stay in session 1")
check("get_landlord_portfolio" in tools_used(s) or "get_landlord_portfolio" in tools_used(s1),
      "q2 should use the portfolio (now or from q1)")

s = chat("The listing says 'sun-drenched 4th floor in a well-maintained building' for 184 Claremont Ave. Is that true?",
         sid1, "3 README q3")
check("fact_check_listing" in tools_used(s) or "estimate_sunlight" in tools_used(s), "q3 should check the listing")

s = chat("Is that normal for the area?", sid1, "4 follow-up: area")
s = chat("Which floor would I need for winter sun?", sid1, "5 follow-up: winter sun")
check(any(c["name"] == "estimate_sunlight" and str(c["args"].get("floor")) == "all" for c in s["tool_calls"]),
      "5 should sweep floors")
s = chat("My bathroom ceiling has been leaking for months. Write a letter to my landlord.", sid1, "6 repair letter")
check("draft_repair_request" in tools_used(s), "6 should draft the letter")
check(not any(c["args"].get("apartment") for c in s["tool_calls"] if c["name"] == "draft_repair_request"),
      "6 must not invent the tenant's apartment")
s = chat("How safe is the walk home at night, and how long is my commute to Columbia (116th & Broadway)?", sid1,
         "7 night walk + commute")
check("night_walk_check" in tools_used(s), "7 should run the night walk")
check(not any(w in s["response"].lower() for w in ("blocks from", "few blocks", "walking distance", "same avenue", "minutes by subway", "minute ride")),
      "7 must not make up commute distances or times")

# --- Session 2: a fresh comparison; session 1 must be untouched ---
s2 = chat("Compare 184 Claremont Ave and 2053 Frederick Douglass Blvd, both in Manhattan", None, "8 new session: compare")
check(s2["session_id"] != sid1, "8 should get a new session")
s = chat("What building were we talking about, and what did the sun check say?", sid1, "8b session 1 still intact")
check(s["session_id"] == sid1, "8b should resume session 1")
check("Frederick Douglass" not in s["response"], "8b: session 1 must not know about session 2's building")

# --- Session 3: a fake address ---
s3 = chat("What about 123 Fake Street?", None, "9 new session: fake address")
check("look_up_building" in tools_used(s3), "9 should try the lookup")
check(s3["session_id"] not in (sid1, s2["session_id"]), "9 should be a new session")

# --- Forged and unknown session IDs ---
print("\n=== forged session IDs")
for forged in ["abc", str(uuid.uuid4()), sid1[:-1] + ("0" if sid1[-1] != "0" else "1")]:
    r = requests.post(f"{BASE}/chat", json={"message": "hi", "session_id": forged}, timeout=120).json()
    print(f"   sent {forged[:12]}... -> got {r['session_id'][:8]}")
    check(r["session_id"] != forged, f"forged id {forged} was accepted")
    uuid.UUID(r["session_id"], version=4)
# Two clients sending the same made-up ID must not share a session.
a = requests.post(f"{BASE}/chat", json={"message": "hello", "session_id": "shared"}, timeout=120).json()["session_id"]
b = requests.post(f"{BASE}/chat", json={"message": "hello", "session_id": "shared"}, timeout=120).json()["session_id"]
check(a != b, "two clients picking the same ID must get different sessions")

# --- Cookie resume ---
print("\n=== cookie resume")
browser = requests.Session()
first = chat("Look up 2053 Frederick Douglass Blvd, Manhattan", None, "cookie: first turn", http=browser)
resumed = browser.get(f"{BASE}/session", timeout=30).json()
check(resumed["session_id"] == first["session_id"], "GET /session should return the cookie's session")
check(len(resumed["history"]) == 2, f"history should have 2 turns, has {len(resumed['history'])}")
again = chat("How many apartments does it have?", None, "cookie: no session_id in body", http=browser)
check(again["session_id"] == first["session_id"], "the cookie should resume the session")

# --- Leases ---
print("\n=== lease: sample loader")
sid = requests.post(f"{BASE}/sample-lease", json={}, timeout=30).json()["session_id"]
s = chat("Review my lease", sid, "lease: sample")
check("review_lease" in tools_used(s), "should call review_lease")

print("\n=== lease: .txt upload")
r = requests.post(f"{BASE}/upload", files={"file": ("lease.txt", SAMPLE.read_bytes(), "text/plain")}, timeout=60)
print("  ", r.status_code, r.json())
check(r.status_code == 200 and r.json()["is_sample"], "txt upload")

print("\n=== lease: PDF upload")
from tests.make_pdf import text_to_pdf  # noqa: E402
pdf = text_to_pdf(SAMPLE.read_text(), "[FICTIONAL SAMPLE — not a real lease]", "[FICTIONAL SAMPLE — not a real lease]")
r = requests.post(f"{BASE}/upload", files={"file": ("lease.pdf", pdf, "application/pdf")}, timeout=60)
print("  ", r.status_code, r.json())
check(r.status_code == 200 and r.json()["is_sample"], "pdf upload")
s = chat("Review my lease, money terms only", r.json()["session_id"], "lease: uploaded PDF")
check("review_lease" in tools_used(s), "should review the uploaded PDF")

print("\n=== lease: bad uploads")
for name, data, ctype, want in [("scan.pdf", b"%PDF-1.4 not really", "application/pdf", 422),
                                ("photo.png", b"\x89PNG....", "image/png", 415),
                                ("big.txt", b"x" * (5 * 1024 * 1024 + 10), "text/plain", 413)]:
    r = requests.post(f"{BASE}/upload", files={"file": (name, data, ctype)}, timeout=60)
    print(f"   {name}: {r.status_code} {r.json().get('error')}")
    check(r.status_code == want, f"{name} should be {want}, got {r.status_code}")

print("\n=== lease: pasted in chat")
s = chat(SAMPLE.read_text() + "\n\nCan you check this lease?", None, "lease: pasted")
check("review_lease" in tools_used(s), "a pasted lease should be reviewed")

print("\n" + ("ALL CHECKS PASSED" if not problems else f"{len(problems)} PROBLEMS:\n- " + "\n- ".join(problems)))
