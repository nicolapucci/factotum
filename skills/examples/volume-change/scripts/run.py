"""Answer with the command that turns the volume up or down (see the lamp skill for the protocol).

This skill is the interface between a model and the device: it passes on which way and, if the
person said it, by how much. How far "turn it up" goes when nobody says is the device's step."""

import json
import sys

params = json.load(sys.stdin)["params"]
command = {"name": "adjust_volume", "direction": params["direction"]}
message = f"Volume {params['direction']}."
if "amount" in params:
    command["amount"] = params["amount"]
    message = f"Volume {params['direction']} by {params['amount']}."
print(json.dumps({"message": message, "commands": [command]}))
