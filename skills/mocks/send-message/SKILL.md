---
name: send-message
description: Send a text message to a person.
examples:
  - send a message to Anna
  - text Paolo that I am late
  - manda un messaggio a Marco
  - scrivi a Giulia che arrivo
target: none
parameters:
  to: {type: string, description: "who to send it to, as the person said it"}
  text: {type: string, description: "what the message says, as the person said it"}
required: [to, text]
effect: irreversible
confirm: ask
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
