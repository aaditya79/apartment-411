"""Phase 3 end-to-end tests against a running server (uv run app.py).

Run: uv run python -m tests.test_conversation [base_url]
Prints every turn's tools and answer, checks the /chat shape, session separation,
forged session IDs, and the lease upload paths.
"""

import json
import re
import sys
import time
import uuid
from pathlib import Path

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SAMPLE = Path(__file__).parent.parent / "data" / "sample_lease.txt"
problems: list[str] = []
# One client that keeps cookies, like a browser: sessions belong to the visitor who started them
# (the IAP account when deployed, a visitor cookie locally), so a cookie-less client is a new visitor
# on every request and can't continue its own sessions.
BROWSER = requests.Session()


def check(ok: bool, message: str) -> None:
    if not ok:
        problems.append(message)
        print(f"   !! {message}")


def chat(message: str, session_id: str | None, label: str, http=BROWSER) -> dict:
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


def fresh() -> str:
    """New search: a new session for this browser (with a kept cookie, no session_id would just continue)."""
    return BROWSER.post(f"{BASE}/clear", timeout=30).json()["session_id"]


def tools_used(body: dict) -> list[str]:
    return [c["name"] for c in body["tool_calls"]]


# --- Session 1: the README queries and follow-ups ---
s1 = chat("I'm thinking of renting at 155 East 92nd Street in Manhattan. Should I worry about anything?", fresh(), "1 README q1")
sid1 = s1["session_id"]
check("look_up_building" in tools_used(s1), "q1 should look up the building")
check({"check_maintenance_record", "get_landlord_portfolio"} <= set(tools_used(s1)), "q1 should run the report tools")

s = chat("Who owns 155 East 92nd Street in Manhattan, and how do they treat tenants in their other buildings?", sid1,
         "2 README q2")
check(s["session_id"] == sid1, "q2 should stay in session 1")
check("get_landlord_portfolio" in tools_used(s) or "get_landlord_portfolio" in tools_used(s1),
      "q2 should use the portfolio (now or from q1)")

s = chat("The listing says 'sun-drenched 4th floor in a well-maintained building' for 155 East 92nd Street. Is that true?",
         sid1, "3 README q3")
check("fact_check_listing" in tools_used(s) or "estimate_sunlight" in tools_used(s), "q3 should check the listing")

s = chat("Is that normal for the area?", sid1, "4 follow-up: area")
s = chat("Which floor would I need for winter sun?", sid1, "5 follow-up: winter sun")
# A sweep: floor='all', or floor omitted (the tool's default is 'all').
check(any(c["name"] == "estimate_sunlight" and str(c["args"].get("floor", "all")) == "all" for c in s["tool_calls"]),
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
s2 = chat("Compare 155 East 92nd Street and 2053 Frederick Douglass Blvd, both in Manhattan", fresh(), "8 new session: compare")
check(s2["session_id"] != sid1, "8 should get a new session")
s = chat("What building were we talking about, and what did the sun check say?", sid1, "8b session 1 still intact")
check(s["session_id"] == sid1, "8b should resume session 1")
check("Frederick Douglass" not in s["response"], "8b: session 1 must not know about session 2's building")

# --- Session 3: a fake address ---
s3 = chat("What about 123 Fake Street?", fresh(), "9 new session: fake address")
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
sid = BROWSER.post(f"{BASE}/sample-lease", json={"session_id": fresh()}, timeout=30).json()["session_id"]
s = chat("Review my lease", sid, "lease: sample")
check("review_lease" in tools_used(s), "should call review_lease")

print("\n=== lease: .txt upload")
r = requests.post(f"{BASE}/upload", files={"file": ("lease.txt", SAMPLE.read_bytes(), "text/plain")}, timeout=60)
print("  ", r.status_code, r.json())
check(r.status_code == 200 and r.json()["is_sample"], "txt upload")

print("\n=== lease: PDF upload")
from tests.make_pdf import text_to_pdf  # noqa: E402
pdf = text_to_pdf(SAMPLE.read_text(), "[FICTIONAL SAMPLE — not a real lease]", "[FICTIONAL SAMPLE — not a real lease]")
r = BROWSER.post(f"{BASE}/upload", files={"file": ("lease.pdf", pdf, "application/pdf")},
                 data={"session_id": fresh()}, timeout=60)
print("  ", r.status_code, r.json())
check(r.status_code == 200 and r.json()["is_sample"], "pdf upload")
s = chat("Review my lease, money terms only", r.json()["session_id"], "lease: uploaded PDF")
check("review_lease" in tools_used(s), "should review the uploaded PDF")

print("\n=== not a lease: a research paper attached as 'the lease'")
sid = fresh()
chat("Look up 41 Tiemann Place, Manhattan", sid, "not a lease: building in the chat")
paper = text_to_pdf((Path(__file__).parent / "fixtures" / "research_paper.txt").read_text(), "", "")
r = BROWSER.post(f"{BASE}/upload", files={"file": ("paper.pdf", paper, "application/pdf")}, data={"session_id": sid}, timeout=60)
check(r.status_code == 200, f"the upload itself is accepted ({r.status_code})")
s = chat("look through the attached lease", sid, "not a lease: review")
reviews = [json.loads(c["result"]) for c in s["tool_calls"] if c["name"] == "review_lease"]
check(bool(reviews) and all("doesn't look like a residential lease" in r.get("error", "") for r in reviews),
      "review_lease must return the not-a-lease error")
check(not re.search(r"bed ?bug|window guard|lead.?paint|sprinkler|\bflags?\b", s["response"], re.IGNORECASE),
      "no flags or missing-disclosure findings for a non-lease")
check("Tiemann" not in s["response"], "the document must not be labelled with the conversation's building")
check(bool(re.search(r"(isn't|is not|doesn't|does not)[^.]{0,40}(lease)", s["response"], re.IGNORECASE)),
      "the answer should say plainly the file isn't a lease")

print("\n=== lease: bad uploads")
for name, data, ctype, want in [("scan.pdf", b"%PDF-1.4 not really", "application/pdf", 422),
                                ("photo.png", b"\x89PNG....", "image/png", 415),
                                ("big.txt", b"x" * (21 * 1024 * 1024), "text/plain", 413)]:
    r = requests.post(f"{BASE}/upload", files={"file": (name, data, ctype)}, timeout=60)
    print(f"   {name}: {r.status_code} {r.json().get('error')}")
    check(r.status_code == want, f"{name} should be {want}, got {r.status_code}")

print("\n=== lease: pasted in chat")
s = chat(SAMPLE.read_text() + "\n\nCan you check this lease?", fresh(), "lease: pasted")
check("review_lease" in tools_used(s), "a pasted lease should be reviewed")

# --- Follow-ups use the building in session state: tools called with no address ---
print("\n=== state follow-ups")
first = chat("Look up 2053 Frederick Douglass Blvd, Manhattan", fresh(), "state: lookup")
sid, already = first["session_id"], set(tools_used(first))
for question, tool in [("is the area safe", "night_walk_check"), ("how much sun", "estimate_sunlight"),
                       ("any pests", "check_pests")]:
    s = chat(question, sid, f"state: {question}")
    calls = [c for c in s["tool_calls"] if c["name"] == tool]
    # Either the tool runs now, without an address, or its result is already in this conversation.
    check(bool(calls) or tool in already, f"'{question}' should call {tool} or reuse it (called {tools_used(s)})")
    # The model may pass the selected building's address explicitly; never a different building.
    check(all("2053 frederick douglass" in c["args"].get("address", "2053 frederick douglass").lower() for c in calls),
          f"'{question}': {tool} should use the building already selected")
    check("street address" not in s["response"].lower(), f"'{question}' must not ask for the address again")
    already |= set(tools_used(s))

# --- Night walk by subway line: one call per line, no duplicate stations, a real answer ---
print("\n=== night walk by line")
s = chat("2053 Frederick Douglass Blvd, tell me about the night walk from the closest 2 stop, 1 stop and C stop.",
         fresh(), "night walk: 2, 1 and C")
walks = [c for c in s["tool_calls"] if c["name"] == "night_walk_check"]
ok_walks = [json.loads(c["result"]) for c in walks if "error" not in json.loads(c["result"])]
stations = [(w["station"]["name"], tuple(w["station"]["lines"])) for w in ok_walks if not w.get("duplicate")]
lines_asked = sorted(str(c["args"].get("line", "")).strip().upper() for c in walks)
check(len(walks) <= 4, f"expected about one night_walk_check per line, got {len(walks)}: {[c['args'] for c in walks]}")
check({"1", "2", "C"} <= set(lines_asked), f"should ask by line 2, 1 and C; asked {lines_asked}")
check(len(stations) == len(set(stations)) == 3, f"three distinct stations expected, got {stations}")
check("tool-call limit" not in s["response"], "must finish with an answer, not the tool-call limit")
ending = s["response"].strip().split("\n")[-1]
check("Cathedral Pkwy" in ending and "C" in ending,
      f"should end by recommending the C at Cathedral Pkwy (2 min, 0 incidents); ended with: {ending[:200]}")

# --- "All the options": every walkable station, and every count carries its window ---
print("\n=== night walk: all options")
s = chat("What are all the night walk options to 155 East 92nd Street, Manhattan?", fresh(), "night walk: all options")
walk_results = [json.loads(c["result"]) for c in s["tool_calls"] if c["name"] == "night_walk_check"]
stations = {(w["station"]["name"], tuple(w["station"]["lines"])) for r in walk_results for w in (r.get("walks") or [r]) if "station" in w}
check(len(stations) > 1, f"all options should cover several stations, got {stations}")
counted = [ln for ln in re.split(r"(?<=[.!?])\s+|\n", s["response"]) if re.search(r"\b\d+\s+(reported\s+)?(street\s+)?incidents?\b", ln)]
bare = [ln for ln in counted if not re.search(r"12 months|2026", ln)]
check(not bare, f"every incident count needs its window; bare: {bare[:3]}")

# --- A repair letter with no stated problem: ask, never invent conditions from records ---
print("\n=== repair letter without a stated problem")
sid = fresh()
chat("Look up 155 East 92nd Street, Manhattan", sid, "repair: building with open violations")
s = chat("create a request to submit to my landlord", sid, "repair: no problem stated")
letters = [json.loads(c["result"]) for c in s["tool_calls"] if c["name"] == "draft_repair_request"]
check(not any("letter_text" in r for r in letters), "no letter may be drafted before the tenant says what's wrong")
check("following conditions in my home" not in s["response"], "the answer must not contain a letter asserting conditions")
check(bool(re.search(r"\?|tell me|let me know|what('s| is) (wrong|the problem)", s["response"], re.IGNORECASE)),
      "the agent should ask what needs repairing")

# --- Casual wording, reworded by the model: still a letter ---
print("\n=== repair letter: casual wording")
sid = fresh()
chat("Look up 155 East 92nd Street, Manhattan", sid, "repair casual: building")
s = chat("my fridge keeps dying and the bathroom ceiling is gross. can you write a letter to my landlord?", sid,
         "repair casual: letter")
letters = [json.loads(c["result"]) for c in s["tool_calls"] if c["name"] == "draft_repair_request"]
check(any("letter_text" in r for r in letters), "a stated problem in casual words must produce a letter")
check(not re.search(r"smoke|carbon monoxide|mold|roach|mice", " ".join(r.get("letter_text", "") for r in letters),
                    re.IGNORECASE), "the letter must not add conditions the tenant never mentioned")

# --- Stay in the domain: redirect off-topic questions, still answer tenant questions ---
print("\n=== domain")
s = chat("explain what linear regression is", fresh(), "domain: off-topic")
check(not s["tool_calls"], f"an off-topic question should call no tools (called {tools_used(s)})")
check(len(s["response"]) < 500, f"an off-topic redirect should be short ({len(s['response'])} chars)")
check(bool(re.search(r"apartment|building|NYC|New York", s["response"])), "the redirect should say what it covers")
check(not re.search(r"slope|intercept|dependent variable|least squares", s["response"], re.IGNORECASE),
      "the redirect must not teach the off-topic subject")
s = chat("What's the most a landlord in NYC can charge me for a security deposit?", fresh(), "domain: tenant rights")
check(bool(re.search(r"one month", s["response"], re.IGNORECASE)) and len(s["response"]) > 150,
      "a tenant-rights question should get a real answer (one month's rent)")
s = chat("How do HPD violation classes work?", fresh(), "domain: HPD classes")
check(bool(re.search(r"class\s*C", s["response"], re.IGNORECASE)) and "hazard" in s["response"].lower(),
      "an HPD question should get a real answer (class C = immediately hazardous)")
for answer in (s["response"],):
    check(not re.search(r"\$\$|\$[A-Za-z\\]", answer), "answers should contain no LaTeX")

# --- Red flags only when worse than the area ---
print("\n=== red-flag rule")
s = chat("I'm thinking of renting at 2053 Frederick Douglass Blvd in Manhattan. Should I worry about anything?",
         fresh(), "red flags vs area")
text = s["response"]
red = text.split("Red flags", 1)[-1].split("Green flags", 1)[0].lower() if "Red flags" in text else ""
check("heat" not in red, "heat (3.3 per 100 apts vs ~96 nearby) must not be listed as a red flag")

# --- A new address in the message: nothing may answer about the previous building ---
print("\n=== new address switches building")
first = chat("Look up 350 5th Avenue, Manhattan", fresh(), "switch: building A")
sid = first["session_id"]
a_label = next(json.loads(c["result"]).get("address") for c in first["tool_calls"] if c["name"] == "look_up_building")
s = chat("2053 Frederick Douglass Blvd, Manhattan", sid, "switch: only building B's address")
stale = [c["name"] for c in s["tool_calls"] if json.loads(c["result"]).get("address") == a_label]
check(not stale, f"tools returned data for the previous building {a_label}: {stale}")

# --- The new example buttons call the tool they showcase ---
print("\n=== example buttons")
s = chat("How much direct sun does a 4th-floor apartment at 155 East 92nd Street, Manhattan get?", fresh(), "example: sunlight")
check("estimate_sunlight" in tools_used(s), f"the sunlight example should call estimate_sunlight (called {tools_used(s)})")
s = chat("Is 155 East 92nd Street, Manhattan worse than its block?", fresh(), "example: vs the block")
check("get_neighborhood_context" in tools_used(s), f"the neighborhood example should call get_neighborhood_context (called {tools_used(s)})")

print("\n" + ("ALL CHECKS PASSED" if not problems else f"{len(problems)} PROBLEMS:\n- " + "\n- ".join(problems)))
