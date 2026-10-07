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
        assert rows.count() == len(served) == 13, rows.count()
        listed = [page.locator("#cap-rows tr.cap-row .fn").nth(i).inner_text().rstrip("()") for i in range(13)]
        assert listed == [t["name"] for t in served], listed
        sun = next(i for i, t in enumerate(served) if t["name"] == "estimate_sunlight")
        rows.nth(sun).click()
        assert page.input_value("#input") == "" and page.get_attribute("#input", "placeholder") == served[sun]["example"]
        assert page.inner_text("#tool-chip").startswith("/estimate_sunlight") and not chats
        page.click("#tool-chip .chip-x")  # × clears the chip and the hint
        assert page.is_hidden("#tool-chip") and page.get_attribute("#input", "placeholder") != served[sun]["example"]
        print("ok  'What I can check' lists exactly the 13 tools; a row picks the check (empty input, example as hint); × clears it")

        # 9. The / menu opens, filters, and picking selects the check: empty input, example as placeholder, nothing sent.
        page.type("#input", "/")
        page.wait_for_selector("#slash button")
        assert page.locator("#slash button").count() == 13
        page.type("#input", "sun")
        assert 1 <= page.locator("#slash button").count() < 13
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
        # 16. The README's three sample queries are word for word the first three start cards, in order.
        import re
        readme = (Path(__file__).parent.parent / "README.md").read_text()
        listed = re.findall(r'^\d\. \*\*(.+?)\*\* "(.+)"$', readme, re.M)
        cards = page.locator("#examples-start .example")
        shown = [(cards.nth(i).locator(".k").text_content().strip(),
                  cards.nth(i).text_content().strip()[len(cards.nth(i).locator(".k").text_content().strip()):].strip())
                 for i in range(cards.count())]
        assert len(listed) == 3 and shown == listed, (shown, listed)
        print("ok  README sample queries match the start cards word for word")
        # 17. Attach and send at once: the message waits for the upload, and review_lease gets the lease first time.
        from tests.make_pdf import text_to_pdf
        big = Path(__file__).parent.parent / "scratch" / "padded_sample_lease.pdf"
        big.parent.mkdir(exist_ok=True)
        big.write_bytes(text_to_pdf(SAMPLE.read_text(), "[FICTIONAL SAMPLE — not a real lease]", "[FICTIONAL SAMPLE — not a real lease]",
                                    padding_bytes=8_000_000))
        page = browser.new_context().new_page()
        page.goto(BASE)
        page.set_input_files("#file", str(big))
        page.fill("#input", "look through the attached lease")
        with page.expect_response(lambda r: r.url.endswith("/chat"), timeout=240_000) as reply:
            page.keyboard.press("Enter")
            page.wait_for_selector("#attach-chip >> text=message will send", timeout=10_000)  # held, and said so
            assert page.input_value("#input") == "look through the attached lease", "the typed text stays while held"
        reviews = [json.loads(c["result"]) for c in reply.value.json()["tool_calls"] if c["name"] == "review_lease"]
        assert reviews and all("error" not in r for r in reviews), reviews
        assert page.locator(".msg.user").count() == 1, "one send, not a failed one and a resend"
        page.wait_for_selector("#attach-chip >> text=Lease attached")
        print("ok  a send right after choosing a file waits for the upload; review_lease gets the lease first time")
        # 18. A sent message appears exactly once; Enter mid-composition (dictation, predictive text) doesn't send.
        page = browser.new_context().new_page()
        page.goto(BASE)
        sent = []
        page.on("request", lambda r: r.url.endswith("/chat") and sent.append(json.loads(r.post_data)["message"]))
        cdp = page.context.new_cdp_session(page)
        page.click("#input")
        page.keyboard.type("is the area ")
        cdp.send("Input.imeSetComposition", {"text": "safe", "selectionStart": 4, "selectionEnd": 4})
        page.keyboard.press("Enter")
        cdp.send("Input.insertText", {"text": "safe"})
        page.wait_for_timeout(400)
        # (A real IME consumes that Enter; the simulated one also types a newline, which send() trims.)
        assert not sent and page.input_value("#input").strip() == "is the area safe", (sent, page.input_value("#input"))
        with page.expect_response(lambda r: r.url.endswith("/chat"), timeout=240_000):
            page.keyboard.press("Enter")
        assert sent == ["is the area safe"] and page.locator(".msg.user").all_inner_texts() == ["is the area safe"], sent
        print("ok  a message appears exactly once in its bubble; Enter while composing doesn't send")
        # 19. Two buildings in one turn: each paired row shows its own building and figures.
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.goto(BASE)
        page.fill("#input", "compare 155 East 92nd Street and 2053 Frederick Douglass Blvd")
        with page.expect_response(lambda r: r.url.endswith("/chat"), timeout=300_000) as reply:
            page.click("#send")
        page.wait_for_selector(".msg.bot .answer")
        rows = {}
        for d in page.locator("details.call").all():
            rows.setdefault(d.locator(".fn").inner_text(), []).append(d.locator(".gist").inner_text())
        paired = {k: v for k, v in rows.items() if len(v) > 1 and k != "look_up_building()"}
        assert paired, rows
        for name, gists in paired.items():
            ok = [g for g in gists if not g.startswith("error")]
            assert len(set(ok)) == len(ok), (name, gists)
            assert all(" — " in g for g in ok), (name, gists)  # the building is named on each row
        print(f"ok  a two-building comparison shows distinct, labelled rows: {paired}")
        # 20. The sunlight card shows every side the tool returned, and the map keeps one copy of the world.
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.goto(BASE)
        page.fill("#input", "how much sun does the 2nd floor of 2053 Frederick Douglass Blvd get")
        with page.expect_response(lambda r: r.url.endswith("/chat"), timeout=300_000) as reply:
            page.click("#send")
        page.wait_for_selector(".card:has-text('Direct sun')")
        suns = [json.loads(c["result"]) for c in reply.value.json()["tool_calls"] if c["name"] == "estimate_sunlight"]
        sides = [x["side"] for x in next(r for r in suns if r.get("sides"))["sides"]]
        card_text = page.locator(".card:has-text('Direct sun')").text_content()  # as written, not as CSS uppercases it
        assert f"{len(sides)} sides" in card_text and all(label in card_text for label in sides), (sides, card_text[:300])
        assert page.evaluate("map.getMinZoom()") == 10 and page.evaluate("map.getZoom()") >= 10
        print(f"ok  the sunlight card shows all {len(sides)} sides with unique labels; the map stays at city zoom")
        # 21. The README's three queries by clicking, in one session: the cards and the tool list stay reachable.
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.goto(BASE)
        with page.expect_response(lambda r: r.url.endswith("/chat"), timeout=300_000) as first:
            page.locator("#examples-start .example").nth(0).click()
        page.wait_for_selector(".msg.bot .answer")
        sid = page.evaluate("sessionId")
        assert first.value.json()["session_id"] == sid
        page.click("#to-examples")
        page.wait_for_timeout(600)
        assert page.locator("#examples-start .example").nth(1).is_visible(), "Examples brings the cards back into view"
        with page.expect_response(lambda r: r.url.endswith("/chat"), timeout=300_000) as second:
            page.locator("#examples-start .example").nth(1).click()
        body = second.value.json()
        assert body["session_id"] == sid == page.evaluate("sessionId"), "card 2 must send into the same session"
        assert "look_up_building" not in [c["name"] for c in body["tool_calls"]], [c["name"] for c in body["tool_calls"]]
        page.click("#capabilities summary")
        assert page.locator("#cap-rows tr.cap-row").count() == 13, "the tool list stays reachable mid-conversation"
        print("ok  cards 1 and 2 clicked in one session (same ID, no second lookup); the 13-tool list stays reachable")
        mobile = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        mobile.goto(BASE)
        mobile.wait_for_selector("#to-examples", state="visible")
        assert mobile.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0, "no sideways scroll at 390px"
        print("ok  the header with Examples fits at 390px")
        # 22. Every card is one control: its heading, body and padding all send that card's full query, as do Enter
        # and Space. (The chat is answered by a stub here: this checks what is sent, not the answer.)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.route("**/chat", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"response": "ok", "session_id": json.loads(r.request.post_data)["session_id"], "tool_calls": []})))
        page.goto(BASE)
        examples = page.evaluate("EXAMPLES")
        cards = page.locator(".example")
        assert cards.count() == len(examples) == 8, cards.count()
        def expected(i):
            return "Review my lease" if examples[i].get("lease") else examples[i]["text"]
        def last_bubble():
            return page.locator(".msg.user").last.inner_text()
        for i in range(8):
            card = page.locator(f'.example[data-i="{i}"]')
            for where in ("heading", "padding"):
                card.scroll_into_view_if_needed()  # each send scrolls the chat down: measure right before clicking
                box = card.locator(".k").bounding_box() if where == "heading" else card.bounding_box()
                n = page.locator(".msg.user").count()
                x, y = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2) if where == "heading" else (box["x"] + 5, box["y"] + 4)
                page.mouse.click(x, y)
                page.wait_for_function(f"document.querySelectorAll('.msg.user').length > {n}")
                page.wait_for_function("!busy")
                assert last_bubble() == expected(i), (i, where, last_bubble(), expected(i))
        for i, key in ((0, "Enter"), (3, " ")):
            n = page.locator(".msg.user").count()
            page.locator(f'.example[data-i="{i}"]').focus()
            page.keyboard.press(key)
            page.wait_for_function(f"document.querySelectorAll('.msg.user').length > {n}")
            page.wait_for_function("!busy")
            assert last_bubble() == expected(i), (i, key, last_bubble())
        page.wait_for_function("!busy")
        page.evaluate("getSelection().removeAllRanges()")  # start clean, then double-click the heading for real
        card = page.locator('.example[data-i="0"]')
        card.scroll_into_view_if_needed()
        box = card.locator(".k").bounding_box()
        page.mouse.dblclick(box["x"] + 20, box["y"] + box["height"] / 2)
        page.wait_for_function("!busy")
        assert page.evaluate("String(getSelection())") == "", "card text can't be selected (or dragged into the input)"
        page.unroute("**/chat")
        print("ok  all 8 cards send their full query from the heading, the padding, Enter and Space; card text isn't selectable")
        # 23. The floor sweep on a tall building is a chart: one row per band of floors, the 1h+ winter band marked.
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent))
        from tools import run_tool as _run_tool
        sweep = _run_tool("estimate_sunlight", {"address": "1 Hanson Place, Brooklyn"}, {})
        bands = json.loads(sweep)["bands"]
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.route("**/chat", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"response": "ok", "session_id": json.loads(r.request.post_data)["session_id"],
             "tool_calls": [{"name": "estimate_sunlight", "args": {}, "result": sweep}]})))
        page.goto(BASE)
        page.fill("#input", "How much winter sun does each floor of 1 Hanson Place, Brooklyn get?")
        page.click("#send")
        page.wait_for_selector(".sweep-row")
        assert page.locator(".sweep-row").count() == len(bands), (page.locator(".sweep-row").count(), len(bands))
        assert page.locator(".sweep-row.mark").count() == 1 and page.locator(".sweep .sbar.w").count() == len(bands)
        assert "Lowest floor with 1h+ winter sun" in page.inner_text(".card:has-text('Winter sun by floor')")
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        page.unroute("**/chat")
        print(f"ok  a 41-floor sweep renders as {len(bands)} chart rows (Dec 21 and today), with the 1h+ winter band marked")
        browser.close()
    print("All UI tests passed.")


if __name__ == "__main__":
    import traceback
    try:
        main()
    except BaseException:
        traceback.print_exc()  # print it before Playwright's shutdown can swallow it
        sys.stdout.flush(); sys.stderr.flush()
        raise SystemExit(1)
