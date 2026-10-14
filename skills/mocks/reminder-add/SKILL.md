---
name: reminder-add
description: Remind the person of something, now or at a time they say.
examples:
  - remind me to call the dentist
  - remind me to take the bins out tomorrow
  - ricordami di chiamare il dentista
  - ricordami di pagare la bolletta venerdì
target: none
parameters:
  what: {type: string, description: "what to remind of, as the person said it"}
  when: {type: string, description: "when to remind, as the person said it"}
required: [what]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
