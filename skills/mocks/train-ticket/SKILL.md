---
name: train-ticket
description: Book a train ticket to a city, on a date.
examples:
  - book a train ticket to Milan on 5 December 2026
  - train ticket to Rome on the 16th of October 2026
  - biglietto del treno per Roma il 16 ottobre 2026
  - prenota il treno per Torino il 3/11/2026
target: none
parameters:
  destination:
    type: string
    description: "the city to go to, as the person said it"
  date:
    type: string
    description: "the day of the trip"
    formats:
      - date: "yyyy-MM-dd"
required: [destination, date]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`). It takes
the day only as a calendar date (`2026-10-16`): *tomorrow* needs today's date, which the platform
does not have, so a person who says it is asked for the day.
