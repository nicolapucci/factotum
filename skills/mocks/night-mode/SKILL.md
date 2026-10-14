---
name: night-mode
description: Turn the night mode of the home on or off.
examples:
  - turn on the night mode
  - night mode off
  - attiva la modalità notte
  - spegni la modalità notte
target: none
parameters:
  enabled: {type: boolean, description: "true to turn the night mode on, false to turn it off"}
required: [enabled]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
