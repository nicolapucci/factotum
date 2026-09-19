# 0005. System devices and external devices

Status: accepted, 2026-09-19

## Context

Factotum's own hardware and third-party IoT differ in how they are identified, how they are controlled
and how far they are trusted. Devices also differ from each other: some have a screen, some have no
microphone.

## Decision

**System devices** are Factotum hardware.

- They have a `hardware_id`, a stable label for the physical unit (a serial or MAC based value). It
  prevents duplicates and is not proof of identity, because a MAC can be forged in one line.
  Authentication uses a credential issued at pairing.
- They declare **capabilities** (`audio_in`, `audio_out`, `display`, `media_player`, `buttons`,
  `led`, `sensors`) and receive only commands from a small, versioned vocabulary that they can
  execute. They never receive code.
- `SystemDevice` offers one ready-made method per command and refuses a command the device cannot
  run.

**External devices** are registered by hand and have no built-in methods. Every way to act on them is
a skill the user binds explicitly. A **binding** carries the device's own parameters, so one skill
serves many devices. A device has a `sensitivity` that raises the effect of any action on it: the
effective effect is the highest of the skill's and the target's.

## Consequences

- Responses are made of typed parts that name the capability they need, so the edge adapts one answer
  to each device.
- A test ties the command vocabulary to the `SystemDevice` methods, so they cannot diverge.
- Code for hardware attached to a device runs on the server, not on the device: either a command is
  added to the vocabulary, or the skill calls an API the device exposes on the LAN.
- If Factotum hardware is ever manufactured, a factory serial with a per-device certificate installed
  in production is the right identity; pairing with a one-time code is enough for do-it-yourself units.
