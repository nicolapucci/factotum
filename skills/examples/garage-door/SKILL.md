---
name: garage-door
description: Open or close the garage door.
examples:
  - open the garage door
  - close the garage
  - apri il garage
  - chiudi la porta del garage
target: external
parameters:
  action: {type: string, enum: ["open", "close"], description: whether the door is to open or to close}
required: [action]
device_parameters:
  entity_id: {type: string, description: the entity of the door in Home Assistant}
  host: {type: string, description: the host name of Home Assistant}
permissions:
  network: ["${device.host}"]
  secrets: [HA_TOKEN]
effect: reversible
confirm: ask
entrypoint: scripts/run.py
---
Opens or closes the garage door through Home Assistant. The door is registered with a sensitivity
of `irreversible` (see `config/devices.example.yaml`), so whatever the skill declares an action on
it counts as irreversible: a device that may not start those refuses, and one that may asks for a
yes before the door moves.
