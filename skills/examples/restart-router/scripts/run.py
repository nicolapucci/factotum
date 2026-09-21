"""Restart the router (see the lamp skill for the protocol)."""

import json
import os
import urllib.request

call = urllib.request.Request(
    "http://router.local/api/reboot",
    method="POST",
    headers={"Authorization": f"Bearer {os.environ['ROUTER_PASSWORD']}"},
)
urllib.request.urlopen(call, timeout=8).read()
print(json.dumps({"message": "The router is restarting."}))
