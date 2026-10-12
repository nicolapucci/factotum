# 0014. Actions: the model proposes, the code decides

Status: accepted, 2026-09-21. What it says of a message that no skill is near (it is a question, and a
model answers it) is superseded in part by
[ADR 0028](0028-the-model-alone-does-nothing-a-request-is-satisfied-by-skills-or-declined.md):
such a message is declined, unless the documents have something relevant to answer it from, and a
value that a model wrote is believed only when it traces back to the message.

## Context

A voice assistant that can only answer is safe. One that can switch a lamp on, open a garage door or
restart a router has to decide, for every message, whether it is being asked to do something, which
of the things it can do, on what, and whether it may. Three facts shape that.

The reader of the message is a small model that is often wrong in small ways: a number as text, a
device that does not exist, a question taken for a command. What it says cannot be what is run. The
things that are run are skills, which are somebody's code, and a skill that was reviewed on Monday
must not be a different skill on Tuesday. And what is said is not who says it (ADR 0004): a shared
speaker in a living room is spoken to by a guest as much as by the owner, so what a device may start
has to be a property of the device.

## Decision

**A message goes to the actions service before it goes to the documents, and the model only
proposes.** The conversation service asks whether the message asks for something to be done; if it
does, what the actions service answers is the answer, and if it does not, the message is a question
as ever. A small model (`chat.fast`, the user's choice of model) reads the message *alone*, never
the conversation, a document or a page of the web, together with the few skills that are near it,
and proposes: which skill, which device, which values, in which language, and what to ask. Then code
takes over. The skill has to be one of those offered, the device one that the caller may use and
that the skill is set up for, the values what the skill declares (a slip that leaves no doubt, a
number written as text, is corrected; anything else is not accepted); what fails is *nothing to do*
or *needs more*, and in the second case the person is asked. A value is never invented. What is
left goes to `authorize()` with the policy of the device that was spoken to: a personal phone may
start what a shared speaker may not.

**A message is compared with the skills by meaning first, and the model is asked only for those that
are near.** What a skill says about itself (its description and each example) is embedded with the
embedding model of its owner and kept in that model's space; a message is embedded, and only the
skills above a threshold go on. Most messages are questions and are far from every skill, so they
cost one embedding and no model call. A skill that has not been indexed yet, or whose owner has no
embedding model, is not lost: it goes on unscored, up to a limit, so that a skill that was just
installed works before it is indexed.

**A skill is a folder, in versions that never change, and enabling a version is the review.** A
`SKILL.md` (YAML frontmatter, the manifest of ADR 0006, then instructions) beside its scripts. A
version is identified by a hash of every file, so changing a script is a new version and reading the
same folder again is not. At most one version runs; nothing runs until somebody has enabled one, and
enabling grants exactly what that version declares (the hosts, the secrets, the accounts). A new
version does not run until it is enabled in its turn, so what changed is looked at again, and going
back to an older version is enabling it. A person manages their own skills, the tenant's are the
administrators', and none of it is done through a device.

**A yes is a confirmation and nothing more, and it is checked again when it comes.** When the
decision is to ask, what was understood (the skill and its version, the values, the device, the
decision) is kept for a minute, one at a time for a person on a device, and the next message on that
device is read as the answer: by its words where they leave no doubt (English and Italian), by the
small model where they do, and whatever is not clearly a yes is not one. A yes with something added,
a question, a long message, a model that cannot be reached are all *other*, which drops the question
and takes the message as a new one. A yes is taken once, however many arrive. When it is taken,
nothing that was true a moment ago is assumed: the version may have been changed or disabled, the
device removed, the policy tightened, and the action is decided again.

**An approval fails closed and, when there is one, it replaces the confirmation.** An action that
needs an approval goes to an `Approver` (ADR 0007), and while no mechanism is configured the answer
is that there is none and the action is refused with a plain reason. The interface has grown as that
ADR expected it would: it answers with an `ApprovalResult` (the outcome, and who approved) rather
than an outcome alone, because the trail has to say who. An approval is a stronger thing than a
spoken yes, so an action that has one is not asked again.

**Every step is audited, and the trail cannot say what did not happen.** Asked, declined, refused,
executed: each leaves a record with the skill, the device, the values (masked where they look like
secrets), the decision and its reasons, the confirmation and the approval. The contract refuses a
record that says something ran without the confirmation or the approval its decision required. A
trail that cannot be written is logged and does not undo what was answered: the trail is kept with
everything else the service keeps, so that it cannot be written while the rest can is rare.

**What runs, and what is told to a device, are interfaces, and what stands in for them says so.**
`SkillRunner` runs a version of a skill and answers `{message, data, commands}`; `CommandDispatcher`
gives a command to a device and answers what became of it. Slice 5a builds everything around them for
real and puts stand-ins on the two ends: the runner says what it would have run and the answer is a
dry run all the way to the trail, where it is *not executed*; the dispatcher reaches no device.
Slice 5b puts the isolated runner and the edge behind them, and nothing above them changes.

**What is said is not generated.** The answer to a request is one of a few sentences that say what
happened (done, not allowed, needs somebody's approval, could not be reached) in the language the
person spoke, which the small model reads out of the message. English and Italian are there; another
language is answered in English until somebody adds it, and a test says that no sentence is missing.
What a skill says of its own result, and the question the model wrote to ask for a yes, are said as
they are.

**A service that cannot be asked never holds up a question.** If the actions service is away or
slow, the message is answered as a question and the turn says so in a warning; if the planner cannot
be reached, the message is not an action, and the outcome says that it was not read so that the turn
says so too. Nothing runs on a guess, and nothing waits for a model.

## Consequences

- The model can be wrong about anything without doing harm: what it gets wrong is corrected, asked
  about, or dropped before anything with an effect is decided, and what is decided is the same code
  whichever model is behind it. The price is a second small-model call for a message that is near a
  skill, which the meaning search keeps to the few.
- A skill's version is what was reviewed, so a supply-chain change is a new version that does not
  run until somebody has looked at it; the catalog lists the versions, their hashes and what each
  was granted.
- A confirmation costs a row and a minute, and lives in the database, so it survives a restart of
  the service and is seen by every instance.
- What a device may start is configuration of the device, and it is decided in one function
  (`authorize`), which the actions service calls and does not reimplement.
- The stand-ins mean that slice 5a can be tried end to end (a message, a decision, a question, a
  refusal, a trail) without anything being switched on; the last step from "would have run" to
  "ran" is slice 5b, and it is the only one that has an effect on the world.
- Approval is still not deliverable: a skill that requires one is refused until a mechanism is
  chosen, which is the deliberate outcome of ADR 0007 and is said to the person in words.
