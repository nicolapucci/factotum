"""Answer with the command that sets the volume (see the lamp skill for the protocol)."""

import json
import sys

level = json.load(sys.stdin)["params"]["level"]
print(
    json.dumps(
        {
            "message": f"Volume set to {level}.",
            "commands": [{"name": "set_volume", "level": level}],
        }
    )
)
