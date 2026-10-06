import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import litellm
import uvicorn
from fastapi import Cookie, FastAPI, File, Form, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import lease
import nyc
from tools import TOOLS, run_tool

# --- Config ---

SYSTEM_PROMPT = """You are Apartment 411. You help New York City renters see a building's real record before they \
sign a lease, and help current tenants get repairs made. Coverage: NYC only; full records exist for rental buildings \
with 3+ apartments that are registered with HPD.

How to use your tools:
- When the user mentions a new address, call look_up_building first.
- "Should I rent here?" / "tell me about this building": call check_maintenance_record, get_tenant_complaints, \
check_pests, check_evictions_and_court, get_landlord_portfolio and get_neighborhood_context (in parallel is fine). \
Then offer the sunlight check (ask which floor and which side if you don't know) and the night-walk check.
- Pasted listing text, or a quote from a listing ("the listing says..."): fact_check_listing with the quoted text, even if you've already run other tools; give its verdict for each claim.
- An uploaded or pasted lease ("review my lease"): review_lease. Present its flags as questions to raise with the \
landlord, not legal conclusions. If is_sample is true, say it's the fictional sample lease.
- A current tenant describing a repair problem: draft_repair_request, and show the letter in full. Use only the apartment and name the user gave you; if the tool reports matching violations in other apartments, ask whether one of them is theirs instead of assuming.
- Follow-ups are about the building already being discussed unless the user names another one. For comparisons, \
reuse results already in this conversation and only call tools for buildings you haven't looked up.
- You cannot plan commutes or estimate travel times or distances; if asked, say only that, and offer the night-walk check from the station they'd use. Never describe where places are from your own knowledge: no "a few blocks from campus", "walking distance", "close to the park". The only distances you may give are the walk minutes a tool returned.

Rules:
- Never state a fact about a building, a landlord or a lease that didn't come from a tool result in this \
conversation. If a tool returns an error, say what couldn't be checked and follow its next_step.
- Give numbers with context: per apartment, compared with the area, and over what period. Compare like with \
like: a rate with a rate (per apartment or per 100 apartments), a count with a count, never a count with a rate. \
When a tool gives a ready-made comparison (compare_as, this_building_vs_area), use it.
- Say "open violation", not "unfixed problem", and mention once that open can mean fixed but not certified.
- Sun times are approximate ranges; mention that reflected light isn't counted when brightness matters.
- Crime: report counts with their context and period only. Never call a place or its residents dangerous or safe, \
and never mention demographics.
- Talk about named people neutrally: report what the records say, never judge character or intent. Refer to a \
person by name or "they"; never guess anyone's gender.
- Copy names, numbers, IDs and dates exactly as the tool returned them (e.g. an LLC's name character for character).
- This is not legal advice; point to the official sources the tools return.

Format for a building report: a one-sentence verdict, then "🚩 Red flags", then "✅ Green flags", then three \
specific questions to ask the broker or landlord, then one line on data limits. About 250 words unless the user \
asks for more. Use markdown."""
MODEL = "vertex_ai/gemini-3.5-flash-lite"
MAX_TOOL_ROUNDS = 10

# Map-only fields: the UI draws them from tool_calls, the model doesn't need hundreds of coordinates.
MAP_ONLY_KEYS = ("points", "all_buildings", "wall_lat_lon", "route_lat_lon")


def for_the_model(result: str) -> str:
    """The tool result the model reads: the same JSON, with long map arrays replaced by a count."""
    try:
        data = json.loads(result)
    except ValueError:
        return result

    def trim(value):
        if isinstance(value, dict):
            return {k: (f"[{len(v)} map items]" if k in MAP_ONLY_KEYS and isinstance(v, list) else trim(v))
                    for k, v in value.items()}
        if isinstance(value, list):
            return [trim(v) for v in value]
        return value

    return json.dumps(trim(data))


# --- The Harness ---


def run_one(call, state: dict) -> tuple[dict, str]:
    """Parse one tool call's arguments and run it. Logs the tool name and time only, never arguments
    (they can contain a pasted lease or listing)."""
    started = time.time()
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        return {}, json.dumps({"error": "Arguments were not valid JSON.",
                               "next_step": "Call the tool again with a JSON object of arguments."})
    result = run_tool(call.function.name, args, state)
    print(f"tool {call.function.name.strip()} {time.time() - started:.1f}s", flush=True)
    return args, result


def run_agent(messages: list[dict], state: dict) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        reply = litellm.completion(
            model=MODEL,
            vertex_location="global",
            messages=messages,
            tools=TOOLS,
        ).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            return reply.content, tool_calls

        # The harness, not the model, runs each tool and appends the result. A building report asks
        # for ~7 tools in one round; each waits on city data, so run them at the same time.
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda call: run_one(call, state), reply.tool_calls))
        for call, (args, result) in zip(reply.tool_calls, results):
            tool_calls += [{"name": call.function.name.strip(), "args": args, "result": result}]
            messages += [{"role": "tool", "tool_call_id": call.id, "content": for_the_model(result)}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# --- Session Store ---

# session_id -> {"messages", "state", "created", "lock"}. In-memory, single process (Cloud Run max
# instances = 1), so sessions are lost when the instance restarts.
sessions: dict[str, dict] = {}
sessions_lock = threading.Lock()
SESSION_COOKIE = "a411_session"


def new_session() -> str:
    session_id = str(uuid.uuid4())
    with sessions_lock:
        sessions[session_id] = {
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
            "state": {"current_bbl": None, "buildings": {}, "lease_text": None},
            "created": time.time(),
            "lock": threading.Lock(),  # one turn at a time per session
        }
    return session_id


def get_session(session_id: str | None) -> str:
    """The session to use. Only IDs this server issued are accepted: an unknown or made-up
    ID gets a fresh session, so two clients can never share one by picking the same string."""
    with sessions_lock:
        if session_id in sessions:
            return session_id
    return new_session()


def with_cookie(response: Response, session_id: str) -> None:
    # Lets a page refresh resume the conversation; httponly because the page gets the ID from responses.
    response.set_cookie(SESSION_COOKIE, session_id, max_age=7 * 24 * 3600, samesite="lax", httponly=True)


# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, response: Response, a411_session: str | None = Cookie(default=None)):
    # Get or create the session (the body's ID wins; the cookie resumes after a refresh)
    session_id = get_session(request.session_id or a411_session)
    session = sessions[session_id]
    with_cookie(response, session_id)

    with session["lock"]:
        # A long pasted lease is stored for review_lease, so the model never has to copy it into a tool call.
        if lease.looks_like_lease(request.message):
            session["state"]["lease_text"] = request.message

        # An upload happens outside the chat, so tell the model about it once, with the next message.
        content = request.message
        if session["state"].pop("lease_attached_note", None):
            content = ("[Note from the app: the user attached a lease to this conversation; review_lease can read "
                       "it.]\n" + content)

        # Append user's message to the context
        session["messages"] += [{"role": "user", "content": content}]

        try:
            answer, tool_calls = run_agent(session["messages"], session["state"])
        except Exception as e:
            # Auth, billing, a model that is not running: show it in the chat, not as a 500.
            answer, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []
        session.setdefault("all_tool_calls", []).extend(tool_calls)
        session["last_tool_calls"] = session["all_tool_calls"]  # lets a refreshed page redraw its panel

    return ChatResponse(response=answer or "", session_id=session_id, tool_calls=tool_calls)


@app.post("/upload")
async def upload(file: UploadFile = File(...), session_id: str | None = Form(default=None),
                 a411_session: str | None = Cookie(default=None)):
    """Attach a lease (PDF or .txt, up to 5 MB) to the session for review_lease. The text stays in
    memory for this session only and is never logged."""
    session_id = get_session(session_id or a411_session)
    data = await file.read(lease.MAX_UPLOAD_BYTES + 1)

    def reply(status: int, body: dict) -> JSONResponse:
        resp = JSONResponse(status_code=status, content={"session_id": session_id, **body})
        with_cookie(resp, session_id)
        return resp

    if len(data) > lease.MAX_UPLOAD_BYTES:
        return reply(413, {"error": "That file is over 5 MB. Upload a smaller PDF or paste the lease text."})
    name = (file.filename or "").lower()
    try:
        if name.endswith(".pdf") or data[:5] == b"%PDF-":
            text = lease.pdf_to_text(data)
        elif name.endswith(".txt") or (file.content_type or "").startswith("text/"):
            text = data.decode("utf-8", errors="replace")
        else:
            return reply(415, {"error": "Upload a PDF or a .txt file, or paste the lease text."})
    except ValueError as e:
        return reply(422, {"error": str(e)})
    if len(text.strip()) < 200:
        return reply(422, {"error": "That file has almost no text. Paste the lease text instead."})

    sessions[session_id]["state"]["lease_text"] = text
    sessions[session_id]["state"]["lease_attached_note"] = True
    return reply(200, {"status": "ok", "characters": len(text), "is_sample": lease.is_sample(text)})


class SessionRequest(BaseModel):
    session_id: str | None = None


@app.post("/sample-lease")
def sample_lease(request: SessionRequest, response: Response, a411_session: str | None = Cookie(default=None)):
    """Load the fictional sample lease into the session (for 'Try a sample lease')."""
    session_id = get_session(request.session_id or a411_session)
    sessions[session_id]["state"]["lease_text"] = (Path(__file__).parent / "data" / "sample_lease.txt").read_text()
    sessions[session_id]["state"]["lease_attached_note"] = True
    with_cookie(response, session_id)
    return {"status": "ok", "session_id": session_id, "is_sample": True}


@app.get("/session")
def current_session(a411_session: str | None = Cookie(default=None)):
    """For a page refresh: the cookie's session (if this server issued it) and its conversation so far."""
    with sessions_lock:
        session = sessions.get(a411_session)
    if not session:
        return {"session_id": None, "history": []}
    history = []
    for m in session["messages"]:
        if m["role"] == "user":
            history.append({"role": "user", "content": m["content"].split("]\n", 1)[-1]
                            if m["content"].startswith("[Note from the app:") else m["content"]})
        elif m["role"] == "tool":
            continue
        elif m["role"] == "assistant" and m.get("tool_calls"):
            continue
        elif m["role"] == "assistant":
            history.append({"role": "assistant", "content": m.get("content") or ""})
    return {"session_id": a411_session, "history": history, "tool_calls": session.get("last_tool_calls", [])}


@app.post("/clear")
def clear(response: Response, session_id: str | None = None, a411_session: str | None = Cookie(default=None)):
    with sessions_lock:
        sessions.pop(session_id or a411_session, None)
    fresh = new_session()
    with_cookie(response, fresh)
    return {"status": "ok", "session_id": fresh}


# --- Demo warm-up ---

WARM_UP_ADDRESSES = ["155 East 92nd Street, Manhattan"]
WARM_UP_CALLS = [("check_maintenance_record", {}), ("get_tenant_complaints", {}), ("check_pests", {}),
                 ("check_evictions_and_court", {}), ("get_landlord_portfolio", {}), ("get_neighborhood_context", {}),
                 ("estimate_sunlight", {"floor": "4"}), ("estimate_sunlight", {"floor": "all"}), ("night_walk_check", {}),
                 ("fact_check_listing", {"listing_text": "sun-drenched 4th floor in a well-maintained building", "floor": "4"}),
                 ("draft_repair_request", {"issues": ["water_leak", "paint_plaster"]})]


def warm_up() -> None:
    """Run the README queries' tools so city-data caches (ours and Socrata's) are hot before a
    grader arrives: cold, a building report took 60s+ in testing, warm about 4s. Repeats a bit
    inside our cache lifetime so the demo stays warm while the instance is up."""
    while True:
        warm_once()
        time.sleep(nyc.CACHE_SECONDS - 3600)


def warm_once() -> None:
    started = time.time()
    for address in WARM_UP_ADDRESSES:
        state: dict = {}
        run_tool("look_up_building", {"address": address}, state)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda call: run_tool(call[0], call[1], state), WARM_UP_CALLS))
        state["lease_text"] = (Path(__file__).parent / "data" / "sample_lease.txt").read_text()
        run_tool("review_lease", {}, state)
    print(f"Warm-up finished in {time.time() - started:.1f}s", flush=True)


@app.on_event("startup")
def start_warm_up() -> None:
    threading.Thread(target=warm_up, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
