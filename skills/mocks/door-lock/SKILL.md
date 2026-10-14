---
name: door-lock
description: Lock or unlock the front door of the home.
examples:
  - lock the front door
  - unlock the door
  - chiudi a chiave la porta
  - apri la serratura
target: none
parameters:
  action: {type: string, enum: ["lock", "unlock"], description: "whether the door is to lock or to unlock"}
required: [action]
effect: irreversible
confirm: ask
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
