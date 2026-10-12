# 0028. The model alone does nothing: a request is satisfied by skills, or it is declined

Status: accepted, 2026-10-12. Built: slice A (2026-10-12) and slice B (2026-10-13).

## Context

The first spoken session on the maintainer's machine (2026-10-12) showed three things.

*"Che ore sono"* was answered "Le ore sono 09:00" at 13:47. No skill gives the time, so
[ADR 0014](0014-actions-the-model-proposes-the-code-decides.md) made the message a question, and the
conversation service let a model answer it: its prompt gives it the date, and says that it "may add what
it knows if it is sure". It made the hour up. *"Alza il volume"* ran the volume skill with a level of 100
that nobody said: the code accepts a value that has the type the skill declares and is inside its
bounds, and a number that a model made up is both (ADR 0014 says that "a value is never invented", and
that was a line of a prompt and nothing in the code). And a skill that fails is told as *"Non ha
funzionato"*, with no word of which one.

They have one cause: the assistant takes what its skills cannot do to be something that its model can.
A model that answers out of what it believes makes what the assistant does depend on what that model
happens to hold, and the audit trail does not say why it said what it said. The maintainer's rule is the
opposite: the model by itself can do nothing, and what the assistant can do is its skills.

## Decision

- **A model answers nothing by itself.** Whatever is asked, a question or a command, is satisfied by one
  or more skills. A model reads the message to choose them and to find what they need (ADR 0014), and it
  may put into words what it is *given*: the passages that the documents returned (somebody else's words,
  as ever: ADR 0011) and the earlier exchanges that a message refers to (ADR 0012). What a skill says of
  its own result is said as it is (ADR 0016). A model never makes a fact of its own, and with nothing to
  work on it is not called.
- **Two kinds of "no", and a person is told which.** Not finding a skill and a skill that fails are both
  failures, and different ones.
- **No skill fits: the assistant says so, before anything is tried.** *"Non ho trovato una skill
  adatta."* A sentence that the code chooses, in the language of the message like every sentence of the
  assistant (`phrases.py`). It is the same for a question and for a command, for a greeting too, and it does
  not depend on which skills exist: with the core skills that are not built yet (the time, small talk, help)
  the assistant behaves exactly as it will when they are, and nothing in the code, the prompts or the tests
  treats the time, or anything else, as a special case. What ADR 0014 called a question, a message that no
  skill is near, is now a message that no skill fits (`no_action` becomes `no_skill`). Whether something
  else can answer it (the documents, below) is the conversation service's business, and so is the
  sentence: it is said in the language of the message, which the conversation service reads itself, with
  no model, by counting the small words of the languages it has sentences in (a statistical detector
  tells nothing of "ciao"), and in the language it is set to when it cannot tell.
- **A skill that was found and fails: the assistant says so, in the skill's own words if it has some.** The
  skill was the right one and could not do it. It may say why, for a person: *"Su meteo.it non ho trovato la
  città."* A script prints it as `error` where it would print `message` (a run that reports a failure ends
  well, with an exit code of 0: one that crashes is not a report); a skill of the platform raises an error
  that carries words for a person; the documents say *"Non ho trovato documenti pertinenti."* Those words
  are spoken as a `message` is: as they are, up to the same length, with the secrets taken out of them by
  the runner, and never read by a model. When the skill has none (it crashed, it timed out, it said nothing,
  or it is a skill of instructions) the sentence names it: *"La skill weather ha fallito."* The same for a
  runner that cannot be reached and for a command that a device did not do. What the trail and the log keep
  is the kind of the failure (its code), as ever. What this refines is ADR 0015's "what a failure says is
  never what the script said": a script's own report is its words, as its message is; what it printed
  without meaning to say it (its standard error, a traceback) and what a service answered to it still never
  reach a person.
- **Looking in the documents is a skill, and the model is shown only what is relevant.** Searching a
  person's documents is something the assistant does: it has a name (`documents`), a person is told when
  it is being done (the `searching` phase), it is audited like any skill, and it **fails** when it finds
  nothing relevant, in its own words. Relevance is decided by the code *before* a passage reaches a
  model. The score that a passage carries today cannot do it: it is a fusion of ranks (ADR 0011) scaled
  by the best a rank can score, so it says how high the passage came and not how well it answers, and a
  question about nothing has a first passage too. What can is the raw similarity of the vector search,
  with a floor; the floor, and what the side that searches by words must show to count, are measured with
  the real embedding model and a real collection (live tests) and not chosen. When no skill fits, the
  documents are what is tried before giving up, and a person who has nothing relevant there either is told
  both in one sentence (*"Non ho trovato una skill adatta, né documenti pertinenti."*): not the first alone,
  because the documents were looked in, and not the second alone, because a command is no question about
  documents. The same goes when the skills or the documents cannot be reached: it is said which. Until
  the core skills are built it stays in the conversation service and behaves as a skill; when it is built
  as one (ADR 0026) it moves, and what the conversation calls is already the shape of a skill (a name, a
  result, a failure with a kind and words) so that the move is a move. "If the sources do not answer, say
  so" stays in the prompt as the second line of defence and never the first.
- **The manifest says what a skill needs, and a model invents nothing.** `required` is what must be
  given, and everything else is optional. A required value that was not said is asked for, as today (*"Mi
  serve ancora: la città."*). An optional value that was not said is left out, and the skill, or the device
  it acts on, applies its default: *"alza il volume"* calls the volume skill with no amount, and the skill
  or the speaker raises it by its usual step. To keep a model from filling in what it was not told, the
  code checks that a value it wrote traces back to the message, and a value that does not is treated as not
  said. The check is in code and the model is not asked to quote the words a value came from: a quote
  proves that the model pointed at some words and not that they say the value, and it costs tokens in every
  request. A number is traced by finding it in the message (in digits, or in words in the two languages the
  assistant has sentences in), or by being the end or the middle of the range that the skill declares for
  it, since an amount can be said with no digit in it (*"al massimo"*, *"a metà"*); a text is traced by its
  words being the message's. A choice from a list (*on*, *off*) is the model's reading of the words and no
  rule in code can search for it: it is left to the checks of the choices and, where an action has an effect,
  to the person's yes. Because a manifest cannot say that one of two values is required, setting the volume
  to a level and turning it up or down are two skills (`volume` and `volume-change`), each of which asks for
  what it cannot do without. The default step belongs to the skill (the script of `volume-change` turns it
  up by 10) and the command that reaches a device says how much (`adjust_volume`, a number of points): a
  device does what it is told and has no step of its own.
- **One or more skills, though one at a time for now.** The rule is stated for any number of them: a
  request is satisfied when every part of it has a skill that ran; a part that has none is declined, a part
  whose skill failed is said to have failed, and what was done is still said. Today a message chooses one
  skill, and skills calling skills is still the open decision of ADR 0026, but nothing built from now on
  takes it for granted that there is exactly one: a sentence names its skill, the trail has a record for
  each, and the reply to a message is put together in one place from the outcomes of its parts.
- **Left out, on purpose.** The core skills themselves (the time, small talk, help, the documents and the
  history as core skills): they are written last, all of them are the same case, and the assistant works
  the same whether they exist or not. Several skills in one request. A default declared in the manifest:
  for now the skill or the device applies it.

## Consequences

- What the assistant can do is its list of skills and nothing else: whoever reads the catalog and the trail
  knows everything it can say or do. A behaviour that depends on what a model believes is a bug.
- An assistant with few skills says *"non ho trovato una skill adatta"* a lot, which is the point: a no is
  cheap and true, and a made-up answer is neither. A message that no skill fits and that has nothing
  relevant in the documents costs an embedding and no answer from a model.
- Greetings, thanks and *"come ti chiami"* are declined until a core skill answers them.
- The documents answer less: a question they do not address is declined and not answered from the model's
  memory. The floor is the price of that: too high, and what is in the documents is refused; too low, and a
  passage that does not answer gets in.
- Superseded in part: ADR 0014 (a message that no skill is near is a question), ADR 0015 (what a failure may
  say), the invariants of `CLAUDE.md` that say the same, `docs/actions.md`, `docs/conversation.md`,
  `docs/skills.md` and `docs/development.md` where they say it, and the prompt of the conversation service,
  which loses "you may add what you know" and the date (a clock that is not a skill: *"che giorno è oggi"*
  would be answered from the prompt).
- *"Alza il volume"* is called with no level. `set_volume` is absolute (a level, 0 to 100), so the volume
  needs a command that is relative (`adjust_volume`), and the devices and the clients that run commands
  learn it (vocabulary version 3).
- The work was two slices, both built. A: the two sentences of "no", the documents as a skill with its
  floor, the skill's own words for a failure, and the prompt. B: a value that cannot be traced to the message
  is not believed, and the volume that is turned up or down with a default step.

## Alternatives considered

- **Give the model the time and tell it not to guess.** It leaves the rule to a prompt, which a small model
  keeps most of the time, and it puts a clock in the model: a skill outside the skills, and the same
  trouble for the next thing it does not know.
- **Let the model write the refusal.** The sentence would vary, and be wrong sometimes. A fixed sentence
  is one that a test checks in each language.
- **One sentence for every "no".** *"Non sono in grado"* says nothing about whether a skill was missing or
  a skill broke, and that is the first thing a person trying to fix it asks.
- **Make the documents a skill that the planner chooses, now.** It is where this goes, and it moves the
  answer that is written a sentence at a time (ADR 0013, 0022 and 0023) and the history out of the
  conversation service in one step. The rule does not need that, so it waits for the core skills, with the
  shape prepared.
- **Ask for every value, optional ones too.** A voice that asks *"di quanto?"* when told to turn it up is
  worse than one that turns it up by a step.

## What was built, and what it found

The first slice (A), in four steps. `no_action` is `no_skill`. A skill that failed is named in every
sentence of a failure (`phrases.py` of the actions service), and a script that ended well can say why it
could not do what was asked with an `error` (`SkillOutput`, `SkillFailedError`): those words are
spoken as a `message` is and the trail keeps them; the weather example reports a city it did not
find that way. The knowledge service gives a passage the raw `similarity` of its section and a search
a `min_similarity`, and counts what it left out. The conversation service asks for the floor, asks no
model when there is nothing to answer from (no relevant passage, no exchange that the message refers
to, no skill) and says which of the two was missing, and which could not be reached, in one sentence of
its own (`phrases.py`), in the language of the message (`factotum.common.language`). The prompt no
longer tells the date or lets the model add what it knows, and tells it that a message which asks for
something to be done is not a question for the sources. The documents and the history are not yet a
skill of the platform: they behave as one (a name, a phase, a failure with words) in the conversation
service.

What the measurements found (`docs/knowledge.md`, `docs/conversation.md`):

* **The score of a passage could not have decided anything.** It is a fusion of ranks, so a question
  about nothing had a first passage at 1.0; the raw cosine is what separates, and it separates well but
  not completely. With `bge-m3` a floor of 0.5 keeps all of 44 questions in English and 34 in Italian
  over a book of 2956 pieces, and 25 of 26 over a small library in two languages (the one it loses is
  an English question about an Italian policy, 0.49), and leaves out 79% of the messages that are about
  nothing in the book and 88% of those in the library. What gets through is mostly a command that names
  something a document has (*restart the router* is 0.60 to its manual, *turn on the oven* 0.61 to a
  recipe) and, once, the time to a trip with times in it (0.51).
* **Told only to say what the sources say, a model of 8B restarts a router by erasing it.** With the
  manual near it answered *restart the router* with the way to reset it 4 times of 4; with the
  instruction that a message which asks for something to be done is not a question for the sources it
  said that it cannot 5 of 5, and it did the same for the oven, the wifi password to delete and *can
  you turn the oven on?*. In Italian it is weaker (it refused *riavvia il router* and then quoted the
  reset 4 times of 5). The second line is the model's and is not exact: the first (the floor, and the
  model not being asked) is the code's.
* **A statistical detector of the language cannot be used on what people say.** `langdetect` tells
  nothing, or the wrong thing, of 10 of 60 short messages (*ciao*, *grazie*, *tell me a joke* are
  Portuguese, Polish, Finnish); counting the small words of Italian and English got 59 of 60 and no
  mistake, and the one it cannot tell (*meteo Carrara*) is answered in the language it is set to.
* **Old tests that asked for a plain answer from a model with no sources were testing the thing
  this record removes**: 26 of the conversation service and 10 of the end-to-end ones (the same
  five tests, run twice) had to be given a passage, or to say what the code says instead.

### The second slice (B)

A value is one that the person said, and the volume can be turned up or down. `adjust_volume` is a
command of the vocabulary (a `delta` from -100 to 100, version 3), run by the system device, the speaker
(which keeps the level between 0 and 100), the hands of the device application and the terminal.
`volume-change` is a skill with a direction (required) and an amount (optional): its script turns the volume
by the amount if there is one and by 10 if there is not. `factotum.actions.grounding` finds out whether a
message says a value; `check_values` leaves out what it does not say (`Checked.unsaid`: an optional value is
not passed on and a required one is asked for), the service logs the names of what was left out and never
the values, and the question that asks for a value that is missing is replaced by the sentence of the code
when the model wrote it in another language than the message.

What the measurements found (`docs/actions.md`; the 8B model of `chat.fast`, 63 commands and 27 questions
said to the three devices, in English and Italian):

* **The check works with the real model.** *Metti il volume al massimo* is a level of 100 and *a metà* one of
  50 (the ends of the range), *di quindici* is an amount of 15, and *turn the volume down a lot* came with
  an amount of 100 that nobody said, which was left out, so the skill turned it down by its step. *Turn it
  up* and *make the volume a bit lower*, which the model had answered with a level of its own in two
  earlier measurements, are done by `volume-change` with a direction and no amount: 8 of 8, and the
  volume 7 of 7 (it was 6 of 8).
* **A model translates, and the check refuses it.** *Dì che è ora di andare* came with the message *it's
  time to go*; the person was asked what to say. Two examples in the prompt that copy the words as they were
  said (one in English, one in Italian) took the announcements from 3 of 6 to 6 of 6. A text is accepted
  when its words are the message's, in any order, so *everyone, dinner is ready* for *tell everyone that
  dinner is ready* is said: it invents nothing, and a stricter rule (the words must follow one another)
  would ask for what was said.
* **The question was in English because the only example of one was.** *Alza il volume* asked *What
  percentage do you want the volume to be?* before the examples had a question in Italian. Now it asks
  *Vuoi alzarlo o abbassarlo?* for *cambia il volume*, and a question that comes in the wrong language is
  not said.
* **The prompt of a small model moves a case or two with every change.** Of the variants that were measured,
  the one that is kept got 60 of 63 commands exactly as meant (it was 48 of 55 over the earlier corpus), 61
  of 63 with the right skill and 26 of 27 questions left as questions; an example more or a sentence
  changed sent *turn it up* or *could you please turn the light on* to *needs more* and brought the garage
  from the phone back, and every one of those was one case of 63. What stays wrong is the model's: *could you
  dim the lights to thirty percent* goes to the scene, *could you open up the garage for me* from the phone
  asks which garage, *reboot the wifi* is no skill, and *what is the weather usually like in Rome in
  August?* is taken for a request.
* **Fourteen tests of the service and one of the end-to-end suite said a value that their message did
  not.** They were given a message that says it, which is the point of the check.

Not built, as the record says: the core skills (the time, small talk, help, the documents and the history as
skills of the platform) and several skills in one request.
