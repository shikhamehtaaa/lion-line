import json
import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import litellm
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tools import TOOLS, run_tool

# --- Config ---

SYSTEM_PROMPT = """You are Lion Line, a commute helper for Columbia and Barnard students in \
Morningside Heights, NYC. The home station is 116 St-Columbia University on the 1 train.

How to work:
- Never guess train times, delays or weather. Use the tools; they return live data.
- "When's the next train" -> get_next_trains. "Can I make it / when should I leave" -> \
catch_the_train (ask uptown or downtown if the user hasn't said). "Is the 1 running / \
delays" -> get_subway_alerts. Walking between buildings -> estimate_walk. Umbrella, \
jacket, or walk-vs-train questions -> get_weather. Coffee/food on the way to the train -> \
coffee_before_train (needs where they are and uptown/downtown). Sunset/sunrise, "where to \
watch it" or "will it be pretty" -> sun_spots.
- Before recommending a subway trip, check get_subway_alerts for that route.
- Remember what the user told you earlier (where they are, where they're headed) and \
reuse it instead of asking again.
- If a tool returns an error, follow its hint: retry with better arguments or tell the \
user plainly what's unavailable.
- Answer in 1-3 short sentences, lead with the decision (e.g. "Leave by 2:41 for the \
2:45 1 train"). Use 12-hour times.
- Never suggest a time that has already passed. Compare every time to the current time above.
- You only have live data for the 1/2/3 line; say so if asked about other lines' times.
"""
MODEL = os.environ.get("MODEL", "vertex_ai/gemini-3.5-flash-lite")
MAX_TOOL_ROUNDS = 6

# --- The Harness ---


def run_agent(messages: list[dict]) -> tuple[str, list[dict]]:
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
            if reply.content:
                return reply.content, tool_calls
            # Gemini sometimes returns an empty turn after tool results. Nudge it once to
            # answer, without saving the nudge into the session history.
            messages.pop()
            retry = litellm.completion(
                model=MODEL,
                vertex_location="global",
                messages=messages + [{"role": "user", "content": "Answer my question using the tool results above."}],
            ).choices[0].message
            text = retry.content or "Sorry, I couldn't put an answer together. Please try asking again."
            messages += [{"role": "assistant", "content": text}]
            return text, tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
                result = run_tool(call.function.name, args)
            except json.JSONDecodeError:
                args = {"raw": call.function.arguments}
                result = json.dumps({"error": "Tool arguments were not valid JSON. Retry with a JSON object."})
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# --- Session Store ---

# session_id -> list of messages. In-memory, single process.
sessions: dict[str, list] = {}

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
def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    # Refresh the clock every turn: a session can stay open for hours.
    right_now = datetime.now(ZoneInfo("America/New_York")).strftime("%A, %B %-d, %Y, %-I:%M %p")
    sessions[session_id][0]["content"] = SYSTEM_PROMPT + f"\nRight now it is {right_now} in New York."

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response or "(no response)", session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    # Cloud Run tells us which port to listen on; locally this defaults to 8000.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
