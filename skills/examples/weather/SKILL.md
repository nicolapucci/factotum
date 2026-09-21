---
name: weather
description: Say what the weather is like now in a city.
examples:
  - what is the weather like in Rome
  - is it raining in Milan
  - che tempo fa a Torino
target: none
parameters:
  place: {type: string, description: "the city, as the person said it"}
required: [place]
permissions:
  network: [geocoding-api.open-meteo.com, api.open-meteo.com]
effect: read_only
entrypoint: scripts/run.py
---
Looks a city up and reads its weather from Open-Meteo, which needs no key. It only reads, so
nothing is asked before it runs.
