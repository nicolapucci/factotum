---
name: volume
description: Set the volume of the device that is being spoken to, to a level the person says.
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
Changes the volume of a system device to a level. The script does not reach the device: it answers
with a command (`set_volume`), and the platform runs it on the device once the checks are made.

It is for a level that the person says. To turn the volume up or down without one ("alza il
volume"), see the `volume-change` skill: a manifest cannot say "this or that is required", so the
two are two skills, and each asks for what it cannot do without.
