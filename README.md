# Lion Line: a Columbia commute agent

**Deployed at:** https://lion-line-git-754123787483.europe-west1.run.app

Lion Line is a web chat agent for Columbia and Barnard students who take the subway from school. It helps answer practical questions when you're trying to leave campus: Can I catch the next train? Should I run? Is the 1 running normally? Do I need an umbrella? Where can I grab coffee?

The agent uses live data instead of making guesses. Each tool call also appears in the chat as an expandable card, so you can see the arguments sent to the tool and the result it returned. It also keeps track of the conversation. For example, if you tell it you're in Mudd and heading downtown, you don't have to repeat that when asking a follow-up question.

## Tools

All data sources are free and need no API key.

| Tool | What it does | Data source |
|---|---|---|
| `catch_the_train` | Combines your walk time to the station with live arrivals and gives a verdict per train: *easy walk (leave by 2:41)*, *leave right now*, *only if you run*, or *you'll miss it*. | MTA GTFS-realtime + campus map |
| `estimate_walk` | Walking and jogging time between places. Knows ~30 Columbia/Barnard buildings and dining halls by name (Butler, Lerner, John Jay, Mudd, SIPA, Hewitt, Manhattanville…), and geocodes any other NYC address. | Built-in campus table + OpenStreetMap Nominatim |
| `get_next_trains` | Live 1/2/3 arrivals at any Broadway–7th Av line station, filterable by direction and route. | MTA GTFS-realtime (protobuf) |
| `get_subway_alerts` | Active delays, suspensions and planned work for any subway route. | MTA alerts feed |
| `get_weather` | Temperature, feels-like, wind and max rain chance over the next 4 hours. | Open-Meteo |
| `coffee_before_train` | Finds open cafes/food spots you can stop at and *still* catch a train: walk there, estimated wait, walk to the station, checked against live arrivals. Says when to leave and how much extra waiting the stop costs vs. going straight to the platform. | Hand-curated spot table (hours, price, our rating) + MTA GTFS-realtime |
| `sun_spots` | Next sunrise/sunset, a sky-quality outlook (vivid / decent / clean but plain / poor) from cloud layers and rain chance, feels-like temperature, and nearby viewpoints with walk time and when to leave. | Open-Meteo + hand-picked viewpoints |

All tools return JSON. If something goes wrong, the tool returns an error and a hint, such as retrying with a recognized campus building, asking the user for their direction, or letting the user know that the MTA feed is unavailable. This lets the agent handle bad inputs and API/network issues without crashing.

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
