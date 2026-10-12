# 0028. The model alone does nothing: a request is satisfied by skills, or it is declined

Status: accepted, 2026-10-12.

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
  skill is near, is now a message that no skill fits (`no_action` becomes `no_skill`, and it says the
  language of the message: the planner reads it, and where no model is asked, because the message is far
  from every skill, the code does). Whether something else can answer it (the documents, below) is the
  conversation service's business.
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
  code checks that a value it wrote traces back to the message (the words it was taken from are in the
  message), and a value that does not is treated as not said. How exactly is built and measured with the
  real model, since an amount can be said with no digit in it (*"al massimo"*, *"a metà"*).
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
- `volume` has to be called with no level. `set_volume` is absolute today (a level, 0 to 100), so *"alza il
  volume"* needs a command that is relative, and the devices and the clients that run commands need to
  learn it.
- The work is two slices. A: the two sentences of "no", the documents as a skill with its floor, the
  skill's own words for a failure, and the prompt. B: a value that cannot be traced to the message is not
  believed, and the volume skill that is relative with a default step.

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
