"""Open or close a garage door through Home Assistant (see the lamp skill for the protocol)."""

import json
import os
import sys
import urllib.request

request = json.load(sys.stdin)
params, device = request["params"], request["device_params"]

service = "open_cover" if params["action"] == "open" else "close_cover"
call = urllib.request.Request(
    f"http://{device['host']}:8123/api/services/cover/{service}",
    data=json.dumps({"entity_id": device["entity_id"]}).encode(),
    headers={
        "Authorization": f"Bearer {os.environ['HA_TOKEN']}",
        "Content-Type": "application/json",
    },
)
urllib.request.urlopen(call, timeout=8).read()
verb = "opening" if params["action"] == "open" else "closing"
print(json.dumps({"message": f"The garage door is {verb}."}))
