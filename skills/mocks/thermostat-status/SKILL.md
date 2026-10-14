---
name: thermostat-status
description: Say how warm it is inside the home, in a room if the person says which.
examples:
  - how warm is it at home
  - what is the temperature in the kitchen
  - quanti gradi ci sono in casa
  - che temperatura c'è in salotto
target: none
parameters:
  room: {type: string, description: "the room, as the person said it"}
effect: read_only
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
