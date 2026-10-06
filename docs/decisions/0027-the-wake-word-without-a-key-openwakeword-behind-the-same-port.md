# 0027. The wake word without a key: openWakeWord behind the same port, its models fetched by a command

Status: accepted, 2026-10-06.

## Context

[ADR 0021](0021-voice-wake-word-on-the-device-speech-through-the-gateway.md) put the wake word on the
device, and [ADR 0025](0025-the-device-application-one-program-for-a-pc-and-a-raspberry-pi.md) put it
behind a port (`WakeWord`, a `VoiceInput`) with one engine: Porcupine, which asks for an AccessKey
that Picovoice gives. The device was built and tried with a push to talk, and the word waited for a
key that a person had to make.

The key cannot be had. When the maintainer tried to make one, Picovoice's console asked for a company
email, and there is no key for a person: its free plan ended on 2026-06-30 (the keys of that plan
were disabled), and what it sells now is for companies. A self-hosted assistant whose only way to be
woken by voice is an account with a company that does not give one to a person has no way to be woken
by voice, and a device that nobody sits at (a service, ADR 0025) needs the word: it has no key to
press.

The maintainer chose openWakeWord, an engine that needs no account, and said to download whatever it
needs.

## Decision

- **Two engines behind the one port, and openWakeWord by default.** `factotum.client_audio.wakeword`
  has `WakeWordEngine` (what `WakeWord` asks of an engine: `process(frame)`, `reset()`, `close()`, and
  the rate and the length of the frame that its microphone is opened at), `OpenWakeWordEngine` and
  `PorcupineEngine`. `wake_word_engine(engine, word, ...)` is the one place that knows which engine
  takes what (a key, a folder of models, a threshold), for the device and for the terminal alike, so
  that a client does not choose. `WakeWord` is given a *factory* of the engine and not an engine:
  the engine is made when the device first listens, off the thread of the loop (loading the models
  takes a third of a second, and Porcupine checks its key over the network), and it is made once, since
  a device that loses the edge does not open its microphone again (ADR 0024).
- **The runtime of openWakeWord is written again, on `onnxruntime` and `numpy`.** openWakeWord is
  three small models of ONNX that run in series: the spectrogram of the sound, the embedding of
  what a window of 76 rows of it says, and the model of the word, which judges the last sixteen
  embeddings. Its package asks for `scikit-learn`, `scipy` and, on Linux, a TensorFlow Lite runtime
  that has no build for the Python of this project (3.13), and a Raspberry Pi is Linux. What the package
  does between the models is a few lines (a frame of 1280 samples at 16 kHz, every 80 ms; the last 1760
  samples for the spectrogram; eight rows of it and one embedding for each frame; the first five scores
  are zero because the buffers hold noise until then), read from its source (Apache 2.0, version
  0.6.0) and written here in `OpenWakeWordEngine`, which is given its three models as sessions so
  that a test gives others.
- **It gives the scores of the package, to the last digit.** Measured against openWakeWord 0.6.0 in
  an environment of its own (it is not a dependency of this project, and was run on `onnxruntime`
  1.30.0), frame by frame: 14 pairs of a clip and a word (3 clips that its author keeps, and 8
  phrases said by the Italian voice of Piper) and the largest difference of a score is 0.0. The
  package is not here to be run, so what stays is that measurement, and the tests that need the real
  models (`e2e/tests/test_e2e_wakeword.py`, live).
- **The models are not in the repository, and nothing fetches them that was not told to.** They are
  their author's, under CC BY-NC-SA 4.0 (for use that is not commercial), and a project that may one
  day be used in a business must not carry them as if they were its own. `factotum-device wake-word
  download [WORD]` fetches the two models that every word shares and the one of the word, 5.6 MB for
  the four words, from the release `v0.5.1` of the author's repository (an address that is in the
  code, and no setting: nothing that a user controls is fetched, and `factotum.common.urlguard` is
  for what is). Each file is known by its size and its SHA-256, pinned in `WAKE_WORDS`: a body that
  is bigger than the file is not read to its end, one that is not the file is refused and nothing of
  it is kept, a file is written whole or not at all (a `.part` and a rename), and a file that is
  there and is what it should be is left alone. The folder is `~/.cache/factotum/openwakeword` (next
  to the voices of Piper) unless `listening.models_dir` says another; the command says the licence
  each time. A device that has no models says what to run (`ModelsNotThereError`, with the command
  and the folder in it), in the doctor and when it first listens, and its exit code is 1.
- **Four words, English.** `alexa`, `hey_jarvis` (the default), `hey_mycroft` and `hey_rhasspy`: the
  models that the author published with the release. `listening.wake_word` is checked when the
  settings are read (a word that has no model is said with the ones that have), and a Porcupine
  keyword (`jarvis`, `computer`...) needs `listening.engine: porcupine`. Training a model for a word
  of the house (`factotum`, or an Italian one) is what openWakeWord supports and what is not done here.
- **The settings.** `listening.engine` (`openwakeword`, or `porcupine`), `listening.threshold` (the
  score, above 0 and up to 1, at which the word is taken to have been said: 0.5, the author's own),
  `listening.patience` (the frames of 80 ms in a row that must be over it), and
  `listening.models_dir`. The key of Porcupine (`listening.access_key`, `$PICOVOICE_ACCESS_KEY`) is
  asked for only when Porcupine is the engine. The invariant of ADR 0025 stands: the secrets of a
  device are in the environment, which wins over the file, and in no line that it says.
- **What was said is forgotten after each utterance.** `WakeWord` makes the engine forget (`reset()`)
  after it has captured what follows the word, and the microphone's queue is drained first: the last
  sixteen embeddings that the model of the word judges would otherwise be those of what was just said,
  which may have the word in it.
- **A word is tried before it is trusted: `doctor --word`.** The doctor checks that the engine can
  be had (the models are there, are what they should be, and load; for Porcupine, that its package
  is) whenever a device waits for a word, and with `--word` it listens for five seconds and says
  how near the word came: the best score, and what to do (a threshold that would have taken it, say it
  nearer, another word). It is how a person chooses `listening.threshold`: the score of a synthetic
  voice (below) is not the score of theirs.
- **Porcupine stays, as an extra.** `libs/client_audio` depends on `onnxruntime` and has
  `pvporcupine` as the extra `porcupine` (`uv sync --all-packages --extra porcupine`); the adapter
  imports it when it is made and says what is missing when it is not there.
- **The terminal has the same engines**, with the same words: `--engine`, `--wake-word`,
  `--models-dir`, `--threshold`, and `--access-key` for Porcupine. It was the other user of `WakeWord`.

## Consequences

- **What a word scored when it was said by a synthetic voice** (the Italian `it_IT-paola-medium` of
  Piper, which says an English word the Italian way, with the default threshold of 0.5):

  | Word | Said as | Best score |
  | --- | --- | --- |
  | `alexa` | "Alexa" | 1.00 |
  | `hey_jarvis` | "Hey Jarvis" / "Ehi Jarvis" / "Hey Jarvis, accendi la luce" | 0.21 / 0.43 / 0.59 |
  | `hey_mycroft` | "Hey Mycroft" / "Ei Maicroft" | 0.00 / 1.00 |
  | `hey_rhasspy` | "Hey Rhasspy" / "Ei Raspi" | 0.01 / 0.02 |

  The sounds that are none of the words (a command, a conversation, "Giarvis") score at most 0.004
  with the model of `hey_jarvis`, and none of them is taken by any of the four. A synthetic voice is
  not a person: this says that a word can be out of reach for an Italian accent (`hey_jarvis` said
  plainly is under the threshold, `hey_mycroft` is not heard unless it is said as it is read in
  Italian), that `alexa` is the sure one, and that a threshold of 0.4 would have taken "Ehi Jarvis"
  and one of 0.2 "Hey Jarvis" (what a lower threshold lets in is not measured here, on a voice that
  says nothing else). Which is why `doctor --word` exists, and why `listening.threshold` and the word
  are settings: what a person's voice does is for the person to measure.
- **It costs little**: 2.1 ms of one core for each frame of 80 ms on the PC of the maintainer (2.6 %,
  with the one thread that the runtime is given), and 0.33 s to make. A Raspberry Pi has not been
  tried.
- **The models are non-commercial.** If this is ever used where that matters, a model has to be
  trained (openWakeWord has the pipeline) or the engine changed, which is one more engine behind the
  port.
- **Two more dependencies for the members that need them**: `onnxruntime` for the library (it was in
  the lockfile already: Piper and faster-whisper bring it) and `httpx` for the device, which fetches the
  models.
- **A device that waits for a word needs its models** before it listens, and says so when it does not:
  the exit code is 1, the service restarts, and the journal says what to run.

## Alternatives considered

- **Keep Porcupine and wait for a key.** Nobody who is not a company can get one, and this would leave
  a device with no way to hear a word.
- **The `openwakeword` package.** Its dependencies do not install on this project's Python on Linux
  (the runtime of TensorFlow Lite), and it pulls `scikit-learn` and `scipy` into a device that runs a
  microphone. What it does that matters is small, and the measurement says that it is the same.
- **The models in the repository.** Their licence is the author's and non-commercial, and 5.6 MB of
  files that are not ours are not what a repository is for.
- **Fetching them the first time that a device needs them.** A device that fetches by itself, when
  it starts, what it is going to run would reach out to the internet without anyone having asked it
  to, and a service that is restarted would do it again. A command that a person runs once, and a hash
  that says what came, is one step more.
- **Another engine** (a keyword search in a speech-to-text that never stops: it transcribes
  everything that is said in a room, which is the wrong thing to do all day and costs a model;
  Rhasspy's Raven: templates that the person records; Snowboy and Mycroft Precise: no longer
  maintained).
- **A word of the house, trained.** The right answer to a word in Italian, and a project of its own
  (synthetic data, training, a measurement on a real voice). The settings make a model another file,
  and the catalogue of words is the place for it.

## What was built, and what building it found

`libs/client_audio` (the engines, the folder of models and what knows them, the factory, the changed
`WakeWord`), `clients/device` (the settings, `wake-word download`, the doctor's check and `--word`, the
exit of a device with no models), `clients/terminal` (the same options), and the tests of each.

- **The terminal was the other user of the key.** Changing what `WakeWord` is given broke the terminal's
  `--voice`, which a search for who builds a `WakeWord` found before a person did: its tests give it
  a stand-in input and never build one, so they could not have said so.
- **A default that changes is a test that waits.** A test that gave a device a word and a key
  gave it, with the new default engine, a device that needs no key and waits for models that are not
  there: it did not fail, it waited for an edge that was never going to answer until it was killed.
  The tests that wake a device now say which engine, and the way of waking it that needs a key says
  so.
- **The first score of a stream is not a score.** The first five frames after an engine starts, or
  after it is made to forget, are zero whatever the model says, because the buffers hold the noise
  that they were started with; a test that listens for a short time (the doctor's) has to know that.
- **The same words score differently in a sentence.** "Hey Jarvis, accendi la luce" scored 0.59 and
  "Hey Jarvis" alone 0.21: the model of the word judges about a second and a third of sound (sixteen
  embeddings), the word and what is around it, so a threshold chosen on isolated words is chosen for
  something that is not said to a device.
- **A test that depended on the home of whoever ran it.** The code was broken on purpose, one way at a
  time, in 240 ways (the runtime, the folder of models, the factory, the input, the settings, the
  doctor, the command line, the downloader and the terminal); 25 of them passed the tests the first
  time. The one that said most was "the device is not given the engine that it was asked to use": a
  test of a device that waits for a word still passed, because the machine of the maintainer had the
  real models in `~/.cache/factotum/openwakeword`, and the real engine heard no word either. The
  tests of the three members that look in a home (the library, the device, the terminal) now run with
  an empty one (`HOME` and `USERPROFILE`, which `Path.home()` and `~` both read), and that test says
  that it was the device that asked for its engine. The others: the size of a model is looked at
  *before* the file is read (a very big file with the right name is not read), what a downloader
  cannot write is a line and not a traceback and leaves nothing half written, the made-up models
  refuse a type that the real ones refuse, a frame is handed to the engine off the thread of the loop,
  the environment's key wins over the file's, the edges of what the settings allow (a patience of 10,
  a threshold of 1) are allowed, and the doctor lets go of the engines that it made. Four did not
  need a test: the hash of a file decides what a size check would (`DL11`), the first five scores that
  follow a reset are zero and so clear the count of frames over the threshold that a reset also
  clears (`OE12`), the three models of openWakeWord have one output each, so the first and the last
  are the same (`OE30`), and the backend of the sound card is made before the command that has no
  use for one, which costs nothing (`CL9`).
- **Tried by hand**, on the Windows machine of the maintainer: `wake-word download` for the four words
  from the real release (every file was checked against its hash), the doctor's `wake word` check
  with the real models (they load), the measurements above, and the live tests (22, with the real
  models, the Italian voice of Piper and the real Whisper). Not tried: a person's voice through a real
  microphone (`doctor --word` is for that), and a Raspberry Pi.
