---
name: oven-preheat
description: Preheat the oven to a temperature, with the fan, the grill or neither.
examples:
  - preheat the oven to 200
  - heat the oven to 180 with the fan
  - preriscalda il forno a 190
  - forno a 220 gradi
target: none
parameters:
  temperature: {type: integer, minimum: 50, maximum: 250, description: "the temperature, in degrees Celsius"}
  mode: {type: string, enum: ["fan", "grill", "static"], description: "how the oven heats"}
required: [temperature]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
