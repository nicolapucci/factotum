---
name: meeting-add
description: Add a meeting or an appointment to the calendar, on a day and, if the person says, at a time.
examples:
  - add a meeting with Anna tomorrow at ten
  - put a dentist appointment on friday at three pm
  - aggiungi una riunione con Marco domani alle dieci
  - metti in calendario il pranzo con Luca il 20 novembre 2026
target: none
parameters:
  title:
    type: string
    description: "what the meeting is, as the person said it"
  day:
    type: string
    description: "the day of the meeting"
    formats:
      - date: "dd/MM/yy"
      - in_days: "+{n}d"
      - weekday: name
  at:
    type: string
    description: "when it starts"
    formats:
      - time: "HH:mm"
required: [title, day]
effect: reversible
---
A mock: it does nothing, and answers as its `mock.yaml` says (see `skills/mocks/README.md`). It takes
the day in the form that the person said it, as a date (`16/10/26`), as a number of days from today
(`+1d`) or as a day of the week (`monday`), and the time as `HH:mm`.
