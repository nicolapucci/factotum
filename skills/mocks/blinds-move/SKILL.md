---
name: blinds-move
description: Open or close the blinds all the way.
examples:
  - open the blinds
  - close the blinds
  - apri le tapparelle
  - chiudi le tapparelle
target: none
parameters:
  action: {type: string, enum: ["open", "close"], description: "whether the blinds are to open or to close"}
required: [action]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
