# Skills

A skill teaches Factotum to do something: a folder with a `SKILL.md` (YAML frontmatter, then
markdown) and, in script mode, the scripts it runs; in instructions mode there are none, and a model
follows the markdown. **How to write one is in [`docs/skills.md`](../docs/skills.md).** The design is
in [`docs/design.md`](../docs/design.md#6-skills) and the way the actions service reads, reviews and
runs them is in [`docs/actions.md`](../docs/actions.md).

`examples/` has one skill for each way an action can be decided:

| Skill | Shows |
| --- | --- |
| `lamp` | a reversible action on an external device: done at once, one skill for every lamp |
| `garage-door` | an action on a sensitive device: a shared speaker may not, one that may asks for a yes |
| `weather` | a read-only action with no device, that reaches a service on the network it declared |
| `volume` | an action on the device that was spoken to, answered with a command for it |
| `restart-router` | an action that asks for an approval, which fails closed until there is a way to give one |
| `announce` | *instructions*: say something on a device (a command, no network, nothing granted) |
| `movie-scene` | *instructions*: two calls to Home Assistant with a token, in order, and what to say |
| `air-quality` | *instructions*: read-only, two calls where the second uses what the first answered |

Point `FACTOTUM_ACTIONS_SKILLS_DIR` at a folder of skills (`skills/examples` to try them) and the
actions service installs what it finds. An installed skill does not run until an administrator has
reviewed it and enabled it: `POST /v1/skills/{id}/enable`.
