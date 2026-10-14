---
name: tv-channel
description: Switch the television to a channel by its number.
examples:
  - put on channel 5
  - go to channel twelve
  - metti il canale 7
  - vai sul canale venti
target: none
parameters:
  channel: {type: integer, minimum: 1, maximum: 999, description: "the number of the channel"}
required: [channel]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
