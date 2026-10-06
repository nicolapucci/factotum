# 0025. The device application: one program for a PC and a Raspberry Pi, over a library of the sound hardware

Status: accepted, 2026-10-06. The wake word that waits for a key, here, is
[ADR 0027](0027-the-wake-word-without-a-key-openwakeword-behind-the-same-port.md)'s: Picovoice has no
key for a person, and the word is watched for by openWakeWord, with no key (the key stays for
Porcupine, as an extra).

## Context

[ADR 0024](0024-the-client-core-one-implementation-of-the-protocol-for-every-client.md) made the
protocol a library and left a client as "its ports": a `VoiceInput`, a `VoiceSink` over the sound
card, the hands for what the device has, and `stay_connected`. The only program that held a sound
card was the terminal, with a module of its own (a wake word of Porcupine's, a speaker that took a
clip, a function that finds where speech ends), written to try the stack from a console. The device
that a person has in a kitchen is not a console: it is a program that starts with the machine, with
a microphone and a speaker, that nobody types into.

What was asked: the real client, in Python, tried by running it on a PC with Windows, then on a
Raspberry Pi; a push to talk for now, because the wake-word engine needs a key that is not there
yet (and the wake word behind the same port later); and nothing in it that the other clients (a
screen, a phone, a streamer with no microphone) could not share.

## Decision

- **Two members of the workspace.** `libs/client_audio` (`factotum.client_audio`) is the sound
  hardware that clients share, and `clients/device` (`factotum.device`) is the application. The
  terminal uses the library too (its own sound module is gone), so a speaker, a microphone and a way
  of finding where speech ends are written once.

  | Module | Is |
  | --- | --- |
  | `client_audio.backend` | `AudioBackend`: the small part of PortAudio that clients use (list, check, open a microphone, open a speaker), and `SoundDeviceBackend`, the only code that imports `sounddevice`, when it first needs it |
  | `client_audio.microphone` | `Microphone`: opened when it is needed, closed when it is not; the blocks the hardware hands over, on a thread of its own, are put in a queue that the loop reads |
  | `client_audio.endpoint` | `capture_speech`: what is said, as chunks, until the person has been quiet for a while, has talked as long as they may, or says they are done |
  | `client_audio.inputs` | `PushToTalk` and `WakeWord`, each a `VoiceInput` |
  | `client_audio.keys` | `EnterKey`: the presses of Enter in a terminal |
  | `client_audio.speaker` | `Speaker`: a clip, a track as it arrives, which sentence is being said, and a volume of its own |
  | `client_audio.testing` | `FakeBackend`, `KeyPresses`: a sound card and a key that a test plays |
  | `device.config` | the settings, read from a file and the environment |
  | `device.hands` | what the device does when it is told to |
  | `device.app` | `build_hardware`, `open_client`, `run_device`: the core, the library and the settings put together |
  | `device.doctor` | what is wrong, said in a line each |
  | `device.render`, `device.cli` | what the device says of itself, and `factotum-device` |
  | `device.testing` | settings and an edge that a test plays |

- **Everything that is a sound card goes through `AudioBackend`.** What decides things (where speech
  ends, which sentence is playing, what a volume does to the samples, which key ends what) is written
  against the interface and tried with a backend that a test plays; the few lines that are
  `sounddevice` are tried by hand (`factotum-device doctor`). A machine with no PortAudio has an
  `AudioUnavailableError` that says what to install, and not an `ImportError` on import.
- **A key first, a word behind the same port.** Both are a `VoiceInput` (ADR 0024); which one a device
  has is `listening.mode`. Push to talk is Enter, since a terminal says what was typed a line at a
  time and nothing of a key held down: the first press begins, being quiet ends it, and so does a
  second press. The microphone is open for what is said and no longer: nothing is heard that was not
  asked to be. A wake word keeps it open, which is what a wake word is. A word that needs a key
  that is not there is refused when the settings are read, and a device that cannot hear never needs
  one.
- **The volume is the speaker's.** `set_volume` scales the samples before they are played (the square
  of the share of the full volume: a person hears loudness and not amplitude, and half the amplitude
  is not half as loud), so it is the same on a PC, on a Raspberry Pi and on whatever has no mixer, and
  the volume of the system is left alone: a PC that is tried is not turned down for the person who
  tries it.
- **A text to say is what this device cannot do.** The `announce` skill answers with `speak`, and a
  device with no voice of its own has nothing to say it with: the voice is made by the model gateway
  (ADR 0008, 0021 and 0023), which the edge reaches and a device does not. It answers `unsupported`
  and says why, and the skill is told that it did not work. A device that showed the text and said
  `ok` would leave an audit trail that says that an announcement was made which nobody heard. What
  would make it work is the edge turning a `speak` command into a voice and streaming it as it does an
  answer, which is a decision of its own and is not made here.
- **A device declares what the application has hands for.** `audio_in` and `audio_out`, and not more
  than the device is registered for: a capability that is declared with no hands is a command that
  can only fail. A device that declares `audio_out` alone is a speaker that is spoken through, which
  never opens a microphone.
- **The settings are a file, checked when they are read.** YAML, strict about what it does not know,
  and what is wrong is said in a line that names the setting and never shows a value that may be a
  secret. The two secrets of a device (its credential, the key of the wake word) may be in the file
  and are better in the environment, which wins: a file of settings is often a file in a repository,
  and a credential is not.
- **The doctor tries each reason that a device may be silent, by itself.** The settings, the sound
  devices that the system lists, a microphone and a speaker that the system accepts at the rates they
  will be used at (16 kHz is what Whisper and the wake word hear, 22.05 kHz what the voice is made
  at), and the edge. It says what to do about each that fails, makes no sound and hears nothing
  unless asked (`--play`, `--listen`), and connects to the edge **as the device**, which is the
  edge's rule that the last connection of a device wins: a device that is running is replaced by it,
  so `--no-edge` is there.
- **It stays connected.** `stay_connected` is what runs (ADR 0024 left it unused), with the waits of
  the settings. How it ended is the exit code: 0 when the person left, 1 for settings or a sound
  card that is wrong, 3 when the edge does not accept the device, 4 when another connection of the
  same device took its place, 5 when it speaks another version, 6 when it has no use for the device.
  A supervisor restarts what failed (1) and not what the edge decided (3 to 6): a device that is
  asked to go away and asks again, or that takes its place back from its twin, is the two of them
  taking it from each other for ever.
- **It says what it does in lines that a journal can hold.** One line for each thing, with a tag, in
  ASCII, flushed at the end of each (standard output is a pipe under a service, and a pipe is not
  written until it is full), and nothing in them is a secret. `--quiet` leaves out the words of the
  answers, which are heard.
- **It says that it has no keyboard.** A service has an input that is the end of a file, so a push to
  talk would stop at once and say nothing: the device says that there is no key to press and that a
  device that nobody sits at listens for a word.
- **A unit for systemd** (`infra/device/factotum-device.service`) starts it with the machine, with the
  settings in `/etc/factotum/device.yaml`, the secrets in a file of the environment that only its user
  can read, and the restarts above.

## Consequences

- **What was tried on the hardware**: `factotum-device devices` and `doctor --no-edge` on the
  Windows machine of the maintainer (the sound devices that PortAudio lists, and the default
  microphone and speaker accepted at the rates they will be used at). A conversation by voice on the
  hardware, the wake word with a real key and a Raspberry Pi are for a person to try: the tests
  (`e2e/tests/test_e2e_device_app.py` and the unit tests of both members) have a sound card that a
  test plays, which says that the application does what it should with what the card hands over and
  nothing of the card.
- **A text to say fails on this device.** A skill that announces something answers well to every
  device that has a screen or a terminal and fails on a speaker of this application, with the reason
  in its audit record. It is the first thing that the next decision about the edge is for.
- **Windows lists the same hardware once for each host API** (MME, DirectSound, WASAPI, WDM-KS: forty-five
  entries for a handful of devices), and `sounddevice` refuses a part of a name that fits several. A
  number, as `devices` lists it, is the sure choice, and what the doctor and the example say.
- **Not in the application, on purpose**: a screen, a light or a player (a device that has them is
  another program, or this one with more hands: the settings refuse what has none), how a device is
  paired and given its credential (the hub gives it, and a person copies it), and starting with the
  machine on Windows. The unit for a Raspberry Pi has not been run on one.
- **A speaker hears itself**: `listen_while_answering` is off unless the settings say that the device
  has headphones or cancels its own echo (ADR 0024 left this to the device).
- **What the terminal keeps is what a terminal is**: the console as a renderer and as the hands of the
  device, the profiles, the files of `--wav-in` and `--wav-out`. Its tests did not change but for an
  import.

## Alternatives considered

- **Grow the terminal into the device.** It is a way to look at the edge from the side of any
  profile, with a console for hands, `/state`, and a keyboard that is its input. A program that
  starts on a Raspberry Pi with all that would carry what only a test needs, and a change to what the
  terminal is for would break the device.
- **A copy of the terminal's sound module in the device.** Two copies of a speaker and of where
  speech ends are two sets of mistakes, and the next client with a sound card would be a third.
- **The volume of the system** (a mixer on Linux, the endpoint volume on Windows). It is a different
  call on each, needs more than the device has the right to, changes the volume of whatever else the
  machine plays, and the Raspberry Pi of a person who has an amplifier has no mixer to ask. The cost
  of the speaker's own volume is that it cannot make a speaker louder than the voice is made, and
  that very low levels have few steps (16 bits).
- **The wake word as the default.** It needs a key that a person makes, and cannot be tried by a
  maintainer who does not have one yet. Push to talk tries the whole path, and the word is a setting.
- **A voice in the device for what `speak` says.** It would put a speech model in every device and
  make the device the place that knows one, against ADR 0008. The voice stays at the gateway.
- **`Restart=always` for the unit.** A device that the edge sent away would come back every few
  seconds for ever, and a device that was replaced would take its place back from its twin.

## What was built, and what building it found

The library, the application, the terminal moved onto the library, and the tests of each.

- **The first run on real hardware showed what the fakes could not.** `devices` listed forty-five
  entries for a handful of devices: the same microphone and speaker are listed once for each host
  API. How `sounddevice` takes a name then mattered: a part of a name that fits more than one device
  is refused, and the doctor's advice was to choose "a part of the name". The advice, and the
  example, now say a number.
- **What the work found wrong in what it had just written**: the rule that the longest wait is not
  shorter than the first said, when it refused, the opposite of what it meant; the level of the log
  was set with `logging.basicConfig(level=...)`, which does nothing when the root logger has a
  handler already (pytest's, or a library's), so the setting could be ignored, and it is now set on
  the logger; and a service, whose input is the end of a file, ended at once and said nothing when a
  push to talk met it (it says what is wrong now).
- **Breaking the code on purpose, one way at a time** (174 ways, in both members) found 29 that no
  test noticed, each now closed by one. The ones that mattered:
  - Without the line that drops a press of Enter that came after what was said was over, that press
    began another thing to say. The fake of the key was looser than the real one (its "was it pressed
    again" never looked at the presses that were waiting), so only a test with the real `EnterKey`,
    fed a line at a time, shows it.
  - Without closing the speech that nobody read to its end, it was closed whenever the interpreter got
    to it and not with what listens to it: a spy that sees when it is closed is what shows it.
  - The microphone of the wake word opened at the rate and the block of the defaults, which are those
    of the stand-in engine, so nothing showed that it was not the engine's; an engine that takes
    others does. The same for the doctor, which could have tried the microphone at the rate of the
    voice and the speaker at the rate of the microphone: the fake did not remember what it was asked.
  - Where the settings put the devices, the level of quiet, the longest and the wait of a word and of a
    key (a dozen of the settings), nothing noticed that one of them was not passed on; one test of the
    application for each of the two ways of listening does.
  - The line that keeps the chunks of a clip to whole samples at a rate with no whole tenth of a second
    (11.025 kHz) was right and had no test; the scaling of the volume, which refuses an odd number of
    bytes, is where it would have shown.
  - `--quiet` had a test that asserted nothing of what is said, `--listen` was tried by no test, and
    a line that is not flushed as it is written (nothing on a console, everything under a service)
    was noticed by none.
