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
    assert fresh != sid and sid not in server.sessions
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


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"All {len(tests)} session tests passed.")
