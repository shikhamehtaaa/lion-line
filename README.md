# Lion Line: a Columbia commute agent

**Deployed at:** https://REPLACE-WITH-YOUR-CLOUD-RUN-URL.run.app

Lion Line is a web chat agent for Columbia and Barnard students who ride the 1 train from
116 St–Columbia University. It answers the questions you actually have while packing up in
Butler: *Can I make the next train? Should I run? Is the 1 even running? Do I need an umbrella?*

It uses **live data**, not guesses, and every tool call is shown in the chat as a card you
can expand to see the raw arguments and result. The agent remembers the conversation, so
once you've said you're in Mudd heading downtown, it reuses that for follow-ups.

Built on the course's `gemini-web-tool-calling` starter (FastAPI + LiteLLM +
`vertex_ai/gemini-3.5-flash-lite`). The `/chat` response keeps the starter's shape:
`response`, `session_id`, `tool_calls` (each with `name`, `args`, `result`).

## Tools

All data sources are free and need no API key.

| Tool | What it does | Data source |
|---|---|---|
| `catch_the_train` ⭐ | Combines your walk time to the station with live arrivals and gives a verdict per train: *easy walk (leave by 2:41)*, *leave right now*, *only if you run*, or *you'll miss it*. | MTA GTFS-realtime + campus map |
| `estimate_walk` ⭐ | Walking and jogging time between places. Knows ~30 Columbia/Barnard buildings and dining halls by name (Butler, Lerner, John Jay, Mudd, SIPA, Hewitt, Manhattanville…), and geocodes any other NYC address. | Built-in campus table + OpenStreetMap Nominatim |
| `get_next_trains` | Live 1/2/3 arrivals at any Broadway–7th Av line station, filterable by direction and route. | MTA GTFS-realtime (protobuf) |
| `get_subway_alerts` | Active delays, suspensions and planned work for any subway route. | MTA alerts feed |
| `get_weather` | Temperature, feels-like, wind and max rain chance over the next 4 hours. | Open-Meteo |
| `coffee_before_train` ⭐ | Finds open cafes/food spots you can stop at and *still* catch a train: walk there, estimated wait, walk to the station, checked against live arrivals. Says when to leave and how much extra waiting the stop costs vs. going straight to the platform. | Hand-curated spot table (hours, price, our rating) + MTA GTFS-realtime |
| `sun_spots` ⭐ | Next sunrise/sunset, a sky-quality outlook (vivid / decent / clean but plain / poor) from cloud layers and rain chance, feels-like temperature, and nearby viewpoints with walk time and when to leave. | Open-Meteo + hand-picked viewpoints |

⭐ = our original tools.

Every tool returns JSON. Failures come back as `{"error": ..., "hint": ...}` telling the
model what to do next (retry with a campus building name, ask the user for a direction,
or say the MTA feed is down), so the chat never crashes on a bad argument or network error.

## Sample queries

1. **"I'm in Mudd heading downtown. Can I make the next 1 train?"**
   → calls `catch_the_train` (and usually `get_subway_alerts`) and tells you when to leave.
2. **"Is the 1 train delayed right now? What about the A?"**
   → calls `get_subway_alerts` for route 1, then route A.
3. **"How long is the walk from Butler to the Hungarian Pastry Shop, and do I need an umbrella?"**
   → calls `estimate_walk` and `get_weather`. Follow up with *"ok what about from Lerner instead?"*
   to see it remember the destination.
4. **"I'm in Butler heading downtown. Can I grab a coffee and still catch the train?"**
   → calls `coffee_before_train` and lists spots with a leave-by time.
5. **"Where should I watch the sunset tonight?"**
   → calls `sun_spots` for the time, sky outlook and viewpoints.

## Run locally

1. A GCP project with billing and the Vertex AI (Agent Platform) API enabled.
2. `gcloud auth application-default login`
3. `uv run app.py`, then open http://localhost:8000

## Deploy to Cloud Run (continuous deploy from GitHub)

1. Push this repo to GitHub.
2. In the Cloud Console: **Cloud Run → Create service → Continuously deploy from a repository**,
   connect GitHub, pick this repo and branch `main`, build type **Dockerfile**.
3. Settings:
   - Authentication: **Allow unauthenticated invocations** (graders use a browser).
   - **Maximum instances: 1**. Sessions live in memory, so a single instance keeps
     every conversation on the same process.
   - Container port: 8080 (the Dockerfile sets `PORT`).
4. Give the service's runtime service account the **Vertex AI User** role
   (`roles/aiplatform.user`) so it can call Gemini.
5. Copy the service URL into `submission.json` and the top of this README.

Optional env var: `MODEL` to override the LiteLLM model string.

## Files

- `app.py`: agent loop, session store, `/chat` and `/clear` endpoints
- `tools.py`: the five tools and their JSON schemas
- `index.html`: the Lion Line chat UI
- `Dockerfile`: container for Cloud Run
