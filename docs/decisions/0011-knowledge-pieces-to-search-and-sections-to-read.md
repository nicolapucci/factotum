# 0011. Knowledge: pieces to search, sections to read, and a search that degrades

Status: accepted, 2026-09-20.

## Context

The knowledge service turns the documents a user gives it into passages a model can answer from. It
has to work with any embedding model the user binds (ADR 0008), on any deployment (ADR 0009), and
with documents nobody vetted: a book, a scan, a page saved from the web, a file somebody else made.
The prototype it replaces cut text by characters, lost words when it cleaned it, returned only the
piece that matched, and trusted whatever the parsers did.

## Decision

**Two cuts of every document.** The *children* are what is embedded and matched (about 300 tokens);
each belongs to a *parent*, the section around it, which is what comes back as context. Neither
crosses a heading. A child's breadcrumb (`Networking > Routers`) is put in front of its text when it
is embedded and searched, but it is not part of the text: it is where the piece is. A section is
returned once, however many of its pieces matched.

**Search is by meaning and by words, fused by rank.** A vector finds a paragraph that says it in
other words (or in another language, with a multilingual model); words find the part number and the
error code a vector blurs. The two scores are not comparable, so reciprocal rank fusion merges the
rankings. The words count as much as the meaning for a name, a code or a couple of words as they are
written, and for half for a question: measured on a real book, at a full weight the words (mostly
common ones, or accidental ones across languages) displaced good passages the meaning had found.
Each search looks at eight pieces for every passage asked for, at least forty, because the pieces
of one section are one passage. Words are read in the language of each document (stemming, stop
words, accents ignored, chosen once when the document is read) and OR-ed, so a question is not a
list of keywords that must all be there. The language of the *query* is never needed.

**A search degrades and says so.** A collection whose vectors are of another model than the one its
owner has now, one being indexed again, an owner with no embedding model, a gateway that does not
answer: the collection is still searched by words, and a warning names the collection and the
reason. Nobody is left without an answer, and nobody is misled into thinking they had the best one.

**A collection records the embedding space, never the model.** Every embedding comes back with the
opaque id of its space and its size. A collection keeps those, so a change of model behind its
owner's role is *noticed* (the ids differ) and the collection re-indexed in the background, instead
of two spaces being compared silently. Vectors have no size in the column; the index that makes a
large collection fast is made per collection, for its size, and only once the collection has grown
past a threshold. Below it the search is exact, which is fast and never misses; building an index
scans the table and must not stall everybody else's writes for a small one.

**Reading is the riskiest code, so it runs where it can be killed.** Every file is read by a fresh
process with a time limit, a memory limit where the system allows one, and none of the service's
secrets; its answer is validated like any other input. Formats are told by their content, never by
their name, and limits on size, pages, text and archive expansion apply before and after.

**A document that failed is failed, and contributes nothing.** What cannot be read, or whose pieces
the model will not take even cut small, fails with a reason the uploader can act on and is not
retried. What is temporary (a gateway down, a rate limit) is retried later and later. A failed
document's pieces are dropped so it never shows up in a search.

**Passages are somebody else's words.** Each is marked `untrusted`: whatever it says is data, never
an instruction, and a turn that contains one has untrusted content in it (`authorize()`).

**Who may change what follows ownership, and administration is not done through a device.** A
collection of a user is seen and changed by that user; one of the tenant is seen by everybody in it
and changed by its administrators. A shared device (which acts as the tenant) can search and never
change. A collection somebody may not see is not found, which tells them nothing about what exists.

**Tenants are separated by column now and by policy later.** Every table carries its tenant, every
query filters by it, and every transaction says the tenant to the database. Row-level security is a
later migration on those columns, for a deployment shared by tenants (slice 10).

## Consequences

- The service needs an embedding model only to index and to search by meaning; without one, documents
  fail with a way out and searches by words still work.
- Cross-language search (a question in Italian, a passage in English) is a property of the
  *embedding model*, not of this service: an English-centric model finds it poorly, a multilingual one
  well. `docs/knowledge.md` records what was measured.
- A retrieved passage is, for a small collection, always the nearest by meaning even if it is not
  near: callers tell a strong answer from a weak one by `matched_by` and `score`.
- Until the hub issues service credentials (slice 7) the service acts for a collection's owner
  through the gateway's development authentication, and refuses to start in production, like the
  gateway does.
- Query-side prefixes some embedding models want (`search_query:`) are not modelled yet; see the open
  questions of the design.
