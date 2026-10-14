---
name: coffee-brew
description: Make a coffee with the coffee machine.
examples:
  - make an espresso
  - make two lungos
  - fammi un caffè
  - prepara un americano
target: none
parameters:
  kind: {type: string, enum: ["espresso", "lungo", "americano"], description: "the kind of coffee"}
  cups: {type: integer, minimum: 1, maximum: 4, description: "how many cups"}
required: [kind]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
