"""The weather now in a city, from Open-Meteo (see the lamp skill for the protocol)."""

import json
import sys
import urllib.parse
import urllib.request


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.load(response)


place = json.load(sys.stdin)["params"]["place"]
found = get(
    "https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(place)
).get("results")
if not found:
    print(json.dumps({"message": f"I could not find {place}."}))
    sys.exit(0)

city = found[0]
now = get(
    "https://api.open-meteo.com/v1/forecast?current=temperature_2m,precipitation"
    f"&latitude={city['latitude']}&longitude={city['longitude']}"
)["current"]
message = f"In {city['name']} it is {round(now['temperature_2m'])} degrees" + (
    " and it is raining." if now["precipitation"] > 0 else "."
)
print(json.dumps({"message": message, "data": now}))
