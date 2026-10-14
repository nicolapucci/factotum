---
name: vacuum-clean
description: Start the robot vacuum cleaner, in one room if the person says which, or in the whole home.
examples:
  - start the vacuum
  - vacuum the kitchen
  - avvia l'aspirapolvere
  - pulisci il salotto
target: none
parameters:
  room: {type: string, enum: ["kitchen", "living room", "bedroom", "bathroom"], description: "the room to clean"}
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
