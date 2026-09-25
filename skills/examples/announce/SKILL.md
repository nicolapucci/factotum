---
name: announce
description: Say something out loud on a device, such as that dinner is ready.
examples:
  - tell the kitchen that dinner is ready
  - announce on the living room display that the guests have arrived
  - say that it is time to go
  - annuncia in cucina che la cena è pronta
  - dì in salotto che è ora di andare
target: system
parameters:
  message: {type: string, description: "what to say, as the person said it"}
required: [message]
effect: reversible
---
Have the device say the message out loud: one `speak` command, with the message as its text. The
device is the one that was named, or the one that was spoken to when none was. There is nothing to
reach on the network, so nothing is granted.
