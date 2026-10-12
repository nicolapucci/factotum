---
name: volume-change
description: Turn the volume of the device that is being spoken to up or down, by an amount if the person says one.
examples:
  - turn the volume up
  - turn it down a bit
  - make it louder
  - turn the volume down by 20
  - alza il volume
  - abbassa il volume di 20
  - più forte
target: system
parameters:
  direction: {type: string, enum: ["up", "down"], description: "whether the volume is to go up or to go down"}
  amount: {type: integer, minimum: 1, maximum: 100, description: "by how much, in percent, when the person says it (leave it out when they do not)"}
required: [direction]
effect: reversible
entrypoint: scripts/run.py
---
Turns the volume of a system device up or down. The script does not reach the device: it answers
with a command (`adjust_volume`), and the platform runs it on the device once the checks are made.

A person who does not say by how much ("alza il volume") gets the usual step, 10 points of the volume:
the model is not asked for a number and makes none up, and the skill decides what "a bit" is. To
set the volume to a level that is said ("set the volume to 30"), see the `volume` skill.
