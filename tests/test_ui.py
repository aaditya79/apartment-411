"""Browser test of the lease chip and session resume, against a running server (uv run app.py).

Run: uv run --with playwright python -m tests.test_ui   (uses your installed Google Chrome)
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SAMPLE = Path(__file__).parent.parent / "data" / "sample_lease.txt"
DRAFT = "look through my lease please and let me know if something is off"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        chats = []
        page.on("request", lambda r: r.url.endswith("/chat") and chats.append(json.loads(r.post_data or "{}")))
        page.goto(BASE)

        # 1. Attaching a lease never sends a message and keeps the draft.
        page.fill("#input", DRAFT)
        page.set_input_files("#file", str(SAMPLE))
        page.wait_for_selector("#attach-chip >> text=Lease attached")
        page.wait_for_timeout(500)
        assert page.input_value("#input") == DRAFT, "the draft must survive an upload"
        assert not chats and page.locator(".msg").count() == 0, f"an upload must not send: {chats}"
        print("ok  upload attaches without sending, draft kept")

        # 2. The × detaches it, on the page and on the server.
        page.click("#attach-chip .chip-x")
        page.wait_for_selector("#attach-chip", state="hidden", timeout=10_000)  # after the server confirms
        assert page.request.get(f"{BASE}/session").json()["lease"]["attached"] is False
        print("ok  × removes the lease")

        # 3. Empty input after an upload: a hint, and send means "review it".
        page.fill("#input", "")
        page.set_input_files("#file", str(SAMPLE))
        page.wait_for_selector("#attach-chip >> text=Lease attached")
        assert "press send to review" in page.get_attribute("#input", "placeholder")
        page.click("#send")
        page.wait_for_function("document.querySelectorAll('.msg.bot .answer').length >= 1", timeout=180_000)
        assert chats and chats[-1]["message"] == "Review my lease", chats
        print("ok  empty send after upload reviews the lease")
        sid = page.evaluate("sessionId")

        # 4. New search clears the chip and the session's lease.
        page.click("#new-search")
        page.wait_for_selector("#attach-chip", state="hidden", timeout=10_000)
        assert page.locator(".msg").count() == 0
        assert page.request.get(f"{BASE}/session").json()["lease"]["attached"] is False
        print("ok  New search clears the lease")

        # 5. Resume the earlier session by pasting its ID (same visitor).
        page.fill("#resume-id", sid)
        page.click("#resume-form button")
        page.wait_for_selector(".msg.bot .answer")
        assert page.evaluate("sessionId") == sid
        assert page.locator("details.call").count() >= 1, "tool-call cards are redrawn"
        assert page.locator(".card").count() >= 1, "the building panel is redrawn"
        assert not page.is_visible("#attach-chip"), "New search dropped that session's lease"
        print("ok  resume by pasted ID restores messages, tool cards and panel (lease stays dropped)")

        # 6. A made-up ID is refused, with a message, not a silent new session.
        page.click("#new-search")
        page.fill("#resume-id", "00000000-0000-4000-8000-000000000000")
        page.click("#resume-form button")
        page.wait_for_selector("#resume-error >> text=No session found with that ID")
        print("ok  unknown ID refused")

        # 7. ?session=<id> works for the same visitor, and is refused for another one (a new browser here).
        page.goto(f"{BASE}/?session={sid}")
        page.wait_for_selector(".msg.bot .answer")
        assert page.evaluate("sessionId") == sid and "session=" not in page.url
        stranger = browser.new_context().new_page()
        stranger.goto(f"{BASE}/?session={sid}")
        stranger.wait_for_selector("#resume-error >> text=No session found with that ID")
        assert stranger.locator(".msg").count() == 0
        print("ok  ?session= resumes for the same visitor, refused for another")
        # 8. "What I can check": exactly the tools the model has; a row drafts its question, unsent.
        page = browser.new_context().new_page()
        chats.clear()
        page.on("request", lambda r: r.url.endswith("/chat") and chats.append(r.url))
        page.goto(BASE)
        served = page.request.get(f"{BASE}/tools").json()
        page.click("#capabilities summary")
        rows = page.locator("#cap-rows tr.cap-row")
        assert rows.count() == len(served) == 12, rows.count()
        listed = [page.locator("#cap-rows tr.cap-row .fn").nth(i).inner_text().rstrip("()") for i in range(12)]
        assert listed == [t["name"] for t in served], listed
        sun = next(i for i, t in enumerate(served) if t["name"] == "estimate_sunlight")
        rows.nth(sun).click()
        assert page.input_value("#input") == "" and page.get_attribute("#input", "placeholder") == served[sun]["example"]
        assert page.inner_text("#tool-chip").startswith("/estimate_sunlight") and not chats
        page.click("#tool-chip .chip-x")  # × clears the chip and the hint
        assert page.is_hidden("#tool-chip") and page.get_attribute("#input", "placeholder") != served[sun]["example"]
        print("ok  'What I can check' lists exactly the 12 tools; a row picks the check (empty input, example as hint); × clears it")

        # 9. The / menu opens, filters, and picking selects the check: empty input, example as placeholder, nothing sent.
        page.type("#input", "/")
        page.wait_for_selector("#slash button")
        assert page.locator("#slash button").count() == 12
        page.type("#input", "sun")
        assert 1 <= page.locator("#slash button").count() < 12
        assert "Sunlight" in page.locator("#slash button").first.inner_text(), "name matches rank first"
        page.locator("#slash button").first.click()
        assert page.input_value("#input") == "" and page.get_attribute("#input", "placeholder") == served[sun]["example"]
        assert page.is_hidden("#slash") and page.is_visible("#tool-chip") and not chats
        page.type("#input", "/zzz")
        assert "No check matches" in page.inner_text("#slash")
        page.keyboard.press("Escape")
        assert page.is_hidden("#slash")
        page.fill("#input", "")
        print("ok  / menu opens, filters, and picking selects the check without filling the input or sending")

        # 10. With a check picked, my own words are sent, the model is steered to that tool, and the chip clears.
        bodies, picks = [], []
        page.on("request", lambda r: r.url.endswith("/chat") and bodies.append(json.loads(r.post_data)))
        page.on("request", lambda r: r.url.endswith("/pick-tool") and picks.append(json.loads(r.post_data)))
        mine = "is it bright in the morning on the 4th floor of 155 East 92nd Street, Manhattan?"
        page.fill("#input", mine)
        with page.expect_response(lambda r: r.url.endswith("/chat")) as reply:
            page.click("#send")
        assert bodies[-1]["message"] == mine and picks[-1]["tool"] == "estimate_sunlight", (bodies, picks)
        assert page.is_hidden("#tool-chip"), "the chip clears after sending"
        used = [c["name"] for c in reply.value.json()["tool_calls"]]
        assert "estimate_sunlight" in used, used
        page.wait_for_selector(".msg.bot .answer")
        print("ok  typed question sent as typed, steered to the picked tool, chip cleared")

        # 11. Picked check + empty input: send asks the example question.
        page.type("#input", "/rats")
        page.keyboard.press("Enter")
        pests = next(t for t in served if t["name"] == "check_pests")
        assert page.input_value("#input") == "" and page.get_attribute("#input", "placeholder") == pests["example"]
        with page.expect_response(lambda r: r.url.endswith("/chat")):
            page.click("#send")
        assert bodies[-1]["message"] == pests["example"] and picks[-1]["tool"] == "check_pests"
        assert page.is_hidden("#tool-chip")
        print("ok  empty send with a picked check asks its example")
        # 12. Several night walks in one turn: every route, station and label is drawn.
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.goto(BASE)
        page.fill("#input", "2053 Frederick Douglass Blvd, tell me about the night walk from the closest 2 stop, 1 stop and C stop.")
        with page.expect_response(lambda r: r.url.endswith("/chat"), timeout=180_000) as reply:
            page.click("#send")
        walks = [c for c in reply.value.json()["tool_calls"] if c["name"] == "night_walk_check"
                 and "error" not in json.loads(c["result"]) and not json.loads(c["result"]).get("duplicate")]
        assert len(walks) == 3, [c["args"] for c in walks]
        page.wait_for_selector("path.walk-route")
        page.wait_for_timeout(500)
        assert page.locator("path.walk-route").count() == 3, page.locator("path.walk-route").count()
        assert page.locator("path.walk-station").count() == 3
        labels = sorted(page.locator(".leaflet-tooltip.station-label").all_inner_texts())
        assert len(labels) == 3 and len(set(labels)) == 3 and all(" · " in l for l in labels), labels
        assert page.locator(".card", has_text="Night walks · 3 stations").locator("li").count() == 3
        assert "Reported incidents, 9pm–5am, in the 12 months to " in page.inner_text(".card:has-text('Night walks')")
        page.screenshot(path=str(Path(__file__).parent.parent / "scratch" / "shots" / "three_walks.png"))
        print(f"ok  3 night walks: 3 routes, 3 station markers, labels {labels}, one card row each")
        # 13. "All the options" (no line): every walkable station is drawn, with the window on the card.
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.goto(BASE)
        page.fill("#input", "What are all the night walk options to 155 East 92nd Street, Manhattan?")
        with page.expect_response(lambda r: r.url.endswith("/chat"), timeout=180_000) as reply:
            page.click("#send")
        page.wait_for_selector("path.walk-route")
        page.wait_for_timeout(500)
        routes = page.locator("path.walk-route").count()
        assert routes > 1, f"all options should draw several routes, drew {routes}"
        card_text = page.inner_text(".card:has-text('Night walks')")
        assert "Reported incidents, 9pm–5am, in the 12 months to " in card_text, card_text
        page.screenshot(path=str(Path(__file__).parent.parent / "scratch" / "shots" / "all_walks.png"))
        print(f"ok  all night-walk options: {routes} routes drawn, window shown on the card")
        # 14. Progress is decoration: if /progress fails or says nothing, the answer still renders.
        for mode in ("abort", "500", "empty"):
            page = browser.new_context().new_page()
            if mode == "abort":
                page.route("**/progress**", lambda r: r.abort())
            elif mode == "500":
                page.route("**/progress**", lambda r: r.fulfill(status=500, body=""))
            else:
                page.route("**/progress**", lambda r: r.fulfill(status=200, body="{}", content_type="application/json"))
            page.goto(BASE)
            page.fill("#input", "Any rats or bedbugs at 155 East 92nd Street, Manhattan?")
            page.click("#send")
            page.wait_for_selector(".msg.bot .answer", timeout=180_000)
            page.wait_for_timeout(300)
            assert page.locator(".progress").count() == 0 and page.locator("#panel-skeleton").count() == 0, mode
        print("ok  a failing or empty /progress never blocks or hides the answer")
        # 15. Stray LaTeX becomes plain text; dollar amounts are never touched.
        page = browser.new_context().new_page()
        page.goto(BASE)
        cases = {
            "$$$y = mx + b$$": "y = mx + b",
            "where $m$ is the slope": "where m is the slope",
            "$$\\frac{rent}{income} \\times 100$$": "(rent)/(income) × 100",
            "Rent is $3,000 and the deposit is $3,000.": "Rent is $3,000 and the deposit is $3,000.",
            "Fees: $20 late fee, $50 move-in ($70 total).": "Fees: $20 late fee, $50 move-in ($70 total).",
            "Between $2,500 and $3,100/mo, about $36,000 a year": "Between $2,500 and $3,100/mo, about $36,000 a year",
        }
        for raw, want in cases.items():
            got = page.evaluate("mathToText", raw)
            assert got == want, (raw, got, want)
        print("ok  stray $…$ math is stripped to plain text; dollar amounts are untouched")
        browser.close()
    print("All UI tests passed.")


if __name__ == "__main__":
    main()
