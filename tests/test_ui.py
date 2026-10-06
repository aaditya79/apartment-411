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
        browser.close()
    print("All UI tests passed.")


if __name__ == "__main__":
    main()
