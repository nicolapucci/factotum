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
| `volume` | an action on the device that was spoken to, answered with a command for it: the volume set to a level that is said |
| `volume-change` | the same turned up or down: the person says by how much, or does not and the skill's step is used; a value that nobody said is never passed on |
| `restart-router` | an action that asks for an approval, which fails closed until there is a way to give one |
| `movie-scene` | *instructions*: two calls to Home Assistant with a token, in order, and what to say |
| `air-quality` | *instructions*: read-only, two calls where the second uses what the first answered |

What is not here is `announce`, which says something out loud on a device: it is a skill of the
platform, in code, that every tenant has without installing anything
([`docs/actions.md`](../docs/actions.md#skills-of-the-platform)). A skill of yours that gives a device
a `speak` (a command, no network, nothing granted) works the way the example `announce` used to, and
the edge makes the voice of it.

Point `FACTOTUM_ACTIONS_SKILLS_DIR` at a folder of skills (`skills/examples` to try them) and the
actions service installs what it finds. An installed skill does not run until an administrator has
reviewed it and enabled it: `POST /v1/skills/{id}/enable`.
