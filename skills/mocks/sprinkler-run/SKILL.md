---
name: sprinkler-run
description: Water the garden with the sprinkler for a length of time.
examples:
  - water the garden for half an hour
  - start the sprinkler for 90 seconds
  - avvia l'irrigazione per dieci minuti
  - innaffia il giardino per un'ora e mezza
target: none
parameters:
  length:
    type: integer
    minimum: 10
    maximum: 7200
    description: "how long to water for"
    formats:
      - duration: seconds
required: [length]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`). It takes
the length as a whole number of seconds.
