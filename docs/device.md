# The device application

`factotum-device` is the program that runs on a Factotum device: a speaker with a microphone, on a PC
for trying it and on a Raspberry Pi for keeping. It connects to the [edge](edge.md) as the device it
was paired as, hears a person when they ask to be heard (a key, or a word), sends what they say as
they say it, and says the answer as it arrives. It has no model, no key of a provider and no
screen: the words of an answer come from the conversation service, their sound from the model
gateway, and what a device may do is decided by the platform.

*Implemented*: the application (`clients/device`) and the library of the sound hardware that it shares
with the terminal (`libs/client_audio`), push to talk with Enter, a wake word behind the same port
(openWakeWord, which needs no account and no key, and Porcupine for whoever has a key), the volume of
the speaker, what a skill has it say, staying connected, `doctor`, and a unit for systemd. The reasons
are in [ADR 0025](decisions/0025-the-device-application-one-program-for-a-pc-and-a-raspberry-pi.md),
[ADR 0026](decisions/0026-saying-something-on-a-device-play-audio-speak-through-the-edge-and-core-skills.md)
and [ADR 0027](decisions/0027-the-wake-word-without-a-key-openwakeword-behind-the-same-port.md);
what every client does of the protocol is [the client core](edge.md#the-client-core). *Not yet*: a
screen, a light or a player, a word of the house (the words are English), and a package for a
Raspberry Pi. Tried by hand so far: `devices` and `doctor` on a Windows PC, with the real models of
the wake word, which load; the rest is tried by the tests, with a sound card that a test plays, and
the wake word with the real models by a live test that says words with a synthetic voice.

## Trying it

```bash
uv sync --all-packages
uv run factotum-device init          # writes the file of settings (it says where)
```

Edit the file: the `hardware_id` of the device as it was paired (the kitchen speaker of
`config/devices.example.yaml` is `kitchen-01`, with the credential `kitchen-dev-token`), and put the
credential in the environment, which is where it belongs:

```bash
export FACTOTUM_DEVICE_TOKEN=kitchen-dev-token       # PowerShell: $env:FACTOTUM_DEVICE_TOKEN = "..."
uv run factotum-device devices                       # the microphones and speakers of this machine
uv run factotum-device doctor                        # the settings, the sound card, the edge
uv run factotum-device doctor --listen --play        # also hear for two seconds, and play a tone
uv run factotum-device                               # be the device
```

To wake it with a word instead of a key (`listening.mode: wake_word`), fetch the models of the word
once, and try it with your own voice:

```bash
uv run factotum-device wake-word download            # the models of "hey jarvis": 3.7 MB, checked
uv run factotum-device doctor --word --no-edge       # say "hey jarvis": how near it came, in 5 seconds
```

The edge has to be up (`docker compose --profile services up -d`, with `config/devices.dev.yaml` as
[`edge.md`](edge.md#trying-it) says) and, for a person to be heard and answered aloud, the model
gateway bound to speech ([`model-gateway.md`](model-gateway.md#speech)) and the edge pointed at it
(`FACTOTUM_EDGE_GATEWAY_URL`). Press Enter and say something (or say the word, with `wake_word`);
being quiet for a moment, or pressing Enter again, ends it. `q` and Enter, or the end of the input,
leave.

```
--       connected as Kitchen speaker (audio_in, audio_out, led)
--       press Enter to speak, and Enter again when you are done (q and Enter: quit)
--       listening
[heard]  Come si resetta il router?
[...]    searching
[say]    Tieni premuto il pulsante di reset per dieci secondi.
```

Each line is a thing that happened, with a tag (`[heard]` what the edge took the recording for,
`[say]` what is being said, `[...]` what the assistant is doing, `[do]` a command that was run, `[stop]`
an answer that was interrupted, `[!]` an answer that failed, `[edge!]` an error the edge sent, `[BUG]`
something the edge did that the protocol does not allow, `--` the device itself). `--quiet` leaves out
what is said, which is heard.

## The settings

A YAML file: `--config`, else `$FACTOTUM_DEVICE_CONFIG`, else `%APPDATA%\factotum\device.yaml` on
Windows and `~/.config/factotum/device.yaml` elsewhere. `config/device.example.yaml` is what `init`
writes, and a test says it is the defaults and that it names every setting. A setting that is wrong
is said by its name (`audio.volume: ...`) and never by its value, and nothing is connected to.

| Setting | Default | Is |
| --- | --- | --- |
| `edge.url` | `ws://127.0.0.1:8084/v1/connect` | where the edge is; `wss://` behind a proxy. `$FACTOTUM_DEVICE_URL` wins |
| `device.hardware_id` | (required) | the label the device was paired under |
| `device.token` | (required, here or in the environment) | the credential it was given when it was paired. `$FACTOTUM_DEVICE_TOKEN` wins |
| `device.capabilities` | `[audio_in, audio_out]` | what this hardware has: only these two, and never more than the device is registered for. `[audio_out]` alone is a speaker that is spoken through, with no microphone |
| `audio.input_device`, `audio.output_device` | the default of the system | a number as `devices` lists it, or a part of the name. A number is the sure choice: Windows lists the same hardware once for each host API, and a part of a name that fits several is refused |
| `audio.volume` | `100` | how loud it speaks at first, in percent of what the voice makes; a skill can change it |
| `listening.mode` | `push_to_talk` | `push_to_talk` (Enter) or `wake_word` |
| `listening.engine` | `openwakeword` | what listens for the word: `openwakeword` (no account, no key; its models are fetched once by `wake-word download`) or `porcupine` (needs an AccessKey, which Picovoice gives to companies: it has had no free plan since 2026-06-30) |
| `listening.wake_word` | `hey_jarvis` (`jarvis` with Porcupine) | the word: `alexa`, `hey_jarvis`, `hey_mycroft` or `hey_rhasspy` for openWakeWord (they are English), one of Porcupine's keywords for Porcupine. A word that the engine has no model for is refused when the settings are read, and said with the ones that it has |
| `listening.threshold` | `0.5` | openWakeWord: the score, above 0 and up to 1, at which the word is taken to have been said. Lower it if the word is not heard (`doctor --word` says how near it came), raise it if a sound is taken for it |
| `listening.patience` | `1` | openWakeWord: the frames of 80 ms in a row that must be over the threshold (1 to 10) |
| `listening.models_dir` | `~/.cache/factotum/openwakeword` | openWakeWord: where its models are (`~` is the home of whoever runs the device) |
| `listening.access_key` | (for Porcupine) | its AccessKey. `$PICOVOICE_ACCESS_KEY` wins. Porcupine with no key is refused when the settings are read |
| `listening.silence_seconds` | `1.2` | how long a person is quiet before what they said is over |
| `listening.silence_level` | `150` | the mean level, of 32768, under which a stretch of sound is quiet |
| `listening.max_seconds` | `12` | the most that one thing said may last |
| `listening.listen_while_answering` | `false` | go on listening while the answer is said, so that a person who speaks over it stops it: only for headphones, or a device that cancels its own echo (a speaker hears itself) |
| `reconnect.first_s`, `reconnect.max_s` | `1`, `30` | a device that loses the edge asks again after the first, and after twice as long each time, up to the second |
| `log_level` | `INFO` | `DEBUG` shows what the libraries say as well, the connection among them |

The credential (and the key of Porcupine, for whoever has one) are secrets: they are better in the
environment than in a file that may be copied somewhere, and they are never written in a line that
the device says, in an error, or in what `doctor` prints.

## How a person is heard

**A key.** A terminal says what was typed a line at a time and nothing of a key held down, so the key
is Enter: the first press opens the microphone, being quiet for `silence_seconds` (or a second press)
ends what is said, and the microphone is closed. Nothing is heard that was not asked to be. What is
said is sent to the edge as it is said, a tenth of a second at a time ([ADR 0022](decisions/0022-audio-in-is-streamed-to-the-edge-and-the-device-ends-the-utterance.md)),
so the answer starts as soon as the person has stopped.

**A word.** With `listening.mode: wake_word` the microphone is kept open and an engine watches it,
a frame of 80 ms at a time: what follows the word is sent as it is said, up to a pause or
`max_seconds`, and the engine forgets what it heard before it watches again. It is the mode for a
device that nobody sits at, and the one a service must use: with a push to talk, a service has no key
to press, and the device says so and stops.

The engine is openWakeWord unless the settings say Porcupine ([ADR
0027](decisions/0027-the-wake-word-without-a-key-openwakeword-behind-the-same-port.md)). It needs no
account and no key, only its models, which are not in this repository (they are their author's, CC
BY-NC-SA 4.0, for use that is not commercial) and are fetched once, on each machine that a device
runs on, by a command that a person runs:

```bash
uv run factotum-device wake-word download hey_jarvis     # or alexa, hey_mycroft, hey_rhasspy
```

Each file is checked against the size and the SHA-256 that the code knows it by before it is kept; one
that is there and is right is left alone, and nothing else fetches anything (a device without them
says what to run, in the `doctor` and when it first listens, and its exit code is 1). They go in
`~/.cache/factotum/openwakeword` (the `--dir` of the command and `listening.models_dir` say another).
The runtime is written here on `onnxruntime`, and gives the scores of the openWakeWord package to the
last digit; it costs about 2 ms of one core for each 80 ms of sound.

**Choosing the word and the threshold.** The four words are English, and said by a voice with an
accent they are not all the same: said by the Italian voice of Piper, `alexa` scored 1.00,
`hey_jarvis` 0.21 ("Ehi Jarvis": 0.43), `hey_mycroft` 0.00 (said "Ei Maicroft": 1.00) and `hey_rhasspy`
0.01. That is a synthetic voice and not yours, which is what `doctor --word` is for: it listens for
five seconds, says how near the word came, and what would have taken it.

```
say 'hey jarvis' now: listening for 5 seconds
...
FAIL  word          'hey_jarvis' was not heard: the best score in 5 s was 0.31, under the threshold of 0.5
                    -> it came near: listening.threshold 0.25 would have taken it (a lower one lets in
                       more sounds that are not the word), or say it nearer to the microphone
```

A score that is from a quarter of the way up is a word that came near; under it the word was not
heard at all, and the advice is to say it nearer or to try another of the four. A threshold that is lower
lets in more of what is not the word, and a higher one lets the word go by: it is the setting to move
until a person is understood and the television is not.

**Porcupine** is for whoever has an AccessKey (Picovoice gives them to companies, and its free plan
ended on 2026-06-30): `uv sync --all-packages --extra porcupine`, `listening.engine: porcupine`, and
the key in `$PICOVOICE_ACCESS_KEY`. Porcupine asks Picovoice's service to validate it the first time
it is made, and the model of the keyword is inside its package.

**While it answers.** By default the device waits for the answer, and for its sound to be played,
before it listens again, because a speaker that hears itself would take its own voice for a person's.
With `listen_while_answering` it goes on listening, and a person who speaks over the answer stops it:
the sound that plays is cut before the edge is told ([ADR 0024](decisions/0024-the-client-core-one-implementation-of-the-protocol-for-every-client.md)).

## What it says, and what it does

The voice of an answer arrives as a stream and is played as it arrives, a sentence at a time, at the
**volume of the speaker**: a `set_volume`, `adjust_volume` or `set_mute` command from a skill (to a level,
or up or down by an amount that was said, or by the step of the speaker, `audio.volume_step`, 10
points unless the settings say another; never past either end) scales the samples before they are
played (the square of the share of the full volume, since a person hears loudness and not
amplitude), so it is the same on a PC and on a Raspberry Pi and the volume of the system is left
alone. A device that the edge sends clips to (an edge that does not stream the voice) plays each clip.

**What a skill has it say is a clip too** ([ADR
0026](decisions/0026-saying-something-on-a-device-play-audio-speak-through-the-edge-and-core-skills.md)).
The device says in its `hello` that it takes `play_audio`, and the edge sends it the voice of an
announcement (a `play_audio` command, which carries a clip made by the model gateway) and, for a skill
that gives it a text to say (`speak`), the voice of each sentence of it, made by the edge. The
application plays a clip when nothing else is being said, in the order they came, and tells the edge
at once that it took it; a person who speaks (the key, or the wake word) cuts what is being said, and
what waits is said after their answer. The console shows each as `announcement: <the words>`.

**Timers and alarms** ([ADR
0029](decisions/0029-the-skills-for-what-a-device-does-are-the-interface-between-the-model-and-the-device.md)).
The device keeps them itself, on its own clock: a `set_timer` or a `set_alarm` carries the clip that
rings, and the device holds it and rings when the time comes, even if the edge is not there then. The
keeper (`factotum.client_core.Timekeeper`) is the device's and not a connection's: it is made once and
lent to each client the device makes after a lost connection, and it is lost when the program stops
(nothing is written to disk in this version, and nothing repeats: an alarm is for one day). A timer
runs on the clock that only goes forward; an alarm is for a time on the wall of the place (the clock
and the time zone of the machine), so an alarm for seven rings at seven. It rings like an
announcement, when nothing else is being said, and again every eight seconds until a person says to
stop it (*ferma il timer*, *spegni la sveglia*) or for three minutes; a muted speaker rings in
silence. Up to eight of each are held. The console shows `timer t1 set for 300 s`, `timer t1 is up`
and `alarm a1 set for 2026-10-14 07:00`.

A device is its hands, and what the hands of `audio_out` do is set the volume. Saying is not the
hands' work: the client core plays a clip, and a text never reaches a device that takes clips, because
the edge makes a clip of it. What these hands are asked for and have no hands for is answered
`unsupported`, and the skill is told that it did not work, which is the truth.

## `doctor`

A device that does not answer has many reasons to be silent, and from outside they look the same.
`doctor` tries each by itself, in the order a person would, and says what to do about those that
fail:

| Check | Is |
| --- | --- |
| `settings` | what was read: the hardware id, the edge, what it has, how it listens |
| `sound devices` | the devices that the system lists (a machine with no PortAudio is told what to install: on Linux, `libportaudio2`) and that there is a microphone and a speaker where the settings need them |
| `microphone`, `speaker` | that the system accepts the device chosen at the rate it will be used at (16 kHz is what Whisper and the wake word hear, 22.05 kHz what the Italian voice is made at); nothing is opened or played. A microphone that Windows does not let a program hear is said with the setting that does it (Settings, Privacy, Microphone) |
| `wake word` (when a device waits for a word, or with `--word`) | that what listens for the word can be had: for openWakeWord that its models are there, are what they should be and load (what is missing is said with the command that brings it, `wake-word download`, with the folder when it is not the usual one); for Porcupine that its package is installed (Picovoice checks the key when the device starts, and it is not asked here) |
| `edge` | that the edge can be reached, accepts the device, knows what it declares (a capability that it is not registered for is said), what it will use (`voice_stream`, `play_audio`, or a clip for each sentence), and nothing that the protocol does not allow |
| `listening` (with `--listen`) | two seconds of the microphone, and how loud it was: one that hears nothing at all is muted, or is not the one that was meant |
| `playing` (with `--play`) | a quiet tone, and a person is asked whether they heard it |
| `word` (with `--word`) | five seconds of the microphone through openWakeWord, after a line that says when to speak: whether the word was taken, the best score it came to, and, when it was not, the threshold that would have taken it or what else to try (Porcupine says that it heard the word and not how near, so this is for openWakeWord) |

It makes no sound and hears nothing unless it is asked to (`--listen`, `--play`, `--word`), and it
connects to the edge **as the
device**: the edge keeps the last connection of a device, so a device that is running is replaced by
`doctor`. Stop the device first, or give `--no-edge`. The exit code is 0 when every check is ok and 1
otherwise.

## How it ends

It stays connected: a device that loses the edge asks again after `reconnect.first_s`, twice as long
after each failure in a row, up to `reconnect.max_s`, and starts over after a connection that
held. What ends it is the exit code:

| Code | Is |
| --- | --- |
| `0` | the person left (`q` and Enter, the end of the input, Control-C) |
| `1` | the settings are wrong, the file is not there, or the sound card failed |
| `2` | the arguments are wrong |
| `3` | the edge does not accept the device (a credential or a hardware id that it does not know) |
| `4` | another connection of the same device took its place: asking again would be the two of them taking it from each other for ever |
| `5` | the edge speaks another version of the protocol: update the device |
| `6` | the edge has no use for the device (`policy`) |

## On a Raspberry Pi

The same program, as a service. It has not been run on one yet: the unit is a starting point, and
`systemd-analyze security factotum-device` says how exposed it is.

```bash
sudo apt install libportaudio2                       # the sound library that sounddevice needs
sudo useradd --system --create-home --groups audio factotum
# the repository in /home/factotum/factotum (git clone, or a copy), then, as that user:
uv sync --package factotum-device                    # uv installs the Python that this needs (3.13)
```

Put the settings in `/etc/factotum/device.yaml` (from `config/device.example.yaml`, with
`listening.mode: wake_word`: a service has no keyboard) and the credential in
`/etc/factotum/device.env`, readable by the `factotum` user and nobody else (and, only for
Porcupine, `PICOVOICE_ACCESS_KEY=...` beside it):

```
FACTOTUM_DEVICE_TOKEN=the-credential-the-hub-gave-when-the-device-was-paired
```

Then fetch the models of the word, once, as that user (the folder that the service will look in is
its own `~/.cache/factotum/openwakeword`):

```bash
sudo -u factotum sh -c 'cd /home/factotum/factotum && .venv/bin/factotum-device wake-word download'
```

```bash
sudo cp infra/device/factotum-device.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now factotum-device
journalctl -u factotum-device -f                     # what it says: the lines above, one each
```

The unit restarts what failed (exit code 1: a sound card that is not there yet at boot, settings
that were wrong) ten times in five minutes, and never what the edge decided (3 to 6), for the reasons
of the table above. Run `doctor` as that user, with the service stopped, to see what it sees:

```bash
sudo systemctl stop factotum-device
sudo -u factotum sh -c 'set -a; . /etc/factotum/device.env; set +a;
    FACTOTUM_DEVICE_CONFIG=/etc/factotum/device.yaml \
    /home/factotum/factotum/.venv/bin/factotum-device doctor --listen --play'
```

## Known limits

- The words are four, in English, and a voice with an Italian accent is not heard by all of them
  alike (above); a word of the house, in Italian, is a model that has to be trained, which is not
  done. The models are their author's, non-commercial.
- A speaker hears itself, and `listen_while_answering` is for headphones; cancelling the echo of a
  speaker in its own microphone is not done.
- A conversation by voice on the hardware, the wake word with a real person's voice, and the unit on
  a Raspberry Pi have not been tried by a person yet; what has is in the next section.
- A device with a screen, a light or a player is not this program: the settings refuse a capability
  it has no hands for, which would be a command that can only fail.
- Windows has no way to start it with the machine yet; a Raspberry Pi has the unit.
- How a device is paired and given its credential is the hub's ([`hub.md`](hub.md)); the credential is
  copied by hand into the environment of the device.

## Tests

The application is tested with a sound card and a key that a test plays (`FakeBackend`, `KeyPresses`
in `factotum.client_audio.testing`) and an edge that a test plays over in-memory connections
(`factotum.device.testing`): the settings, the hands, what it says, putting the hardware together
from the settings, each way of listening, staying connected, the doctor with each check by itself,
and the command with its exit codes. `e2e/tests/test_e2e_device_app.py` runs the program against the
whole assistant on a real socket (a person presses the key and says a question, the answer is
streamed back to the speaker, a command is run by the hands of the device, a text to say is refused
and the skill is told, a wrong credential, and the doctor against a real edge), in memory and in
Postgres. The library of the sound hardware is tested against a backend that a test plays, and the
few lines of `SoundDeviceBackend` against a stand-in for `sounddevice`; what remains is what only a
card shows, which `doctor` is for.

The wake word is tried at three levels: the runtime of openWakeWord against models that are made up
(three that answer with what they were given, so that what flows from one to the next can be read:
the shapes, the window of 480 samples that the spectrogram needs, the 76 rows of an embedding, the
sixteen embeddings of the word, the first five scores that are zero), the downloader against a
server that a test plays (a file that is not the one that was expected is never kept, whatever it
is: a property), and the real models with the Italian voice of Piper and the real Whisper
(`e2e/tests/test_e2e_wakeword.py`, live: [`development.md`](development.md#with-the-real-wake-word)).

The code was broken on purpose, one way at a time, in 174 ways across the two members: 29 of them
passed the tests the first time, and each has a test now (ADR 0025 says which mattered).
