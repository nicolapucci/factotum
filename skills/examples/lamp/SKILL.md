---
name: lamp
description: Turn a lamp on or off, or set how bright it is.
examples:
  - turn on the living room lamp
  - switch off the lamp
  - set the bedroom lamp to 30 percent
  - accendi la luce del salotto
  - spegni la lampada
target: external
parameters:
  state: {type: string, enum: ["on", "off"], description: whether the lamp is to be on or off}
  brightness: {type: integer, minimum: 0, maximum: 100, description: "how bright it is to be, in percent"}
required: [state]
device_parameters:
  entity_id: {type: string, description: the entity of the lamp in Home Assistant}
  host: {type: string, description: the host name of Home Assistant}
permissions:
  network: ["${device.host}"]
  secrets: [HA_TOKEN]
effect: reversible
confirm: never
entrypoint: scripts/run.py
---
Switches a lamp through the REST API of Home Assistant. The same skill serves every lamp: each is
bound to it with the entity and the host that are its own (see `config/devices.example.yaml`).
Turning a lamp on or off can be undone, so nothing is asked before it is done.
