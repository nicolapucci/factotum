---
name: shopping-list-add
description: Add something to the shopping list.
examples:
  - add milk to the shopping list
  - put bread on the list
  - aggiungi il latte alla lista della spesa
  - metti le uova nella lista
target: none
parameters:
  item: {type: string, description: "what to add, as the person said it"}
required: [item]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
