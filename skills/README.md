# Skills

A skill teaches Factotum to do something: a folder with a `SKILL.md` (YAML frontmatter, then
markdown) and, in script mode, the scripts it runs. The design is in
[`docs/design.md`](../docs/design.md#6-skills) and the way the actions service reads, reviews and
runs them is in [`docs/actions.md`](../docs/actions.md).

`examples/` has one skill for each way an action can be decided:

| Skill | Shows |
| --- | --- |
| `lamp` | a reversible action on an external device: done at once, one skill for every lamp |
| `garage-door` | an action on a sensitive device: a shared speaker may not, one that may asks for a yes |
| `weather` | a read-only action with no device, that reaches a service on the network it declared |
| `volume` | an action on the device that was spoken to, answered with a command for it |
| `restart-router` | an action that asks for an approval, which fails closed until there is a way to give one |

Point `FACTOTUM_ACTIONS_SKILLS_DIR` at a folder of skills (`skills/examples` to try them) and the
actions service installs what it finds. An installed skill does not run until an administrator has
reviewed it and enabled it: `POST /v1/skills/{id}/enable`.
