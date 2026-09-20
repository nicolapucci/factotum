# 0012. Conversation: no history in a turn, and a log to search when a message refers to it

Status: accepted, 2026-09-21.

## Context

The usual way to give an assistant a memory is to send the last N messages of the conversation with
every request. For this assistant that is the wrong default. Most of what is said to a speaker or
typed to a terminal is a request that stands alone ("what does the warranty not cover?", "turn off
the lamp"). Sending the history anyway costs tokens and time on every turn, lets an old topic bend
a new answer, and carries whatever an earlier answer quoted from a document (or a web page) into
every turn after it, where it can act as an instruction.

People do refer to what was said before, and the assistant has to follow: "and in English?", "why?",
"what did you tell me yesterday about the router?". That is the only reason to keep the history.

## Decision

**A turn is stateless.** The model gets the message and the passages found in the user's documents,
and nothing of the conversation, unless the message refers to it.

**Every exchange is saved, to be consulted.** When a turn ends, its question, its answer, the
documents it cited and when it happened are written to a log that belongs to whom the turn acted
for (a user, or the tenant for a shared device). Nothing else keeps the text of a conversation.

**A small model call decides whether a message refers to an earlier exchange.** It sees only the
message (never the history, so it stays cheap and cannot be steered by it) and answers with a
structured value: no reference; a reference to *the exchange just before* (a continuation: "and in
English?", "why?", "the second one"); or a reference to something *said earlier* (with the words to
look for and, if the message says when, a day, a week or a month). It uses the `chat.fast` role, and
it is not made at all for someone who has no exchange yet. If it fails, the turn goes on without
history and says so: a follow-up is asked again, an answer is never held up.

**The small model is asked in the way that any small model gets right.** The user chooses the model
(ADR 0008), and asked for the label straight away three small models were wrong in three different
ways: one called nearly every follow-up a message that stands alone, another called nearly every
message a follow-up. What made all three right was two things: the examples are given as turns of
the conversation the model is in, not as a list in the instructions, and the answer says *why* in a
few words before it says which kind. The numbers are in `docs/conversation.md`.

**What is consulted is only what was referred to.** A continuation brings the last exchanges of the
conversation (three). A reference to something earlier searches the log, by words and by meaning at
once, restricted to the day, week or month asked for, and brings the best three. Each comes with
its date. They are given to the model as quotations of an earlier conversation, not as instructions.

**The search of the log is the conversation service's own.** It is the same idea as the search of
documents (words and meaning, fused by rank, degrading to words when there is no embedding model)
over a much smaller and simpler corpus: short exchanges, appended one at a time, of one owner. The
vectors are made after the turn, in the background, through the gateway, so a turn never waits for
them; the rows remember the space their vector lives in, and a change of model is noticed like it is
for a collection. Only the small shared pieces are shared: rank fusion, and the way to reach the
gateway (now `libs/model_client`, which the knowledge service uses too).

**Whatever an answer quoted stays marked.** An exchange records whether its turn had untrusted
content in it (a passage of a document). A turn that brings such an exchange back has untrusted
content too, so the rule that a document must not be able to order an action follows the text
through the log instead of being lost the moment it is saved.

**A conversation is a thread of exchanges, and knowing which one is not the client's job.** A client
that has an idea of a thread (a web page) names it. One that has not (a speaker) gets the
conversation of its subject and device that was active in the last half hour, or a new one. Exchanges
are saved when the answer is complete; one that failed or was abandoned leaves nothing.

**The log is the user's.** It can be listed, read, deleted one conversation at a time or all at once,
and expires after a number of days if the deployment says so. Only a person reads or forgets it, and
only their own. A shared device can be spoken to and remembered (its turns use, and add to, the log
of the tenant) but is never a way to read or delete what the household said: administration does not
go through a device (ADR 0004), and the log of the tenant will be managed from the hub.

**A period that a model read is a hint.** The day, week or month that the small model finds in a
message narrows the search of the log; when nothing in it matches, what was said at any time is
searched before giving up, because a model now and then names a day the message did not, and what
was asked for is then not lost. A period that a person asked for through the API is a wall.

## Alternatives considered

- **Send the last N messages every turn.** Rejected: the cost, the noise, and the carried
  instructions, described above.
- **Index the log in the knowledge service as documents.** Rejected for now. Changing a collection
  is administration by a person, never by a device (ADR 0004), and a shared device acts as the
  tenant; the history must be left out of every default search of documents; it must be deletable
  and expirable by itself; and it should work when the documents do not (no collection, no embedding
  model). Knowledge remains the place for what a person gives the assistant, not for what the
  assistant remembers of the conversation. This can be revisited when the hub issues service
  credentials (slice 7).
- **Decide by rules whether a message refers to the past** (cue words). Rejected: it does not work
  across languages, and a missed reference is invisible. It may come back as a way to skip the model
  call for messages that are plainly self-contained, if the call turns out to cost too much.
- **Summarize the conversation as it goes.** Not needed: nothing is carried forward, so there is
  nothing to shorten.

## Consequences

- A turn costs one small model call more when there is something to refer to, and nothing more
  otherwise; a message that stands alone carries the same context whether it is the first of the day
  or the thousandth.
- The assistant does not remember a fact by itself: it recalls an exchange when it is pointed at.
  A message that refers to the past without saying enough to find it gets an answer that says it
  does not know what is meant.
- The log grows with use. Searching it is exact over one owner's exchanges, which is fast into the
  tens of thousands; an index comes when that stops being true.
- Long-term memory of facts ("my mother's birthday is on the third") is a different thing and is not
  part of this decision.
