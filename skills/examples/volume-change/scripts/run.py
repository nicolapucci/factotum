"""Answer with the command that turns the volume up or down (see the lamp skill for the protocol).

The person says by how much, or does not: when they do not, the volume goes by the usual step."""

import json
import sys

STEP = 10
"""How much "turn it up" is, in points of the volume, when nobody says."""

params = json.load(sys.stdin)["params"]
amount = params.get("amount", STEP)
delta = amount if params["direction"] == "up" else -amount
print(
    json.dumps(
        {
            "message": f"Volume {'up' if delta > 0 else 'down'} by {amount}.",
            "commands": [{"name": "adjust_volume", "delta": delta}],
        }
    )
)
