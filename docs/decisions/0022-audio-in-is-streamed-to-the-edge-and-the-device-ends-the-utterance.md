# 0022. Audio in: a stream of chunks to the edge, and the device ends the utterance

Status: accepted, 2026-10-05.

## Context

Voice is a clip in each direction: a device records after its wake word, sends one `audio_utterance`
frame (the recording, base64, in a JSON text frame of up to 4 MiB) and waits for the turn and the
spoken answer to finish before it listens again ([ADR
0021](0021-voice-wake-word-on-the-device-speech-through-the-gateway.md)). That record named what
comes next: a real stream of audio over the edge's WebSocket, and a person speaking over an answer
interrupting it where it is. This one decides the stream going **in**, which is also what a client
that tests the whole stack should speak, so that the client that will be on a Raspberry Pi and the
one that tests it differ in where the sound comes from and in nothing else.

What the runs with real speech (`e2e/tests/test_e2e_speech.py`) showed about the ground this stands
on:

- **Whisper is not a streaming recognizer here.** The gateway's `stt` role takes a whole clip, and
  the model reads a window of 30 seconds whatever the clip is: 2 to 4.5 s on a processor, 0.4 to
  1.6 s on the card, for a command of a second. A stream does not make that shorter. What it does
  give is the *moment* the edge learns that somebody is speaking (to stop the answer that is being
  given, as the person speaks and not when they have finished), no frame of the size of a whole
  recording (4 MiB is a limit made for it), and the place to put what comes after: partial
  transcripts, and a decision about where speech ends that is not the device's.
- **Where speech ends** is today a device's decision, made by what is on it: the wake word, then
  silence (`MicrophoneInput`: 1.2 s of quiet, or 12 s at most). Deciding it on the server needs a
  model that tells speech from silence, on the edge or through the gateway; the edge has no model and
  holds no key, and a model is the gateway's ([ADR
  0008](0008-the-model-gateway-is-the-only-place-that-knows-models.md)).

## Decision

- **Three new frames from a device to the edge**, in the JSON text frames that are the whole of the
  protocol, additive to version 1 (a device that sends a clip keeps working, and so does an edge that
  is sent one):

  | Frame | Says |
  | --- | --- |
  | `audio_start` | `id`, `format` (`pcm_s16le`: 16-bit little-endian samples, one channel, no container), `sample_rate` (16000 is what the models read; it may be any rate of `AudioPayload`). A person has started talking to the device |
  | `audio_chunk` | `id`, `seq` (0, 1, 2... with no gap), `data` (the samples, base64, at most 64 KiB: two seconds at 16 kHz; 100 to 200 ms is what a device sends) |
  | `audio_end` | `id`: the person has stopped talking |

- **A stream is an utterance.** Its `id` is in the namespace of `utterance` and `audio_utterance`,
  and what answers it (`heard`, `part`, `turn_ended`, an `error` with `ref`) says that id. **Every
  stream the edge accepts ends with exactly one `turn_ended`**: `finished`, `failed` with a code, or
  `cancelled`.
- **`audio_start` interrupts the answer to the last utterance at once**, not when the stream ends:
  that is the whole of speaking over an answer. The edge does what it does for a new `utterance`
  today (stops reading the conversation's stream, which stops the model, and sends `turn_ended
  cancelled` for the old one). A stream that was still open ends `cancelled` too, as it does for an
  `utterance`, an `audio_utterance` or a `cancel` of its `id`. A device that wants to be interrupted
  by a wake word heard while it plays an answer stops its own playback and sends `audio_start`; what
  is played is the device's.
- **The device says where speech ends** (`audio_end`: its own silence detection, a button released,
  a maximum), as it decides today. The edge does not listen for silence in this slice: an edge that
  did would need a model, which is the gateway's to name (a `vad` role, if it comes to that), and
  nothing here prevents it. The stream is also what a server-side decision would be made on.
- **At `audio_end` the edge puts the samples in a WAV header and asks the gateway to transcribe
  them**, as it does for a clip, and runs the text through the same turn. A recording that comes out
  blank, and a stream with not one sample in it, end the turn `failed` (`nothing_heard`). The gateway
  does not change.
- **One stream at a time per connection, and bounded.** A second `audio_start` while one is open
  drops the first (its id ends `cancelled`) and opens the new one. A stream is dropped, and its id
  ends `failed` with the code, when it goes over what a clip may hold (`audio_too_long`: 2 MiB less
  the header of the WAV, about 65 s of 16 kHz), when no chunk comes for
  `FACTOTUM_EDGE_STREAM_STALL_S` (default 5 s, `stream_stalled`: a device that died in the middle of
  a sentence), when a chunk is out of order (`stream_out_of_order`) or when one is over its size
  (`chunk_too_big`: a chunk bigger than the limit is still a frame, so that the stream can be ended
  for it and the connection kept). A chunk or an end for an id that is not the open stream is an
  `error` frame with `ref` (`unknown_stream`) and nothing else happens: a late frame of a stream
  that was dropped does not drop the one that is open. A device without `audio_in` is refused
  (`capability_missing`), and so is one when the edge has no speech (`speech_not_configured`), as
  for a clip, **before anything is interrupted**: a person who was not heard does not stop the
  answer that is going on. Chunks count as the device saying something, for its idle timeout; a ping
  does not keep a stalled stream alive.
- **A `heard` frame from the edge** says what was transcribed (`utterance_id`, `text`), before the
  answer, for a clip as well as for a stream: a screen can show what the assistant understood, and a
  client that tests the stack can say what a recording was taken for without looking inside the
  services. It is not a part of the answer and is not filtered by capabilities: it is sent to every
  device that sent a recording, which is the only kind of device that can have one.
- **The answer is not streamed in this slice.** A `speech` part carries one clip for a sentence, as
  today: a sentence is short, and the first sound still comes after the first sentence. Chunks of
  output audio are a later, additive change that this does not prevent.
- **The clients.** `DeviceClient.say_audio_stream` takes an `AudioStream` (the rate, and the chunks
  as they come) and sends the start when the person begins, a chunk for each piece and the end
  when there is no more, and goes no further once the edge has ended the stream. The real
  `MicrophoneInput` produces an `AudioStream` as it captures what follows the wake word, and a
  `WavFileInput` produces one from a file, in the same chunks, as fast as the edge takes them or at
  the pace of a microphone: that is the client that tests the stack. `factotum-terminal` gets
  `--wav-in`, `--wav-out` and `--realtime` for a run with no hardware, and the end-to-end tests run
  every voice scenario both ways: the recording whole, and streamed.

## Consequences

- **A recording still has two ways in**, and a clip is the simpler one: a device that does not
  stream, and every test that does not care, send `audio_utterance`. The limit of a frame stays 4 MiB
  for it; a stream never gets near it.
- **What a stream does not do yet**, and what is left for later records: partial transcripts (the
  model of the gateway takes a clip), the edge deciding where speech ends, chunks of output audio,
  and a device hearing a wake word over its own voice (the echo of the speaker in the microphone is
  the device's to cancel, and is the first thing a real Raspberry Pi will have to solve).
- **The latency of an answer is the end of speech plus the transcription**, as it is with a clip;
  what is gained is where the interruption happens and the room for what follows.
- **A device that was only ever sent frames it knows** is not affected, except by `heard`: the edge
  now sends it to a device that sent a recording, so the terminal client learns to render it, and a
  client written before it would call it a frame that is not the protocol. The only client written
  against the edge is the terminal's, changed in the same commits.
- **What the edge keeps for a stream** is its samples, at most what a clip may hold, until it ends
  or is dropped: nothing is written to disk and no audio is logged.

## Alternatives considered

- **Binary WebSocket frames for the chunks.** Smaller on the wire (base64 is a third bigger: 43 KB/s
  at 16 kHz where 32 KB/s would do), and a second codec in every wire and every client for it. A
  LAN does not feel the difference; a frame type can later be carried in both ways.
- **The edge decides where speech ends.** The right place for a device that only streams, and a
  model that the edge does not have (above). It is for a later record, on top of this stream.
- **A capability `audio_stream`** to register for the devices that may stream. A device that has a
  microphone may send a recording in either way; a second thing to pair and to configure for no
  protection that `audio_in` does not already give.
- **A second WebSocket for audio**, or WebRTC. A second connection to authenticate and to keep
  together with the first, for what one connection with small frames does; WebRTC for a LAN and a
  protocol of JSON text frames is a different system.

## What was built, and what building it found

In four steps: the frames, the edge's session and its limits (`factotum.contracts.audio`,
`factotum.contracts.edge`, `factotum.edge.session`); the terminal (`say_audio_stream`,
`AudioStream`, the streaming microphone, `WavFileInput` and `WavFileOutput`, the three options and
`[heard]`); the end-to-end tests in both ways with a person who speaks over an answer; and
`docs/edge.md`. The format of a stream is a type of its own (`StreamFormat`), so that an
`AudioPayload` stays what the models read (a WAV file), and the limit of a stream is what makes a
clip of it that is still within `MAX_AUDIO_BYTES`.

The tests found four things nobody had looked at:

- **A recording that was given up on while it was being transcribed ended its turn with no word at
  all** (the device waited for an end that nobody was going to send). It ends `cancelled`, for a clip
  as well as for a stream.
- **A client waited for ever for the answer to an utterance that the edge had said it did not
  take** (no microphone, no speech): the wait now ends as a failed answer with the edge's code.
- **A run over several files that lost its connection said the next file into it** and waited for
  its answer: what is heard in the moment the connection ends is not said.
- **The services that the end-to-end tests put together hand a response over when it is whole**
  (`httpx.ASGITransport` does not stream), so nothing can be seen there of an answer that is being
  written: a held answer is cut and its model let go of, which is what is shown, and the sentence
  by sentence arrival is shown on the real sockets of the edge's own tests.
