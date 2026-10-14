---
name: sports-score
description: Say the result of the latest match of a football team.
examples:
  - how did Juventus do
  - what was the score of the Milan game
  - com'è finita l'ultima della Roma
  - risultato del Napoli
target: none
parameters:
  team: {type: string, description: "the team, as the person said it"}
required: [team]
effect: read_only
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
