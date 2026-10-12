"""Switch a lamp through Home Assistant.

The parameters arrive as JSON on standard input ({"params": ..., "device_params": ...}) and the
answer leaves as JSON on standard output ({"message": ..., "data": ..., "commands": [...]}, or
{"error": ...} for a skill that knows why it could not do it). The token is in the environment, by
the name the skill declares.
"""

import json
import os
import sys
import urllib.request

request = json.load(sys.stdin)
params, device = request["params"], request["device_params"]

service = "turn_on" if params["state"] == "on" else "turn_off"
body = {"entity_id": device["entity_id"]}
if service == "turn_on" and "brightness" in params:
    body["brightness_pct"] = params["brightness"]

call = urllib.request.Request(
    f"http://{device['host']}:8123/api/services/light/{service}",
    data=json.dumps(body).encode(),
    headers={
        "Authorization": f"Bearer {os.environ['HA_TOKEN']}",
        "Content-Type": "application/json",
    },
)
urllib.request.urlopen(call, timeout=8).read()
print(json.dumps({"message": f"The lamp is {params['state']}."}))
