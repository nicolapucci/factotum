---
name: tv-record
description: Record a television programme on a channel, from a time, for a length of time if the person says.
examples:
  - record channel five tonight at nine
  - record channel 7 at half past eight for an hour and a half
  - registra il canale 3 dalle 20:15 per due ore
  - registra il canale cinque alle otto
target: none
parameters:
  channel:
    type: integer
    minimum: 1
    maximum: 999
    description: "the number of the channel"
  at:
    type: string
    description: "when the programme starts"
    formats:
      - time: "HH:mm"
  length:
    type: integer
    minimum: 1
    maximum: 480
    description: "how long to record, when the person says"
    formats:
      - duration: minutes
required: [channel, at]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`). It takes
the time as `HH:mm` and the length as a whole number of minutes.
