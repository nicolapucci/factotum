---
name: thermostat-set
description: Set the target temperature of the heating of the home, for the whole home or for one room.
examples:
  - set the thermostat to 21 degrees
  - make the bedroom 19 degrees
  - metti il termostato a 22 gradi
  - imposta la temperatura della cucina a 20
target: none
parameters:
  temperature: {type: integer, minimum: 5, maximum: 30, description: "the temperature, in degrees Celsius"}
  room: {type: string, description: "the room, as the person said it"}
required: [temperature]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
