---
name: movie-scene
description: Dim the lights of the living room for watching a movie.
examples:
  - set the movie scene
  - dim the lights for the movie
  - it is movie time
  - prepara la scena per il film
  - abbassa le luci, guardiamo un film
target: none
permissions:
  network: [homeassistant.local]
  secrets: [HA_TOKEN]
effect: reversible
---
The lights are managed by Home Assistant, at `homeassistant.local` on port 8123 (change it, here and
in the permissions above, to where yours is). Every call is a POST with the header
`Authorization: Bearer TOKEN` and a JSON body.

1. Switch the ceiling light off: POST `http://homeassistant.local:8123/api/services/light/turn_off`
   with `{"entity_id": "light.living_room_ceiling"}`.
2. Switch the backlight of the television on at 20 percent: POST
   `http://homeassistant.local:8123/api/services/light/turn_on` with
   `{"entity_id": "light.tv_backlight", "brightness_pct": 20}`.

Say that the movie scene is set.
