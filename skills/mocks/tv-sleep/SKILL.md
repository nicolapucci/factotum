---
name: tv-sleep
description: Turn the television off after a length of time, as a sleep timer does.
examples:
  - turn the tv off in an hour and a half
  - switch off the television in 45 minutes
  - spegni la tv tra mezz'ora
  - spegni la televisione fra due ore
target: none
parameters:
  after:
    type: string
    description: "how long from now"
    formats:
      - duration: iso
required: [after]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`). It takes
the length as an ISO 8601 duration (`PT1H30M`).
