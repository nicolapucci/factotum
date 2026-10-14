---
name: music-play
description: Play music on the device that is being spoken to, of an artist, a song or a genre if the person says one.
examples:
  - play some music
  - play Queen
  - metti della musica
  - suona un po' di jazz
target: system
parameters:
  query: {type: string, description: "what to play, as the person said it"}
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`).
