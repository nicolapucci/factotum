# 0024. The client core: one implementation of the protocol for every client

Status: accepted, 2026-10-05.

## Context

The protocol of the edge has been spoken by one client so far, the terminal. Each decision since it
was written ([ADR 0021](0021-voice-wake-word-on-the-device-speech-through-the-gateway.md),
[0022](0022-audio-in-is-streamed-to-the-edge-and-the-device-ends-the-utterance.md),
[0023](0023-audio-out-the-voice-of-an-answer-is-streamed-from-the-gateway-to-the-device.md)) added
something that a client has to get right, and the terminal held all of it, in one module next to
what only a terminal is: what to say in `hello`, the parts of an answer one by one, the commands
and their acknowledgements, a stream sent as a person speaks, an answer that is stopped when they
speak over it, the sound that arrives faster than it plays, a connection that is gone, an edge that
does what the protocol does not allow.

The clients that come next differ from it in what they have and not in what they say: a speaker with
a microphone and a speaker, a screen that cannot start a conversation, a phone, a streamer with no
microphone. A client for each, written from the terminal's, would be a copy of the protocol, and of
its mistakes, for each of them; and every one of them would have to find out again what the terminal
already knows (a client over a connection that is gone must not say anything into it; a speaker that
the person speaks over must be silent before the edge is told, not after).

What was asked: clients in Python, tried by running them on a PC, a push to talk instead of a wake
word for now (the wake-word engine needs a key that is not there yet), and clients with different
features, so nothing that depends on one of them (a microphone, a screen) may be in what they share.

## Decision

- **A library, `libs/client_core` (`factotum.client_core`), is the client.** It depends on
  `factotum.contracts` and a WebSocket library and on nothing else: no sound card, no terminal, no
  model, no service. A client of a device is the core and the ports of what that device has.

  | Module | Is |
  | --- | --- |
  | `connection` | `Connection` (what a client needs of a socket), `WebSocketConnection`, `ConnectionLost`, `CouldNotConnect` |
  | `client` | `DeviceClient`: `hello` and `ready`, the parts of an answer, commands and their `ack`, the heartbeat, what a person says (text, a recording, a stream), `cancel`, and what the edge does wrong |
  | `playback` | `VoicePlayer`: the sound of an answer, played as it arrives |
  | `loop` | `VoiceLoop`: the conversation of a client with a microphone |
  | `reconnect` | `Backoff` and `stay_connected`: a device that asks for the edge again, patiently |
  | `ports` | `Identity`, `Renderer`, `CommandDriver`, `CommandRouter` |
  | `audio` | `AudioStream`, `VoiceInput`, `VoiceOutput`, `VoiceSink`, `MarkedSink` |
  | `testing` | what a test of a client is made of: a connection that a test plays the edge on, fakes of every port |

- **A device is what it was given.** `Identity` says who it is and what it can do (`capabilities`,
  and a `display`), and that is what `hello` declares; the client declares `voice_stream` when it
  was given a `VoiceSink` and the device has `audio_out`, and nothing else. A device with no
  microphone cannot start a conversation (`CannotConverse`); one with no speaker has the words of
  what is said, with no sound; one with no screen is never sent what needs one (ADR 0013), and says
  so loudly if it is. The edge does none of this by guessing: the core says what the device has, and
  the edge's filter does the rest.
- **The hands of a device are put together, not inherited.** A command goes to the `CommandDriver`
  (the hands of one part: a volume, a light, a screen, a player) that has the capability the command
  requires, through `CommandRouter(*drivers)`, which is also what a device with these hands declares
  it can do. A command that none has is `unsupported`, with the capability that is missing in its
  detail. A speaker is a volume and a light; a phone is those and a screen and a player.
- **What a person says is a port too.** A `VoiceInput` hands over what was said: a recording whole,
  or an `AudioStream` of chunks as the person speaks (ADR 0022). Whether a wake word was heard, a key
  was held, a file ended is the input's business and nothing else's. `VoiceLoop` is what any client
  with a microphone does with it: send what is said as it is said, wait for the answer, and for what
  was received of its sound to be played, before it listens again; or, for a device that does not
  hear itself (headphones, an echo that is cancelled), `listen_while_answering`, in which what is
  said while the answer is given stops it (speaking over it, ADR 0022). The microphone is opened
  **once** and outlives the connections: a wake-word engine takes seconds to be built, and what is
  heard while the device has no connection has nobody to be said to and is let go of (a stream of it
  is closed, so the microphone is not left holding it). When the microphone has no more (the files
  of `--wav-in` are over), the answer to the last thing said is waited for, and so is its sound.
- **The sound of an answer is played by a task of its own.** The edge sends it faster than it plays
  (ADR 0023). A client that played each frame where it read it would not read the next until the
  speaker had taken this one, and a ping, a command and a person speaking over the answer would all
  wait for the sound. So `VoicePlayer` queues what is received and plays it from a task, in order:
  `begin`, the samples, `finish`, to the speaker's `VoiceSink`. A speaker that wants to know which
  sentence it is saying (a display that highlights it, a device that keeps the words with the
  sound) also has `mark(part_seq)` (`MarkedSink`), called before the first samples of each sentence.
  A speaker that fails (unplugged, in use) loses the track it was playing, the client says so as a
  notice, and the next track is tried again: the answer goes on.
- **Speaking over an answer is the client's to cut, and it cuts first.** Whatever starts something
  new (`say`, `say_audio`, `say_audio_stream`) or stops it (`cancel`) first stops what plays (what is
  queued is dropped and the speaker told to stop, if it was playing) and leaves the answer that was
  going on: what is still on its way for it is let go of in silence, because it is nobody's bug, and
  only then is the edge told. Connection lost, `run` over and `close` stop it as well.
- **The core says what the edge gets wrong, and loudly.** Sound to a device that did not ask for it,
  or for an answer nobody waits for; a chunk outside a track, out of order, or of a part that is not
  a speech part that was sent; a track that begins while one is going on, or that ends when none is;
  a finished answer whose sound was not ended; a clip in a `speech` part to a device that takes its
  voice as a stream. These go to `Renderer.violation`. A turn that ends cut short (`cancelled`,
  `failed`) with a track open is not a mistake of the edge (ADR 0023): its end ends the track, and
  what was received is played to the end.
- **A device that loses the edge asks again.** `stay_connected` connects, converses, waits and
  connects again, with a `Backoff`: one second, twice as long after each failure in a row, up to
  thirty seconds, each wait a quarter more or less by chance (the devices of a home that lost the
  edge together do not all ask in the same moment), and a connection that held for thirty seconds
  starts the waits over. It does **not** ask when asking again would be wrong: the edge refused the
  device (a credential that is wrong, a version that is not spoken), or ended the connection because
  the device was replaced by its twin (asking again would be the two of them taking it from each other
  for ever), because it has no use for it (`policy`) or because of the version. What ended it is said
  (`Ending`).
- **The terminal is a client of the core.** It keeps what a terminal is: the console as a renderer
  and as the hands of the device (`TerminalDevice`, which keeps a volume, a light, a player and a
  screen, and answers `/state`), the keyboard as what is said, a microphone with a wake word and a
  speaker of the sound card (`MicrophoneInput`, `SpeakerOutput`, whole clips), and WAV files for both
  (`WavFileInput`, `WavFileOutput`, which is a clip and a sink: with `--wav-out` the voice is taken
  as the stream that it is). `factotum.terminal.client` and `connection` moved; the classes did not
  change.
- **The end-to-end tests carry sound each way.** A device of `e2e` carries it as a clip (a recording
  whole in, a clip in each sentence out) or as a stream (chunks in, a speaker that is given the
  sound as it arrives out), and every test of `e2e/tests` runs both ways, as it ran with a recording
  whole and streamed in (ADR 0022); the speaker of the second is `FakeSpeaker`, a `MarkedSink`, and
  what it was given is read back sentence by sentence.

## Consequences

- **A new client is its ports.** The device application that comes next is a `VoiceInput` (a key to
  push to talk, a wake word behind the same port later), a `VoiceSink` over the sound card, the
  hands for what the device has, and `stay_connected`: no line of the protocol. Its tests are
  the core's fakes and the real edge of `e2e`.
- **Nothing changed on the wire.** The core is the terminal's client, generalised; what a device
  sends and is sent is ADRs 0013 and 0021 to 0023. A client in another language would implement
  `docs/edge.md`, and the core is the reference for what it must do (and say when the edge is wrong).
- **What is not in the core, on purpose**: anything that needs hardware or a key (a sound card, GPIO,
  a wake-word engine), the configuration of a device, how it is paired and given its credential, and
  the process it runs as. The core has no `sounddevice` and no `numpy`.
- **Not used yet**: `stay_connected` is tested and no client runs it (a terminal that loses the edge
  ends and says so; the device application will not), and no speaker uses the marks of a `MarkedSink`
  but the fake of the tests.
- **What ADR 0023 left stays**: the edge does not pace, so a client holds what it has not played yet
  (a minute of speech at 22 kHz is 2.6 MB) and drops it when it stops; the echo of a speaker in its own
  microphone is the device's to cancel before it listens while it speaks.

## Alternatives considered

- **Keep the client in `clients/terminal` and import it from the others.** A device that imports the
  terminal gets a console, a command line and stdin that it does not use, and a change to what the
  terminal is for breaks the device.
- **A base class that each client subclasses.** What differs between devices is which pieces they
  have (a microphone, a speaker, a screen, hands for a volume), in different numbers, and a base
  class is a list of methods that a device that lacks a piece has to remember to override. Ports are
  composed: what the device was not given is what it does not declare.
- **The wake word in the core.** It needs the hardware and, for the engine chosen, a key; the core
  takes what a person said and not how they were heard, so that a push to talk, a wake word and a
  file are the same to it.
- **The supervisor of the process for what happens when the edge goes** (`Restart=always`). A process
  that is restarted builds its microphone again (the wake-word engine takes seconds), forgets what it
  was saying, waits the same each time, and does not know which refusals are not worth asking again.
  The supervisor of the process is still there for what the core does not catch.

## What was built, and what building it found

The ports, the player and the client (generalised from the terminal's, with what it learned of the
voice), the loop, the supervisor, then the terminal and the end-to-end devices rebuilt on top of
them. The tests of the terminal's client moved with it, adapted to the ports and checking what they
checked; the whole of `e2e`, run both ways, is the check that what a person meets did not change. What
the work found:

- **`playing` was false for a track that had begun and not ended**, so speaking over an answer whose
  samples had all been taken (and whose end had not come) did not tell the speaker to stop: the
  player has a flag for a track that is open.
- **Breaking the code on purpose, one way at a time** (over sixty ways in the core) found eight
  holes that had tests which passed all the same, each now closed by a test: a stop that did not
  drop what was queued (the test queued nothing behind what was playing, so there was nothing to
  drop); a `cancel` that did not leave the answer, so what was in flight for it was played after it;
  a client that is closed while what reads is held up, which has to silence the speaker without
  waiting for it; a track ended twice, and a chunk after the end of its track, both let through; and
  the waits of `VoiceLoop`, three of them: after each thing said, and after the last, for the answer
  and for its sound. They cover for each other in the mode the tests used (a person who is listened
  to again only when the answer is played), so taking away either passed; it took a person who has
  more to say while the answer plays, and one who leaves while it is still being said, to show each.
  No mutant of the core was left alive.
- **A fake that is a marked sink changes what a plain one records**, so there are two (`FakeVoiceSink`
  and `FakeMarkedSink`), and a speaker that is both a clip and a sink is a third (`FakeSpeaker`).
- **A microphone that heard something in the moment the connection went** would have sent it to
  nothing and waited for its answer: it is dropped, its stream is closed, and the next connection
  does not hear it.
- **Measuring what the tests run found what no mutation had touched**: the branch of the loop for a
  connection that is gone as something is said (the send fails) had no test, nor had the one for a
  recording, and not a stream, heard as the connection went. They have.
