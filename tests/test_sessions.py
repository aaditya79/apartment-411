"""Session rules without calling the model: only server-issued IDs are accepted, forged or
unknown IDs get a fresh session, the cookie resumes, sessions stay separate, uploads attach
to the right session. Run: uv run python -m tests.test_sessions"""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

import app as server

# Stand-in for the model: echo the message and how many turns this session has seen,
# appending the reply to the history the way the real harness does.
def fake_agent(messages, state):
    answer = f"echo: {messages[-1]['content'][:40]} (turns: {sum(m['role'] == 'user' for m in messages)})"
    messages.append({"role": "assistant", "content": answer})
    return answer, []


server.run_agent = fake_agent
server.warm_up = lambda: None

SAMPLE = Path(__file__).parent.parent / "data" / "sample_lease.txt"


def post_chat(client, message, session_id=None):
    r = client.post("/chat", json={"message": message, "session_id": session_id})
    assert r.status_code == 200 and set(r.json()) == {"response", "session_id", "tool_calls"}, r.text
    return r.json()


def test_server_issues_ids():
    client = TestClient(server.app)
    first = post_chat(client, "hello")
    assert uuid.UUID(first["session_id"]).version == 4
    again = post_chat(client, "second", first["session_id"])
    assert again["session_id"] == first["session_id"] and "turns: 2" in again["response"]


def test_forged_and_unknown_ids_are_replaced():
    client = TestClient(server.app)
    real = post_chat(client, "hello")["session_id"]
    for forged in ["abc", "", "../../etc", str(uuid.uuid4()), real[:-1] + ("0" if real[-1] != "0" else "1"),
                   real.upper()]:
        got = post_chat(TestClient(server.app), "hi", forged)  # a different browser, no cookie
        assert got["session_id"] != forged and got["session_id"] != real, forged
        assert "turns: 1" in got["response"], "a forged ID must start an empty session"


def test_same_made_up_id_never_shared():
    a = post_chat(TestClient(server.app), "from a", "shared-id")["session_id"]
    b = post_chat(TestClient(server.app), "from b", "shared-id")["session_id"]
    assert a != b


def test_cookie_resumes_and_session_endpoint():
    browser = TestClient(server.app)
    first = post_chat(browser, "remember me")
    assert browser.cookies.get(server.SESSION_COOKIE) == first["session_id"]
    resumed = post_chat(browser, "after refresh")  # no session_id in the body: the cookie resumes it
    assert resumed["session_id"] == first["session_id"] and "turns: 2" in resumed["response"]
    page = browser.get("/session").json()
    assert page["session_id"] == first["session_id"]
    assert [h["role"] for h in page["history"]] == ["user", "assistant", "user", "assistant"]
    # A forged cookie is ignored too.
    stranger = TestClient(server.app)
    stranger.cookies.set(server.SESSION_COOKIE, first["session_id"][:-2] + "zz")
    assert stranger.get("/session").json()["session_id"] is None


def test_clear_gives_a_new_session():
    browser = TestClient(server.app)
    sid = post_chat(browser, "hello")["session_id"]
    fresh = browser.post("/clear", params={"session_id": sid}).json()["session_id"]
    assert fresh != sid and sid in server.sessions  # the old chat stays resumable by its ID
    assert post_chat(browser, "new chat")["session_id"] == fresh


def test_uploads_attach_to_the_right_session():
    a, b = TestClient(server.app), TestClient(server.app)
    sid_a, sid_b = post_chat(a, "a")["session_id"], post_chat(b, "b")["session_id"]
    r = a.post("/upload", files={"file": ("lease.txt", SAMPLE.read_bytes(), "text/plain")}, data={"session_id": sid_a})
    assert r.status_code == 200 and r.json()["session_id"] == sid_a and r.json()["is_sample"]
    assert server.sessions[sid_a]["state"]["lease_text"] and not server.sessions[sid_b]["state"]["lease_text"]
    loaded = b.post("/sample-lease", json={"session_id": sid_b}).json()
    assert loaded["session_id"] == sid_b and server.sessions[sid_b]["state"]["lease_text"]
    forged = TestClient(server.app).post("/sample-lease", json={"session_id": "made-up"}).json()
    assert forged["session_id"] not in ("made-up", sid_a, sid_b)


def test_pasted_lease_is_stored():
    client = TestClient(server.app)
    sid = post_chat(client, SAMPLE.read_text())["session_id"]
    assert server.sessions[sid]["state"]["lease_text"]
    sid2 = post_chat(client, "a short question about rent and the landlord and the lease term")["session_id"]
    assert sid2 == sid  # same session; a short message doesn't replace the stored lease
    assert "FICTIONAL SAMPLE" in server.sessions[sid]["state"]["lease_text"]


SHORT_LEASE = ("LEASE. Landlord: Example Realty LLC. Tenant: Sam Tenant. Premises: 155 East 92nd Street, Apt 2B, "
               "New York, NY 10128. Term: 12 months commencing 01/01/2027. Monthly rent: $3,000.00. Security deposit: "
               "$3,000.00. Late charge: $50 after five days. Tenant shall not sublet without Landlord's consent.")


def test_short_pasted_lease_is_stored_but_questions_dont_replace_it():
    import lease
    assert len(SHORT_LEASE) < 500 and lease.looks_like_lease(SHORT_LEASE)
    client = TestClient(server.app)
    sid = post_chat(client, SHORT_LEASE)["session_id"]
    assert server.sessions[sid]["state"]["lease_text"] == SHORT_LEASE
    question = ("My landlord says the tenant has to pay a $3,000 security deposit before the lease term starts. Is "
                "that allowed for this apartment under the lease and NY rent rules?")
    assert not lease.looks_like_lease(question) or len(question) < 1500
    post_chat(client, question, sid)
    assert server.sessions[sid]["state"]["lease_text"] == SHORT_LEASE, "a question must not replace the stored lease"


def make_docx(text: str) -> bytes:
    """A minimal real .docx: a zip with word/document.xml, one w:p per paragraph."""
    import io
    import zipfile
    from xml.sax.saxutils import escape
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>"
                   for p in text.split("\n\n"))
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def upload(name: str, data: bytes, ctype: str):
    return TestClient(server.app).post("/upload", files={"file": (name, data, ctype)})


def test_upload_formats_and_sizes():
    from tests.make_pdf import text_to_pdf
    lease_text = SAMPLE.read_text()
    MB = 1024 * 1024

    r = upload("lease.docx", make_docx(lease_text), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert r.status_code == 200 and r.json()["is_sample"], r.text
    sid = r.json()["session_id"]
    assert "Example Realty LLC" in server.sessions[sid]["state"]["lease_text"]

    for name, data, ctype, says in [
        ("lease.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 2000, "application/msword", "old Word document (.doc)"),
        ("lease.pages", b"PK\x03\x04" + b"\x00" * 2000, "application/x-iwork-pages-sffpages", "Apple Pages"),
        ("lease.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 2000, "image/jpeg", "photo (JPEG)"),
        ("IMG_0001.HEIC", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 2000, "image/heic", "iPhone photo (HEIC)"),
    ]:
        r = upload(name, data, ctype)
        assert r.status_code == 415 and says in r.json()["error"] and "PDF" in r.json()["error"], (name, r.text)

    big = upload("big.txt", b"x" * (21 * MB), "text/plain")
    assert big.status_code == 413 and big.json()["error"].startswith("That file is 21.0 MB; the limit is 20.0 MB"), big.text

    scan = text_to_pdf("\n" * 120, "", "", padding_bytes=12 * MB)  # 3 empty pages, 12 MB: like a scan
    assert len(scan) > 12 * MB
    r = upload("scan.pdf", scan, "application/pdf")
    assert r.status_code == 422 and "scan with no text layer" in r.json()["error"], r.text

    real = text_to_pdf(lease_text, "", "", padding_bytes=12 * MB)  # a big PDF that does have text
    r = upload("lease.pdf", real, "application/pdf")
    assert r.status_code == 200 and r.json()["is_sample"], r.text


def test_picked_check_is_a_note_for_the_model_once():
    seen = []
    original = server.run_agent

    def capture(messages, state):
        seen.append(messages[-1]["content"])
        return fake_agent(messages, state)

    server.run_agent = capture
    try:
        client = TestClient(server.app)
        sid = post_chat(client, "hello")["session_id"]
        bad = client.post("/pick-tool", json={"tool": "delete_everything", "session_id": sid})
        assert bad.status_code == 400 and "Unknown check" in bad.json()["error"]
        assert client.post("/pick-tool", json={"tool": "estimate_sunlight", "session_id": sid}).json()["tool"] == "estimate_sunlight"
        body = post_chat(client, "is it bright in the morning?", sid)  # /chat's shape is checked in post_chat
        assert seen[-1].startswith("[Note from the app: the user picked the check estimate_sunlight") \
            and seen[-1].endswith("is it bright in the morning?"), seen[-1]
        assert server.sessions[sid]["turns"][-1]["user"] == "is it bright in the morning?", "history keeps my words"
        post_chat(client, "and in winter?", sid)
        assert "picked the check" not in seen[-1], "the pick applies to one message only"
    finally:
        server.run_agent = original


def iap(email: str) -> dict:
    """The header IAP adds on Cloud Run."""
    return {"x-goog-authenticated-user-email": f"accounts.google.com:{email}"}


def test_resume_by_id_same_visitor_only():
    owner = TestClient(server.app, headers=iap("ab1234@columbia.edu"))
    sid = post_chat(owner, "Look up 155 East 92nd Street, Manhattan")["session_id"]
    post_chat(owner, "and the walk home?", sid)
    owner.post("/upload", files={"file": ("lease.txt", SAMPLE.read_bytes(), "text/plain")}, data={"session_id": sid})

    # Same Columbia account, different browser (no cookies): resumes, with turns and the lease.
    other_browser = TestClient(server.app, headers=iap("AB1234@columbia.edu"))
    r = other_browser.get("/session", params={"session_id": sid})
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["session_id"] == sid and len(view["turns"]) == 2 and view["lease"]["attached"], view
    assert view["turns"][0]["user"].startswith("Look up 155 East 92nd")
    assert other_browser.cookies.get(server.SESSION_COOKIE) == sid  # and later turns continue it
    assert post_chat(other_browser, "anything else?")["session_id"] == sid

    before = len(server.sessions)
    for client, sid_try in [(TestClient(server.app, headers=iap("xy9876@columbia.edu")), sid),  # another user
                            (TestClient(server.app), sid),                                      # local: other visitor
                            (other_browser, str(uuid.uuid4())), (other_browser, "made-up")]:      # forged IDs
        r = client.get("/session", params={"session_id": sid_try})
        assert r.status_code == 404 and r.json()["error"] == "No session found with that ID.", r.text
    assert len(server.sessions) == before, "a refused resume must not create a session"

    # Another visitor can't continue it through /chat either: they get their own fresh session.
    stranger = TestClient(server.app, headers=iap("xy9876@columbia.edu"))
    assert post_chat(stranger, "hi", sid)["session_id"] != sid


def test_upload_never_sends_and_both_clears_drop_the_lease():
    client = TestClient(server.app)
    sid = post_chat(client, "hello")["session_id"]
    turns, messages = len(server.sessions[sid]["turns"]), len(server.sessions[sid]["messages"])
    r = client.post("/upload", files={"file": ("my lease.txt", SAMPLE.read_bytes(), "text/plain")}, data={"session_id": sid})
    assert r.status_code == 200 and r.json()["name"] == "my lease.txt"
    assert len(server.sessions[sid]["turns"]) == turns and len(server.sessions[sid]["messages"]) == messages, \
        "an upload must not send a message"
    assert client.get("/session").json()["lease"] == {"attached": True, "name": "my lease.txt"}

    # The × on the chip.
    assert client.post("/lease/clear", json={"session_id": sid}).json()["lease_attached"] is False
    assert not server.sessions[sid]["state"].get("lease_text")
    assert client.get("/session").json()["lease"]["attached"] is False

    # New search.
    client.post("/upload", files={"file": ("lease.txt", SAMPLE.read_bytes(), "text/plain")}, data={"session_id": sid})
    fresh = client.post("/clear", params={"session_id": sid}).json()["session_id"]
    assert not server.sessions[sid]["state"].get("lease_text"), "New search drops the old session's lease"
    assert not server.sessions[fresh]["state"].get("lease_text")
    assert client.get("/session").json()["lease"]["attached"] is False


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"All {len(tests)} session tests passed.")
