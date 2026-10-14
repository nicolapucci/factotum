---
name: fridge-temperature
description: Set the temperature of the fridge, in degrees Celsius.
examples:
  - set the fridge to 4 degrees
  - make the fridge colder
  - metti il frigorifero a 5 gradi
  - temperatura del frigo
target: none
parameters:
  temperature: {type: number, minimum: 1, maximum: 8, description: "the temperature, in degrees Celsius, which may have a decimal"}
required: [temperature]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
