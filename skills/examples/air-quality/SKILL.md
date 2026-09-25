---
name: air-quality
description: Say how good the air is in a city.
examples:
  - how is the air quality in Rome
  - is the air clean in Milan
  - what is the air like in Turin today
  - com'è la qualità dell'aria a Torino
  - com'è l'aria a Milano
target: none
parameters:
  place: {type: string, description: "the city, as the person said it"}
required: [place]
permissions:
  network: [geocoding-api.open-meteo.com, air-quality-api.open-meteo.com]
effect: read_only
---
Open-Meteo needs no key.

First find the place: GET `https://geocoding-api.open-meteo.com/v1/search?count=1&name=PLACE`. The
answer has `results`, a list, and the first has `latitude` and `longitude`.

Then read the air: GET
`https://air-quality-api.open-meteo.com/v1/air-quality?current=european_aqi&latitude=LAT&longitude=LON`.
The answer has `current.european_aqi`, the European air quality index (the lower, the cleaner).

Say the index and the place, in the language of the message.
