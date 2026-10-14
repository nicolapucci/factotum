---
name: blinds-set
description: Open the blinds by a percentage, from 0 (shut) to 100 (fully open).
examples:
  - open the blinds to 60 percent
  - set the blinds to 30
  - tapparelle al 50 per cento
  - apri le tapparelle a metà
target: none
parameters:
  position: {type: integer, minimum: 0, maximum: 100, description: "how open the blinds are to be, in percent"}
required: [position]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
