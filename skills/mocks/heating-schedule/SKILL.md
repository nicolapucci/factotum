---
name: heating-schedule
description: Turn the heating on from one time to another.
examples:
  - heating from 7:30 to 9:15
  - turn the heating on from six until eight in the evening
  - accendi il riscaldamento dalle sei alle otto
  - riscaldamento dalle sette di mattina alle dieci di sera
target: none
parameters:
  start:
    type: string
    description: "when the heating comes on"
    formats:
      - time: "HH:mm"
  end:
    type: string
    description: "when the heating goes off"
    formats:
      - time: "HH:mm"
required: [start, end]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`). It takes
both times as `HH:mm`.
