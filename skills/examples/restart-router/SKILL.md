---
name: restart-router
description: Restart the router of the home. Everybody is disconnected for a couple of minutes.
examples:
  - restart the router
  - reboot the wifi
  - riavvia il router
target: none
permissions:
  network: [router.local]
  secrets: [ROUTER_PASSWORD]
effect: reversible
approval: required
entrypoint: scripts/run.py
---
Restarts the router. It is a skill that asks for an approval: somebody who is allowed to has to say
yes through a channel other than the conversation (ADR 0007). Until such a channel is set up the
answer is that it cannot be done, and nothing is restarted.
