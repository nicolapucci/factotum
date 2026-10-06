# 0026. Saying something on a device: `play_audio`, `speak` through the edge, and core skills

Status: accepted, 2026-10-06.

## Context

[ADR 0025](0025-the-device-application-one-program-for-a-pc-and-a-raspberry-pi.md) left one thing that
a device of the application cannot do: say a text that a skill asks to be said (`speak`). A device has
no voice of its own, and the voice is the model gateway's, which a device does not reach. The edge
makes the voice of an *answer* (it asks the gateway for each sentence and sends the sound), but a
*command* goes through the edge as it is, with the text in it, so a `speak` reaches the device as words
and is answered `unsupported`. The `announce` skill fails on every device of the application, and so
does any skill that has something long to say.

What a skill makes a device say is not the feedback of the skill. The feedback ("Done.") is the text
of the answer to the person who asked: it goes the way every answer goes (text parts, filtered by what
the device can render, spoken sentence by sentence by the edge: ADR 0013 and 0023), and it is at most
2000 characters. What an announcement says is *content for a device*, often another one than the one
the person spoke to, and a skill may have more of it than a feedback holds (a recipe, the weather).

What was asked, in conversation with the maintainer: the gateway synthesizes the voice and the audio
is passed on to the device to play; a clip is a whole clip; a skill that has more to say says it as a
text; and skills that the platform itself provides ("core" skills), which may use the gateway
directly, are a convenience worth having in numbers, so that other skills can later call them.

## Decision

- **A command that carries a clip: `play_audio`.** `PlayAudio(text, audio)`: a clip of 16-bit mono PCM
  in a WAV container (`AudioPayload`, at most 2 MiB: about 47 seconds at the rate of a Piper voice of
  medium quality) and the words that it says. It requires `audio_out`, like `speak` and `set_volume`.
  The words are not for the device to say: they are for the trail of what was done (a clip is opaque,
  a text is not), for a device that shows what is said, and for the edge to fall back on (below). The
  vocabulary becomes version 2, and a frame (`CommandFrame`) with a clip in it is one frame of the
  protocol (at most 4 MiB, as a recording is).
- **`play_audio` is the platform's and not a skill's.** What a skill (a script, or a plan that a model
  writes) may give a device is `SkillCommand`, the vocabulary without `play_audio`; what a device may
  be sent is `DeviceCommand`, with it. A skill that could send any audio could make a device say what
  nobody can read in the trail, and a plan could not write a clip anyway.
- **A device takes it by a feature, and says when it is accepted.** `Feature.PLAY_AUDIO` is declared in
  the `hello` by a client that has a speaker, as `voice_stream` is, and is taken by a device that may
  have it (`audio_out`). The client core queues what it is given and answers the command `ok` at
  once, which means *accepted*, and not *played*. The chain from a person's message to a command is
  synchronous (the conversation service waits for the actions service, which waits for the edge, which
  waits for the device), so an `ok` at the end of a clip would hold up the answer to the person who
  asked for as long as the announcement lasts, in another room. What happens after it is the
  device's, as it is for an answer.
- **What is queued waits for quiet, in order.** A device says one thing at a time: what a command
  carries is played once no answer is being given on the device and nothing else is playing, in the
  order it came, so the feedback of a skill is said before the content that the same skill sent to the
  same device. A person who speaks while it plays cuts what plays (the answer is theirs to cut, ADR
  0022 and 0024), and what was queued behind it waits for the end of what they say. What waits is
  bounded by its size (what a clip holds, in all, is 12 MiB: a few minutes of speech) and not by its
  number, because a long text is many clips: a clip that does not fit is answered `failed`. A client
  that waits for its answer to be played (`wait_until_played`) waits for what is queued too, since a
  device that hears itself would take the announcement for a person.
- **`speak(text)` is made by the edge.** A session with a speech model, for a device that declared
  `play_audio`, cuts the text into sentences (what is said of an answer: `speakable`, the same
  chunker), asks the gateway for the voice of each in turn (`synthesize`, a whole clip), and sends
  each as a `play_audio` as soon as it is made, which the device plays as it arrives: the first sound
  is heard after the first sentence. The command is `ok` when every sentence has been accepted, and
  `failed` when one could not be made (the gateway, or no `tts` bound) or accepted. It is what makes
  `speak` work for every skill, and so for a skill that a person taught. A device that did not declare
  the feature is sent `speak` as words, as before. A `play_audio` for a device that did not declare it
  is sent to it as a `speak` of the words it carries: a clip that a device cannot play is a text that
  it may say, or show.
- **Core skills.** A core skill is a skill that the platform provides: it is *written in code*, runs in
  the process of the actions service with what that service has, and is found, chosen, authorized,
  confirmed and audited as any skill is. It has a `SKILL.md` like the others (a name, a description,
  examples, parameters, an effect), which is what the matcher and the planner read, and a function
  that runs it. What makes it a core skill is not a field that anybody can write in a manifest:
  - the text of its `SKILL.md` is in the code, and a stored version **is** the core skill when the
    hash of its content is the hash of that text. A person's own skill of the same name and another
    text is a skill of theirs, run in the sandbox as any is; the very same text is the core skill, and
    nothing is gained by it;
  - the first time a tenant is asked for (a message that is asked about, or its administrators
    listing, installing or reading the skills), the skills of the platform are installed for it and
    enabled, and left alone after that: one that an administrator disabled stays disabled until a new
    version of it ships, which is installed and enabled as a version of a folder is. What decides it is
    whether that version was ever enabled, so that a skill that was installed and never enabled (the
    service stopped between the two) is enabled, and one that was enabled and then disabled is not;
  - a service is given its core skills (`CoreSkills`) and has none without: the others (tests,
    stand-ins) are as they were;
  - what it asks of the platform is what it is given (a speech port, a clock), never the whole
    service, and it validates what it is given as the input that it is: a skill of the platform is
    called with what a message said, and later with what other skills say.
- **`announce` is a core skill.** It cuts the message into sentences as an answer is cut, puts them
  together in blocks of at most 400 characters (a clip is at most 2 MiB), asks the gateway for the
  voice of every block at the same time, and gives the device a `play_audio` for each, in order, and
  none if one voice cannot be made. The device is the one that was named, or the one that was spoken
  to: the rest of the decision is the actions service's, as for any command (the capability is
  checked, the device is the one it was aimed at, a command it did not declare is not given). The
  feedback to the person is what a skill says. The `SKILL.md` that was a skill of instructions in
  `skills/examples` is gone from there: the name is the platform's.
- **The commands of one run are given in order, and the first that is not done stops the rest.** It
  was not so (every command was given and the first failure was reported), which is harmless for a
  volume and a card and wrong for the sentences of a message: a clip that a device refused would
  have been followed by the one after it.
- **Not decided here: calls between skills.** A core skill is meant to be called by others one day, so
  what it is given (`RunRequest`) and what it answers (`message`, `data`, commands) is the same
  whoever asks, and nothing in a core skill knows that it was a message that chose it. How a skill asks
  for another (a call returned as data, declared in the manifest, authorized with the same principal,
  audited as a child, bounded in depth) is for a decision of its own.

## Consequences

- **`speak` works on every device that has a speaker and a voice behind the edge**, for every skill,
  with the sound of the Piper voice that the tenant bound; with no `tts` bound it is `failed` (it says
  so), not `unsupported`.
- **The first sound of a long `speak` comes after the first sentence**, and the sentences follow with a
  gap that is the time of a command and a queue (a few milliseconds on a network of a home); the
  voice of an answer, which is a stream, has none. A voice of a person (the shape of a sentence) is
  made one sentence at a time either way.
- **`ok` of `play_audio` is accepted, not played.** A device that fails to play it, or that a person
  speaks over, says so in what it shows and nothing else: the skill has been told that it was done,
  and what it did was give it to the device. A speaker that is unplugged loses the clip it was playing
  and goes on with the next (what ADR 0024 says of an answer).
- **The edge needs a speech model to make `speak`**, and its HTTP interface takes a body of one frame
  (4 MiB) where it took 64 KiB, because a command may carry a clip. Everything that calls it is a
  service that authenticates.
- **A core skill is code of the platform in the process of a service**, with what that service has:
  a mistake in one is a mistake of the service, and what it is called with is somebody's words. Each
  needs a strict reading of its parameters, a limit on what it makes (a clip costs a model's time),
  and tests with two tenants; and the platform's skills are only what cannot be said as a request or a
  command that a sandbox may make (the reason for each is in its ADR or its docstring).
- **An administrator can disable a core skill** and it stays so; a new version of it is enabled again
  where it ships.
- **What does not change**: the sandbox, the proxy and the plan of a skill that is instructions (ADR
  0015 and 0016: a core skill does not run in them, and a plan cannot give a clip); `authorize()`; the
  confirmation and the approval of an action; the rule that a script reaches only what it was granted;
  the vocabulary that skills may use but for one command.

## Alternatives considered

- **The skill asks the gateway for the voice by itself.** For a skill of a person, the sandbox has no
  way to the gateway (the network of a container is the proxy's, and the proxy refuses the
  machine's own services), no credential for it, and no way to a device but through the commands it
  returns as data. And what it would return is a clip, which is opaque to the trail and to the
  checks. A core skill is not in the sandbox, which is why it may.
- **`ok` when the clip has been played.** It is the truer answer and the wrong one for a synchronous
  chain: the person who asked an announcement in another room would wait for it to end to hear that
  it was done, with the timeouts of three services in the way.
- **Frames of voice for a command** (a new family tied to the id of a command, as the sound of an
  answer is tied to its utterance). Gapless, and a new set of frames, of checks in the client core
  and of ways to be wrong, for what a clip per sentence does in a few milliseconds more.
- **A cache of voices in the gateway, and `announce` as a skill of instructions.** It would avoid
  making the same voice twice and nothing else: the core skills are wanted as pieces that others can
  use, and a clip made once and sent to many devices is the same saving with the skill at the place
  where the devices are chosen.
- **The device makes the voice.** A model in every device, against ADR 0008.

## What was built, and what it found

Built as decided. The vocabulary is version 2 with `PlayAudio`, and `SkillCommand` is what a script,
a plan and a skill of a person may give (`SKILL_COMMAND_MODELS`); `Feature.PLAY_AUDIO` is declared by
a client with a sink or a speaker of clips and `audio_out`. The client core takes a clip at once,
says it when nothing else is, in order, from a task of its own, bounded by 12 MiB and cut by a person
who speaks, and says a clip for a device that did not declare the feature as a violation. The edge
makes a clip of each sentence of a `speak` (`factotum.common.sentences` is the cutting of sentences
that the edge had, moved where the actions service can use it) and sends a `play_audio` for a device
that did not declare the feature as the words it carries. In the actions service, `factotum.actions.core`
has the registry (`CoreSkills`), the base of a skill (`CoreSkill`) and `announce`; the service runs
them and the catalog provides them. `skills/examples/announce` is gone, the example home has the
platform's. Everything is tried at the layers it is at (the registry, the skill, the service with the
skill, the catalog, the edge, the client core, the hands of the two devices), and as a person meets it
(`test_e2e_announce.py`, the series, the hub's house, the device application).

What building it found, besides the mistakes that a test caught as they were written:

- **The time limit of a skill is 10 seconds unless it says, and a voice may take longer.** The first
  clip after a start loads the voice (4.9 s on a processor), and a message is more than one clip: the
  skill declares 60, and a test says that it is not less than what one voice is waited for.
- **What a person says is a phrase.** The values of a message are checked as text of at most 500
  characters, and white space is made one space, so an announcement that a person asks for is one clip
  or two. The limit of the skill itself (2000 characters, what a command may hold) is for what other
  skills will ask for.
- **A task group raises a group.** The voices are asked for at the same time with `asyncio.TaskGroup`,
  and the first failure came out of it as an `ExceptionGroup`, which the service logged as *a runner
  failed* and wrote in the trail as `ExceptionGroup`. A failure of the voice is now one error that says
  what kind it was (`the voice could not be made (gateway_unavailable)`), never what the gateway said,
  and what is not an error of ours is let through as it is.
- **Enabling "when it was just installed" loses a skill.** The first rule of the registry enabled a
  skill when the install had added a version, and a service that stopped between the install and the
  enabling left it there for good, since the next start found nothing new. What decides it is whether
  the version was ever enabled (a disable leaves that mark).
- **A clip per sentence is too many clips for a message.** The first `announce` made a clip of each
  sentence, which for a message of 500 characters is a dozen requests of the gateway at once and a
  dozen commands to the edge; the sentences are put together in blocks of 400 characters, so it is
  one clip or two, and the tests say where the edge of a block is (a sentence is never cut, and a block
  that is full to the last character is a block).
- **A device that was given a clip it did not ask for answered `ok`.** The hands of the terminal had no
  case for `play_audio` and a `match` that falls through is a result that says it was done; they say
  *unsupported* now, as the hands of the device application do, and the client core does not ask the
  hands at all for a clip that its device did not say it plays.
- **What was proved by an end-to-end test that was a limitation.** The test of the device application
  that asserted that a text to say is what a device cannot do is now the test that says that it does it
  (and says what the platform's own skill and a skill of a person each come to on it).
- **A series that read a device before it had finished.** The series looks at what every device was
  made to say after one was spoken to, and an announcement is said on another device while the answer
  is given on this one: it waits for each to be done before it reads, which it did not and which only
  worked because a fake plays at once.
- **A catalog that is shown first.** A tenant's administrator who opened the list of skills before
  anyone had said a word saw no `announce`: the catalog provides the skills of the platform too, not
  only the first message.
