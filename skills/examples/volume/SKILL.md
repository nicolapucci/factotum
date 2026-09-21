---
name: volume
description: Set the volume of the device that is being spoken to.
examples:
  - set the volume to 30
  - volume at fifty percent
  - alza il volume a 70
  - abbassa il volume a 20
target: system
parameters:
  level: {type: integer, minimum: 0, maximum: 100, description: "the volume, in percent"}
required: [level]
effect: reversible
entrypoint: scripts/run.py
---
Changes the volume of a system device. The script does not reach the device: it answers with a
command (`set_volume`), and the platform runs it on the device once the checks are made.
