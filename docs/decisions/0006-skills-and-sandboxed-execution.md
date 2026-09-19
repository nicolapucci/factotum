# 0006. Skills and sandboxed execution

Status: accepted, 2026-09-19

## Context

Users must be able to teach Factotum an action with a markdown document and, optionally, code. A
model decides when it runs, and text from the web or from a document can try to steer that model.

## Decision

- A skill is a folder with a `SKILL.md` (YAML frontmatter and markdown) and optional scripts. The
  frontmatter is a validated `SkillManifest` that declares parameters, permissions (network hosts,
  secrets, linked accounts) and risk (`effect`, `confirm`, `approval`). The author must state the
  `effect`; nothing undeclared is available to the skill.
- **Script mode**: the script runs in a separate `runner` container, gets its parameters on stdin
  and answers `{message, data, commands}` on stdout. Commands for devices leave as data that the
  platform runs after the checks, so the sandbox needs no channel back into the platform.
  **Instructions mode**: the model follows the markdown using only the platform's primitives within
  the declared permissions; the plan is executed by the platform, which resolves secrets.
- The runner is non-root, has a read-only filesystem and CPU, memory and time limits, and sits on an
  internal network whose only exit is a proxy with a per-skill allowlist. In v1 scripts speak HTTP(S)
  only.
- Skills are matched by embedding descriptions and examples; a model picks the skill and extracts the
  parameters with structured output validated against the skill's schema.
- Text from the web or from documents that entered a turn forces a confirmation before any action
  with an effect.

## Consequences

- Isolation is at process level. It suits code the user wrote or trusts, not code from strangers, so
  a multi-tenant cloud needs a stronger sandbox (gVisor, Kata) or only instructions-mode skills.
- The `SkillRunner` interface lets a stronger backend replace the first one without touching the rest.
- YAML 1.1 reads an unquoted `on` or `off` as a boolean; the manifest validation turns that mistake
  into a clear error.
