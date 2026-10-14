---
name: fan-speed
description: Set how fast the fan runs.
examples:
  - put the fan on high
  - turn the fan down to low
  - ventola alla massima velocità
  - metti la ventola a media
target: none
parameters:
  speed: {type: string, enum: ["low", "medium", "high"], description: "how fast the fan is to run"}
required: [speed]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
