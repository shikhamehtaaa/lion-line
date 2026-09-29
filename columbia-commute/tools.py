"""Tools for the Columbia Commuter agent, and the JSON that describes them to the model.

Every data source here is free and needs no API key:
- MTA GTFS-realtime feeds (live subway arrivals and service alerts)
- Open-Meteo (weather)
- OpenStreetMap Nominatim (geocoding places that aren't in our campus table)

Every tool returns a JSON string. On failure it returns {"error": ..., "hint": ...}
so the model can tell the user what went wrong or retry with better arguments.
"""

import json
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2

NYC = ZoneInfo("America/New_York")

# --- Data sources (all free, no key) ---

MTA_123_FEED = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs"
MTA_ALERTS_FEED = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.json"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires an identifying User-Agent.
HEADERS = {"User-Agent": "columbia-commuter-agent/1.0 (class project)"}

# --- Static reference data ---

# Broadway-7th Av line (1/2/3) stations in Manhattan and the Bronx, GTFS stop_id -> name.
# The 2/3 only stop at the express stations (96, 72, Times Sq, 34, 14, Chambers).
STATIONS = {
    "101": "Van Cortlandt Park-242 St", "103": "238 St", "104": "231 St",
    "106": "Marble Hill-225 St", "107": "215 St", "108": "207 St", "109": "Dyckman St",
    "110": "191 St", "111": "181 St", "112": "168 St-Washington Hts", "113": "157 St",
    "114": "145 St", "115": "137 St-City College", "116": "125 St",
    "117": "116 St-Columbia University", "118": "Cathedral Pkwy (110 St)", "119": "103 St",
    "120": "96 St", "121": "86 St", "122": "79 St", "123": "72 St",
    "124": "66 St-Lincoln Center", "125": "59 St-Columbus Circle", "126": "50 St",
    "127": "Times Sq-42 St", "128": "34 St-Penn Station", "129": "28 St", "130": "23 St",
    "131": "18 St", "132": "14 St", "133": "Christopher St-Sheridan Sq", "134": "Houston St",
    "135": "Canal St", "136": "Franklin St", "137": "Chambers St", "138": "WTC Cortlandt",
    "139": "Rector St", "142": "South Ferry",
}

# Campus landmarks (lat, lon), so walking estimates work without a geocoder.
# Keys are lowercase aliases; several aliases can point at the same spot.
_LERNER = (40.8069, -73.9636)
_JOHN_JAY = (40.8061, -73.9618)
PLACES = {
    "116 st station": (40.8078, -73.9640),
    "116 st-columbia university": (40.8078, -73.9640),
    "cathedral pkwy (110 st)": (40.8036, -73.9667),
    "110 st station": (40.8036, -73.9667),
    "125 st station": (40.8156, -73.9585),
    "college walk": (40.8075, -73.9626),
    "columbia": (40.8075, -73.9626),
    "low library": (40.8080, -73.9621),
    "butler library": (40.8064, -73.9630),
    "butler": (40.8064, -73.9630),
    "lerner hall": _LERNER,
    "lerner": _LERNER,
    "ferris booth commons": _LERNER,
    "john jay dining hall": _JOHN_JAY,
    "john jay": _JOHN_JAY,
    "jj's place": _JOHN_JAY,
    "hamilton hall": (40.8069, -73.9612),
    "northwest corner building": (40.8099, -73.9617),
    "pupin hall": (40.8099, -73.9612),
    "mudd": (40.8094, -73.9600),
    "uris hall": (40.8090, -73.9612),
    "dodge fitness center": (40.8093, -73.9628),
    "international affairs building": (40.8078, -73.9597),
    "sipa": (40.8078, -73.9597),
    "teachers college": (40.8104, -73.9601),
    "barnard": (40.8095, -73.9645),
    "milstein center": (40.8098, -73.9638),
    "hewitt dining hall": (40.8095, -73.9650),
    "manhattanville": (40.8165, -73.9575),
    "business school": (40.8174, -73.9568),
    "st john the divine": (40.8038, -73.9618),
    "hungarian pastry shop": (40.8035, -73.9633),
    "koronet pizza": (40.8034, -73.9651),
    "morningside park": (40.8065, -73.9580),
    "riverside park": (40.8095, -73.9688),
}

WALK_M_PER_MIN = 80  # about 3 mph
JOG_M_PER_MIN = 140  # a hurried jog with a backpack
GRID_FACTOR = 1.25   # streets aren't straight lines; scales crow-flies distance
STATION_BUFFER_MIN = 1.5  # stairs + OMNY tap + getting to the platform


# --- Helpers ---


def _error(message: str, hint: str = "") -> str:
    """The model can't see exceptions, so errors come back as data it can act on."""
    return json.dumps({"error": message, "hint": hint} if hint else {"error": message})


def _now() -> datetime:
    return datetime.now(NYC)


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, NYC).strftime("%-I:%M %p")


def _find_station(name: str) -> str | None:
    """Match a user-typed station name ('116', 'columbia', 'times square') to a stop_id."""
    q = name.lower().replace("parkway", "pkwy").replace("street", "st").replace("square", "sq").replace("th ", " ").strip()
    q = q.removesuffix("th").removesuffix(" station").strip()
    if not q:
        return None
    # Exact or prefix matches first ("14 st" must not match "145 St"), then substrings.
    for stop_id, station in STATIONS.items():
        s = station.lower()
        if q == s or s.startswith(q + " ") or s.startswith(q + "-"):
            return stop_id
    for stop_id, station in STATIONS.items():
        if q in station.lower():
            return stop_id
    return None


def _resolve_place(place: str) -> tuple[float, float, str] | None:
    """Campus table first, then OpenStreetMap, restricted to the NYC area."""
    key = place.lower().strip()
    if key in PLACES:
        lat, lon = PLACES[key]
        return lat, lon, place
    for alias, (lat, lon) in PLACES.items():
        if key in alias or alias in key:
            return lat, lon, alias
    r = requests.get(
        NOMINATIM_URL,
        params={
            "q": place, "format": "json", "limit": 1,
            "viewbox": "-74.26,40.92,-73.70,40.49", "bounded": 1,  # NYC bounding box
        },
        headers=HEADERS, timeout=10,
    )
    r.raise_for_status()
    hits = r.json()
    if not hits:
        return None
    return float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0]["display_name"].split(",")[0]


def _walk_minutes(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    """Return (meters, minutes) for walking between two lat/lon points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    meters = 2 * 6_371_000 * math.asin(math.sqrt(h)) * GRID_FACTOR
    return meters, meters / WALK_M_PER_MIN


_feed_cache: dict[str, tuple[float, object]] = {}


def _fetch_trip_feed() -> gtfs_realtime_pb2.FeedMessage:
    """The 1/2/3 feed, cached for 20s so one chat turn doesn't hammer the MTA."""
    cached = _feed_cache.get("123")
    if cached and time.time() - cached[0] < 20:
        return cached[1]
    r = requests.get(MTA_123_FEED, headers=HEADERS, timeout=10)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    _feed_cache["123"] = (time.time(), feed)
    return feed


def _normalize_direction(direction: str) -> str | None:
    d = direction.lower().strip()
    if d in ("uptown", "north", "northbound", "n", "bronx"):
        return "N"
    if d in ("downtown", "south", "southbound", "s", "midtown"):
        return "S"
    if d in ("", "both", "any"):
        return ""
    return None


def _arrivals(stop_id: str, direction: str, route: str = "") -> list[dict]:
    """Upcoming arrivals at a stop, soonest first."""
    now = time.time()
    wanted = {stop_id + d for d in (direction or "NS")}
    out = []
    for entity in _fetch_trip_feed().entity:
        if not entity.HasField("trip_update"):
            continue
        trip = entity.trip_update.trip
        if route and trip.route_id != route:
            continue
        for stu in entity.trip_update.stop_time_update:
            if stu.stop_id not in wanted:
                continue
            ts = stu.arrival.time or stu.departure.time
            if ts and ts >= now - 30:
                out.append({
                    "route": trip.route_id,
                    "direction": "uptown" if stu.stop_id.endswith("N") else "downtown",
                    "arrives_at": _clock(ts),
                    "minutes_away": round((ts - now) / 60, 1),
                    "_ts": ts,
                })
    return sorted(out, key=lambda a: a["_ts"])


# --- Tools ---


def get_next_trains(station: str = "116 St-Columbia University", direction: str = "both",
                    route: str = "", limit: int = 6) -> str:
    """Live arrivals on the 1/2/3 at a Broadway-7th Av line station."""
    stop_id = _find_station(station)
    if not stop_id:
        return _error(
            f"'{station}' isn't a 1/2/3 station I know.",
            "Use a Broadway-7th Av line station such as '116 St-Columbia University', "
            "'Cathedral Pkwy (110 St)', '96 St', 'Times Sq-42 St'. Other lines aren't supported.",
        )
    d = _normalize_direction(direction)
    if d is None:
        return _error(f"Unknown direction '{direction}'.", "Use 'uptown', 'downtown' or 'both'.")
    if route and route not in ("1", "2", "3"):
        return _error(f"Route '{route}' isn't on this line.", "Use '1', '2', '3' or leave route empty.")
    try:
        arrivals = _arrivals(stop_id, d, route)
    except Exception as e:  # network error or a garbled protobuf
        return _error(f"MTA real-time feed unavailable: {e}",
                      "Tell the user live times are unavailable right now and suggest checking the MTA app.")
    for a in arrivals:
        a.pop("_ts")
    result = {
        "station": STATIONS[stop_id],
        "now": _now().strftime("%-I:%M %p"),
        "arrivals": arrivals[: max(1, min(limit, 12))],
    }
    if not arrivals:
        result["note"] = ("No trains are scheduled in the live feed. Service may be suspended "
                          "or rerouted here; check get_subway_alerts.")
    return json.dumps(result)


def get_subway_alerts(route: str = "1") -> str:
    """Active MTA delays, suspensions and planned work for one subway route."""
    route = route.upper().strip().removesuffix(" TRAIN")
    try:
        r = requests.get(MTA_ALERTS_FEED, headers=HEADERS, timeout=10)
        r.raise_for_status()
        entities = r.json().get("entity", [])
    except (requests.RequestException, ValueError) as e:
        return _error(f"MTA alerts feed unavailable: {e}", "Tell the user and suggest the MTA app or mta.info.")

    now = time.time()
    alerts = []
    for ent in entities:
        alert = ent.get("alert", {})
        if not any(ie.get("route_id") == route for ie in alert.get("informed_entity", [])):
            continue
        periods = alert.get("active_period", [])
        active = not periods or any(p.get("start", 0) <= now <= (p.get("end") or now + 1) for p in periods)
        if not active:
            continue
        text = next(
            (t["text"] for t in alert.get("header_text", {}).get("translation", []) if t.get("language") == "en"),
            "",
        )
        kind = alert.get("transit_realtime.mercury_alert", {}).get("alert_type", "Alert")
        alerts.append({"type": kind, "summary": text[:400]})

    # Real-time problems first, planned work after.
    alerts.sort(key=lambda a: a["type"] in ("Planned Work", "Special Schedule", "Station Notice"))
    return json.dumps({
        "route": route,
        "checked_at": _now().strftime("%-I:%M %p"),
        "active_alerts": alerts[:8],
        "status": "Good service - no active alerts" if not alerts else f"{len(alerts)} active alert(s)",
    })


def estimate_walk(origin: str, destination: str) -> str:
    """Walking distance and time between two places around Columbia / NYC."""
    try:
        a = _resolve_place(origin)
        b = _resolve_place(destination)
    except requests.RequestException as e:
        return _error(f"Geocoding service failed: {e}", "Retry with a campus building name or a street corner.")
    missing = [p for p, hit in ((origin, a), (destination, b)) if hit is None]
    if missing:
        return _error(
            f"Couldn't find {missing} in NYC.",
            "Try a campus building (Butler, Lerner, Mudd, SIPA, Barnard...) or an address like "
            "'Broadway and 110th St, New York'.",
        )
    meters, minutes = _walk_minutes(a[:2], b[:2])
    return json.dumps({
        "from": a[2], "to": b[2],
        "distance_miles": round(meters / 1609, 2),
        "walk_minutes": math.ceil(minutes),
        "jog_minutes": math.ceil(meters / JOG_M_PER_MIN),
        "note": "Estimate based on straight-line distance adjusted for the street grid.",
    })


def catch_the_train(start: str, direction: str, station: str = "116 St-Columbia University",
                    route: str = "") -> str:
    """For each upcoming train: can you walk to it, do you need to run, or is it gone?"""
    stop_id = _find_station(station)
    if not stop_id:
        return _error(f"'{station}' isn't a 1/2/3 station I know.",
                      "Use e.g. '116 St-Columbia University' or 'Cathedral Pkwy (110 St)'.")
    d = _normalize_direction(direction)
    if not d:
        return _error(f"Need a direction, got '{direction}'.",
                      "Ask the user if they're going 'uptown' or 'downtown', then retry.")
    try:
        origin = _resolve_place(start)
    except requests.RequestException as e:
        return _error(f"Geocoding failed: {e}", "Retry with a campus building name.")
    if origin is None:
        return _error(f"Couldn't find '{start}'.", "Ask the user which building or corner they're at.")
    station_coords = PLACES.get(STATIONS[stop_id].lower())
    if station_coords is None:
        try:
            hit = _resolve_place(STATIONS[stop_id] + " subway station, Manhattan")
        except requests.RequestException:
            hit = None
        if hit is None:
            return _error("Can only plan walks to stations near campus.",
                          "Use 116 St, Cathedral Pkwy (110 St) or 125 St.")
        station_coords = hit[:2]

    meters, walk_min = _walk_minutes(origin[:2], station_coords)
    jog_min = meters / JOG_M_PER_MIN
    try:
        arrivals = _arrivals(stop_id, d, route)[:5]
    except Exception as e:
        return _error(f"MTA real-time feed unavailable: {e}", "Tell the user live times are unavailable.")

    now = time.time()
    plan = []
    for a in arrivals:
        mins = (a["_ts"] - now) / 60
        slack = mins - walk_min - STATION_BUFFER_MIN
        if slack >= 2:
            verdict, leave = "easy walk", a["_ts"] - (walk_min + STATION_BUFFER_MIN + 1) * 60
        elif slack >= 0:
            verdict, leave = "leave right now", now
        elif mins - jog_min - STATION_BUFFER_MIN >= 0:
            verdict, leave = "only if you run", now
        else:
            verdict, leave = "you'll miss it", None
        plan.append({
            "route": a["route"], "arrives_at": a["arrives_at"], "minutes_away": a["minutes_away"],
            "verdict": verdict, "leave_by": _clock(leave) if leave else None,
        })
    result = {
        "from": origin[2], "station": STATIONS[stop_id],
        "direction": "uptown" if d == "N" else "downtown",
        "walk_to_station_minutes": math.ceil(walk_min),
        "now": _now().strftime("%-I:%M %p"),
        "trains": plan,
    }
    if not plan:
        result["note"] = "No upcoming trains in the live feed; check get_subway_alerts for suspensions."
    return json.dumps(result)


def get_weather(place: str = "Columbia") -> str:
    """Current conditions plus rain chance for the next few hours."""
    try:
        hit = _resolve_place(place)
        if hit is None:
            return _error(f"Couldn't find '{place}' in NYC.", "Use 'Columbia' or an NYC neighborhood.")
        lat, lon, name = hit
        data = requests.get(
            FORECAST_URL,
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                "hourly": "precipitation_probability",
                "forecast_hours": 4,
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                "timezone": "America/New_York",
            },
            timeout=10,
        ).json()
        current = data["current"]
    except (requests.RequestException, KeyError, ValueError) as e:
        return _error(f"Weather service failed: {e}", "Tell the user weather is unavailable right now.")
    rain = data.get("hourly", {}).get("precipitation_probability", [])
    return json.dumps({
        "place": name,
        "temp_f": current["temperature_2m"],
        "feels_like_f": current["apparent_temperature"],
        "precipitation_now_in": current["precipitation"],
        "wind_mph": current["wind_speed_10m"],
        "max_rain_chance_next_4h_pct": max(rain) if rain else None,
    })


# --- What the model sees ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_next_trains",
            "description": (
                "Live MTA arrival times for the 1, 2 and 3 trains at a Broadway-7th Av line station "
                "(e.g. 116 St-Columbia University, the default). Use for 'when's the next train'. "
                "Only covers the 1/2/3 line."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "station": {"type": "string", "description": "Station name, e.g. '116 St-Columbia University', '96 St', 'Times Sq-42 St'."},
                    "direction": {"type": "string", "enum": ["uptown", "downtown", "both"], "description": "Uptown = toward the Bronx, downtown = toward Midtown/Downtown."},
                    "route": {"type": "string", "enum": ["", "1", "2", "3"], "description": "Filter to one route, or empty for all."},
                    "limit": {"type": "integer", "description": "Max arrivals to return (1-12, default 6)."},
                },
                "required": ["station"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_subway_alerts",
            "description": (
                "Active MTA service alerts (delays, suspensions, reroutes, planned weekend work) for one "
                "subway route. Call before recommending a subway trip, or when trains seem missing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {"type": "string", "description": "Subway route letter or number, e.g. '1', '2', 'A', 'L'. Buses aren't supported."},
                },
                "required": ["route"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_walk",
            "description": (
                "Walking distance and time (and jogging time) between two places. Knows Columbia/Barnard "
                "buildings by name (Butler, Lerner, John Jay, Mudd, SIPA, Pupin, Barnard, Manhattanville...) "
                "and can geocode other NYC addresses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Starting place, e.g. 'Butler Library' or '350 W 110th St'."},
                    "destination": {"type": "string", "description": "Destination, e.g. 'Mudd' or 'Hungarian Pastry Shop'."},
                },
                "required": ["origin", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "catch_the_train",
            "description": (
                "Combines walking time from where the user is with live arrivals to say, for each of the "
                "next trains, whether they can walk, must run, or will miss it, and when to leave. Use when "
                "the user asks 'can I make the next train' or 'when should I leave'. Needs a direction; ask "
                "the user if unclear."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Where the user is now, e.g. 'Mudd' or 'John Jay'."},
                    "direction": {"type": "string", "enum": ["uptown", "downtown"]},
                    "station": {"type": "string", "description": "Station to board at. Default '116 St-Columbia University'; '125 St' and 'Cathedral Pkwy (110 St)' also work."},
                    "route": {"type": "string", "enum": ["", "1", "2", "3"], "description": "Optional route filter."},
                },
                "required": ["start", "direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current temperature, feels-like, wind, and max rain chance over the next 4 hours near a NYC place. Use for umbrella / walk-vs-train decisions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "A campus building or NYC place. Default 'Columbia'."},
                },
                "required": [],
            },
        },
    },
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {
    "get_next_trains": get_next_trains,
    "get_subway_alerts": get_subway_alerts,
    "estimate_walk": estimate_walk,
    "catch_the_train": catch_the_train,
    "get_weather": get_weather,
}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return _error(f"Unknown tool '{name}'.", f"Available tools: {list(TOOL_MAP)}")
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return _error(f"Bad arguments for {name}: {e}", "Check the tool's parameter names and retry.")
    except Exception as e:  # last line of defense: a bug in a tool shouldn't 500 the chat
        return _error(f"{name} failed unexpectedly: {type(e).__name__}: {e}")
