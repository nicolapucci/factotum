# 0023. Audio out: the voice of an answer is streamed, from the gateway through the edge to the device

Status: accepted, 2026-10-05.

## Context

[ADR 0022](0022-audio-in-is-streamed-to-the-edge-and-the-device-ends-the-utterance.md) streamed what
a person says *in* and left the answer for later: a `speech` part still carried one clip for a
sentence, made whole by the gateway (`POST /v1/synthesize`, one JSON answer), and the edge read the
next event of the conversation only once the sound of the sentence before was made and sent. A client
that plays that has to hold a clip for each sentence and chain them, cannot start before a sentence
is all said, and cannot stop a sentence it has begun to receive.

What was measured, with the real Italian voice of Piper on a processor (`FACTOTUM_LIVE_TESTS=1`,
`test_live_speech.py`):

- **Piper makes a sentence in 0.03 to 0.3 s and says it in two to five**, and its own API
  (`PiperVoice.synthesize`) is already a stream of one chunk of audio for each sentence of the text,
  after turning the whole text into phonemes. So for Piper the gain in the time to the first sound is
  small: a sentence of the answer is a sentence of the voice. What a stream gives is a flow that
  does not wait for the end of anything (the sentences of an answer are one continuous sound), a
  stop that is immediate, and the room for an engine that does stream inside a sentence.
- **Its voice is loaded by the first sentence it says, and that takes 4.9 s.** The client with which
  the edge asks the gateway had the default of httpx, five seconds for every wait: the first answer
  after a start would have reached a device as words with no sound, and a Whisper bigger than `small`
  would not have been heard at all on a processor. Found here, fixed in the same slice.

## Decision

- **The gateway streams synthesis.** `POST /v1/synthesize` takes `stream: true` and answers in
  server-sent events, as `/v1/chat` does, one JSON object each (`SynthesisEvent`):

  | Event | Says |
  | --- | --- |
  | `format` | `sample_rate` (and `pcm_s16le`: 16-bit little-endian samples, one channel, no container): first, and only when the voice has sound to give |
  | `samples` | `data`: whole samples, base64, at most 64 KiB, in practice a tenth of a second |
  | `done` | the voice has said it all |
  | `error` | `code`, `message`: it failed after it had begun |

  Only `done` says a voice is whole: **a stream that ends without it, or with an `error`, was cut off**,
  and the client raises it (`GatewayUnavailableError`, or the typed error of the code) instead of
  ending as if it had finished; what a listener has been given is never mistaken for a whole
  sentence. A voice with nothing to say is `done` alone. A voice that cannot begin is an HTTP error
  with its own status, never a 200 whose body says the opposite (the first piece is waited for before
  the response is sent, and a failure to begin is retried before anything is).
- **A provider's voice is a generator of pieces** (`ProviderAdapter.synthesize_stream`, yielding
  `SpeechPiece`: a rate and samples). Piper yields one for each sentence, each made on a thread of its
  own and only when somebody asks for the next, so that what waits for it is not held and a listener
  that has gone is not served another. A provider that makes the clip whole yields it as one piece;
  the providers that have no voice refuse it, as they refuse the clip. **The gateway puts the pieces
  together before it cuts them** (a network hands over what it has, and a piece may end in the middle
  of a sample: the rest goes with the next), cuts them into chunks of whole samples, and refuses a
  voice that changes its rate in the middle of what it says. The profile of the `tts` role says it
  streams.
- **Three new frames from the edge to a device**, additive to version 1:

  | Frame | Says |
  | --- | --- |
  | `voice_start` | `utterance_id`, `format` (`pcm_s16le`), `sample_rate`: the sound of the answer begins |
  | `voice_chunk` | `utterance_id`, `seq` (0, 1, 2... with no gap, in a track), `part_seq` (the `seq` of the `speech` part whose sound this is), `data` (whole samples, base64, at most 64 KiB) |
  | `voice_end` | `utterance_id`: the sound is all sent |

  The words of a sentence go first and **as soon as the sentence is whole** (the `speech` part, with
  no `audio`), as for every device; its sound follows as chunks, labelled with the part. The sound
  of an answer is one track from its `voice_start` to its `voice_end`; a voice that changes the rate
  of its samples (another voice was bound in the middle of an answer) begins another track after the
  one that ended, counted from 0 again. Every frame of a voice comes **before the `turn_ended` of its
  turn**, and a turn that ends cut short (`cancelled`, `failed`) has no `voice_end`: its `turn_ended`
  ends the track, and no sound follows it.
- **It is negotiated.** A device that takes a voice as a stream says so in its `hello`
  (`features: ["voice_stream"]`; it needs `audio_out`, a speaker), and the edge says which features it
  will do in `ready`, of what the device declared, the edge has and the device is registered for. A
  device that said nothing is sent a clip in each `speech` part, as before: it keeps working, and so
  does an older device with a newer edge (an empty set of features is not put in a frame at all). A
  `hello` with `features` is a frame that an edge from before them does not know: the edge is
  upgraded before its devices.
- **The sound is made on a task of its own.** The edge gives the sentences of an answer to a `Voice`
  as it writes them, and the voice makes the sound of each, in order and one at a time, and sends it
  as it is made, while the text of the answer goes on: **the words do not wait for the sound.** The
  turn ends when the last sentence has been said. A sentence whose sound could not be made has none
  (its words were sent all the same) and the next is tried; a voice that nothing is bound for
  (`tts_not_configured`) is not asked again for the rest of the answer, and that is not a warning, as
  it was not for a clip; any other failure is one, with its code.
- **A turn that is cut short stops the voice where it is.** A person who speaks over the answer, a
  `cancel`, a new utterance and a device that goes away end the voice before the turn says so: the
  sentence being made is given up on, the gateway's stream is closed (which stops the voice, and a
  Piper that is making a sentence on its thread finishes that one and is not asked for another), and
  nothing follows the `turn_ended`. A device that stops its own playback to send `audio_start` (ADR
  0022) ignores what is still in flight for the turn it left: the `utterance_id` says which.
- **The edge does not pace.** It sends the sound faster than it plays, a frame at a time as the voice
  makes it, and the socket is what holds it back; the device holds what it has not played yet (a
  minute of speech at 22 kHz is 2.6 MB) and drops it when it stops. A speaker that cannot hold that is
  a device that says so in a feature of its own, later.

## Consequences

- **The sound begins when the voice has made its first piece**, and for a voice that makes a
  sentence at a time that is a sentence, as before; what changed is that nothing after it waits for
  the clip of the one before, a device plays one continuous sound, and a stop is immediate.
- **`Speech.audio` stays**, for a device that does not take a stream, and so does `POST
  /v1/synthesize` without `stream`. Nothing was removed.
- **The gateway reaches a cold voice**: the edge waits `FACTOTUM_EDGE_GATEWAY_TIMEOUT_S` (60 s) for
  the gateway to speak or to hear, and three seconds to connect.
- **What this does not do**, left for later records: pace the sound; make the next sentence while
  the one before is being sent (a sentence is made when the one before has been said, which for a
  voice that is faster than speech costs nothing, and for one that is not is a gap that a device
  hears); a `speak` command of a skill that is said by the edge and streamed (it is still a command
  that the device runs); partial synthesis inside a sentence for a provider that does not make it
  (Piper does not); binary frames.
- **A client now has two ways in and two ways out**, each the simpler one and the better one. What
  plays a voice (a buffer, a speaker, the cut when somebody speaks) is the core that every client
  shares ([ADR 0024](0024-the-client-core-one-implementation-of-the-protocol-for-every-client.md)).

## Alternatives considered

- **Raw bytes for the sound** (a chunked HTTP body from the gateway, binary WebSocket frames to the
  device). Less to send, and a failure in the middle of a body that has begun is a connection that
  ends, with no code to say why and no way to tell a whole voice from one that was cut. Events say
  both, and a tenth of a second of base64 is 5.9 KB.
- **A track for each sentence** (a start and an end for each `speech` part). The same information as
  `part_seq` on the chunks, and a device that plays one sound would join them again.
- **The device asks the gateway directly**, with the text that the edge sends. A second credential
  and a second route for a device to be given, and the edge, which knows what a device is and whom it
  acts for, is who may say what is said.
- **Several sentences made at once.** The voice of a sentence of the answer starts when the one before
  has been said: a voice that is faster than speech (Piper is) never makes a device wait, and what
  is made ahead is made for nothing when somebody speaks over it.

## What was built, and what building it found

In five steps: the contracts (`SynthesisEvent`, `voice_start`, `voice_chunk`, `voice_end`,
`features`); the gateway (the providers' `synthesize_stream`, the core, the route); the client of the
gateway (`GatewaySpeech.synthesize_stream`); the edge (`factotum.edge.voice`, the session); and the
end-to-end tests with a probe on the wire. The tests found things that nobody had looked at:

- **The edge's client of the gateway waited five seconds** and Piper takes 4.9 to say its first
  sentence (above). `FACTOTUM_EDGE_GATEWAY_TIMEOUT_S`.
- **`GatewaySpeech` had no tests of its own**: the clip had only ever been tried through the
  end-to-end rig. It has them.
- **A listener that goes away leaves the voice making what it was making**, which is right and was
  first read as a bug by a test that let the voice go on a moment after the listener left: the
  gateway notices that nobody is listening when the connection ends, and a sentence that was in the
  thread by then is finished and thrown away. The third sentence is not made.
- **Breaking the code on purpose** found one hole (nothing noticed that the voice of a turn is
  stopped when the connection is lost while the voice is stuck and the text goes on) and one mutant
  that is equivalent (the explicit close of the stream of the voice, which the event loop's
  finalization of an async generator does one iteration later).
- **The services that the end-to-end tests put together hand a response over when it is whole**, as
  for the answer's text: a sentence is not seen there arriving *while* it is made. It is shown on
  real sockets: the gateway served by uvicorn with a voice that makes a sentence only after the test
  has seen the one before arrive (`e2e/tests/test_e2e_voice_wire.py`), and the edge's own tests with
  a gate in the voice. What the end-to-end tests do show is that everything is right from the model
  that wrote the answer to the last chunk, and that a voice that is made slowly neither holds back
  the words nor survives a person speaking over it.
