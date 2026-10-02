# 0021. Voice: the wake word stays on the device, speech goes through the gateway

Status: accepted, 2026-10-02.

## Context

Slice 8 adds a basic voice pipeline: a device waits for a wake word, records what is said after
it, and speaks the answer back. Three things needed a place to live that nothing before this slice
had decided: where the wake word is detected, where speech-to-text and text-to-speech run, and
what shape audio takes on the wire. ADR 0013 already said that audio would be new frames, not a
change to the existing ones, and left the rest open.

A wake word is not a model a user picks the way a chat model or an embedding model is: it runs
continuously, on every frame of audio a microphone produces, and a round trip to a server for each
one would add latency and bandwidth for no benefit. Speech-to-text and text-to-speech are the
opposite: the gateway already has two roles for exactly this (`stt`, `tts`, in
`factotum.contracts.models.ModelRole` since slice 1), unused until now, and the principle that only
the gateway knows which model answers a role ("you choose the models") applies to a recording and
an answer precisely as it does to a chat message.

## Decision

- **The wake word is detected on the device, never through the gateway.** It has no model role and
  never will: a device's own wake-word engine decides when to start recording, and the edge and the
  gateway see nothing before that moment.
- **Speech-to-text and text-to-speech are the gateway's `stt` and `tts` roles**, served by two new
  providers that run in the gateway's own process, `whisper_local` (faster-whisper) and
  `piper_local` (Piper): no address, no key, chosen locally over a hosted provider so that nothing
  needs to be sent off-network for a transcription or a synthesis
  ([ADR 0010](0010-where-data-goes-is-the-users-choice-of-deployment-and-providers.md) still
  applies; this is the deployment's choice of provider, not a new policy). A device sends a
  recording as a new `audio_utterance` frame, parallel to `utterance`; the edge transcribes it and
  runs the result through the same turn an `utterance` would have. A `Speech` part gains an
  optional `audio` field, filled in by the edge when a device declares `audio_out`, left empty (the
  text still goes) when no `tts` binding exists.
- **Audio is carried whole, one clip per direction, in the existing JSON frames** (`AudioPayload`:
  format, sample rate, bytes as base64), not as a new binary frame type and not streamed. This is
  the simplest shape that works, and it is explicitly provisional: it is what ADR 0013 called "a new
  kind of part," not a reopening of the protocol itself.

## Consequences

- **A device effectively stops listening for its own wake word while an answer is being delivered**
  in this slice's terminal client: recording the next utterance only begins after the current one's
  turn, and its spoken answer, are both done. A real always-listening device does not need this
  limit, but building it now is unneeded complexity for what this slice is testing.
- `MAX_FRAME_BYTES` (`factotum.contracts.edge`) grew from 64 KB to 4 MB to fit a clip of speech
  base64-encoded in a JSON frame. It is bounded by `AudioPayload`'s own `MAX_AUDIO_BYTES` (about a
  minute of audio) and, in practice, by a client's own limit on how long it records after a wake
  word; a device that sent a crafted frame at the new ceiling is still only sending one frame.
- **What comes next is already named, not merely implied**: a future slice replaces the single clip
  with a real stream of audio over the edge's WebSocket, and lets a wake word heard while an answer
  is playing interrupt it where it is, not only between turns. Both are additive changes to the
  contracts and to the edge, the same kind ADR 0013 already described, and do not change this
  decision's placement of the wake word or of speech-to-text and text-to-speech.
