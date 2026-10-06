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
        assert not page.is_visible("#attach-chip")
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
        page.wait_for_timeout(500)
        assert not page.is_visible("#attach-chip") and page.locator(".msg").count() == 0
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
        browser.close()
    print("All UI tests passed.")


if __name__ == "__main__":
    main()
