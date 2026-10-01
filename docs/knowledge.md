# The knowledge service

Documents in, passages out. It reads the files a user gives it, cuts them into pieces, embeds the
pieces through the model gateway, keeps them in Postgres with pgvector, and answers a question with
the passages that answer it. It never knows which model makes the embeddings, or which provider, or
which key: it asks the gateway for the embeddings of a collection's *owner* (ADR 0008).

*Implemented*: plain text, Markdown, HTML, PDF and Word documents; collections owned by a user or by
the tenant; search by meaning and by words at once; re-indexing when the embedding model changes;
an HTTP API and a container image. *Not yet*: OCR for scanned pages, web pages fetched by address,
other formats.

The reasons behind the design are in [ADR 0011](decisions/0011-knowledge-pieces-to-search-and-sections-to-read.md).

## From a file to a passage

```mermaid
flowchart LR
    up[upload] --> keep[the file is kept<br/>status: pending]
    keep --> read[read in an isolated process]
    read --> lang[language detected]
    lang --> cut[cut into sections and pieces]
    cut --> emb[pieces embedded<br/>through the gateway]
    emb --> ready[status: ready]
```

**Uploading answers at once** (`202`) with the document waiting to be read. The workers do the rest,
and `status` and `progress` on the document say how far they are (`pending`, `processing`, `ready`,
`failed`; progress is the share of its pieces that have a vector).

**What a file is comes from what is in it**, never from its name or the type the client declared:
`%PDF-` is a PDF whatever it is called, a zip with `word/document.xml` is a Word document, and a
text that starts like a page is HTML. Only Markdown and plain text, which look alike, are told by
name. Anything else is refused before it is kept (`415`).

**Reading runs where it can be killed.** Each document is read by a fresh Python process with a time
limit (5 minutes), a memory limit where the system has one, an environment with none of the
service's secrets, and limits on the size of the file (25 MB by default), the pages (2000), the text
(5 million characters) and, for Word files, what the archive expands to. A hostile file can hang or
crash that process and the service does not notice. What the process answers is validated like any
other input, and if it failed, only the name of the exception is logged, never text a traceback
may quote.

**What is taken from each format**

| Format | Structure it keeps |
| --- | --- |
| Plain text | paragraphs; an indented paragraph is code |
| Markdown | headings, paragraphs, lists, code blocks, tables; front matter is skipped |
| HTML | the main content (`main`, `article`, else the body), headings, lists, code, tables; scripts, styles, menus and forms are not text |
| PDF | the text of each page, cleaned of running headers and footers, page numbers, dot-leader contents pages and hyphenation at line ends; the outline (bookmarks) gives each page its chapter and section; pages are known, for citations. A scan with no text layer is refused with a message that says it needs OCR |
| Word | paragraphs, headings by style, lists, tables |

**The language** of a document is detected once, from a sample of its prose spread over the whole
document (code and tables do not count), unless the uploader said it. It only chooses how the words
are searched (below): the search never needs the language of the *question*.

**Two cuts.** The document is cut into *sections* under its headings, and each section into
*pieces* of at most about 300 tokens. A piece never crosses a heading. A block longer than a piece
is cut at the end of a sentence, else between words, else anywhere; code at the end of a line with
its indentation kept; a table between rows, repeating its header row in every piece. What is
embedded is the piece with its breadcrumb (`Networking > Routers`) in front, so that "reset" under
*Routers* is not "reset" under *Passwords*. Sizes are estimated (3 characters to a token, a high
guess on purpose) against what the embedding model will take.

**A model that says the pieces are too long** (`context_too_long`) is answered by cutting them in
half again, down to a floor, before giving up.

**When it goes wrong**

| What happened | What becomes of the document |
| --- | --- |
| The file cannot be read, is too big, has no text | `failed`, with a reason ("the PDF has no text in it: it may be scanned pages, which need OCR"); not retried |
| The owner has no embedding model | `failed`, `embedding_not_configured`, with the way out; `POST /v1/documents/{id}/retry` after setting one up |
| The pieces do not fit the model even cut small | `failed`, `context_too_long` |
| The gateway is down, a model is busy, a rate limit | tried again after 30 s, 60 s, 120 s... (a rate limit's own `retry-after` is obeyed); after the fifth attempt, `failed`, `embedding_unavailable` |
| Something nobody expected | the same, with a message that says nothing about the file |
| The worker dies | its job's lease runs out and another worker takes it up: pieces already embedded are not embedded again and the file is not read again |

A document that failed has its pieces dropped: it never shows up in a search.

## Searching

`POST /v1/search` with a question, optionally the collections to look in (by default every one the
caller may see) and how many passages (default 8).

1. The question is embedded **once for each owner** whose collections are searched, and as that
   owner (a user's collections use the user's embedding model, the tenant's use the tenant's).
2. For each collection of a space the question can be compared with, the nearest pieces by meaning
   are found. The best of all the collections of one embedding space come first.
3. The pieces that have some of the *words* of the question are found, in each collection, read in
   the language of each document: stemmed, without stop words, accents and capitals ignored ("citta"
   finds "città"), the words OR-ed and ranked by how many a piece has. The headings of a piece are
   searched too, and weigh less than its text.
4. The rankings are merged by **reciprocal rank fusion**: a piece both put high is very likely what
   was asked for, and a piece only one found still gets its place. Scores of a vector distance and of
   word matching are not comparable; ranks are. **The words count as much as the meaning for a name,
   a code or a couple of words as they are written** (`ssh-keygen`, `E-4021`, `chmod 755`: what a
   vector blurs and the words find), **and for half for a question**: the words a question shares
   with a passage are mostly common ones (and, when the passage is in another language, only
   accidental ones), so they may lift what the meaning also found but cannot bury it under what
   only they found. Half is a strong lever, because the fusion flattens: a piece that only a list
   counting half found lands below the sixty-first of the other, while one that both found is lifted
   past the meaning's first. This is why a name must count fully, or a piece the vector never found
   would never be seen (see *Measured*). Each search looks at 8 pieces for every passage asked for
   (at least 40, at most 200): a section holds several pieces, so a ranking of twenty holds far
   fewer sections.
5. A section comes back once, however many of its pieces matched, as the best piece with the whole
   section as `context`. The passage has `matched_by` (`vector`, `text`, or both), a relative `score`
   in (0, 1], its document, source, page and headings for a citation, and its language.

**Every passage is somebody else's words**: `untrusted` is always `true`. Whatever a passage says is
data, never an instruction to the caller, and a turn that contains one has untrusted content in it
(`authorize()` asks for a confirmation before an action with an effect in such a turn).

**The search degrades instead of failing.** The answer always has passages when the words find any;
`warnings` say, per collection, what could not be used:

| Warning | When | What it means |
| --- | --- | --- |
| `needs_reindex` | the embedding model behind the owner is no longer the one that made the vectors, or the collection is being indexed again | searched by words only, until it is indexed again (`POST /v1/collections/{id}/reindex`) |
| `embedding_not_configured` | the owner has no embedding model | searched by words only |
| `vector_unavailable` | the gateway did not answer | searched by words only, this time |

A model change is *noticed* by the space id every embedding comes back with: it is recorded by the
collection, and if the question's differs, the collection is marked and searched by words from then
on, without asking the model again.

**Across languages** is a property of the embedding model, not of this service. Meaning is what
carries a question in one language to a passage in another, and only a multilingual model puts both
in the same space. Detecting the language and choosing a model per language would put the vectors in
different spaces, which cannot be compared. With an English-centric model such as `nomic-embed-text`
a question finds passages in its own language well and in another poorly; with a multilingual one
such as `bge-m3` it finds them across languages almost as well (82% of Italian questions over an
English book against 91% of English ones, see *Measured*). Bind a multilingual model to `embedding`
and re-index.

**A passage is the nearest, not always near.** By meaning the nearest pieces always come back, even
when none is near. `matched_by` and `score` are how a caller tells a strong answer (found by both
searches) from a weak one (by meaning alone).

## Collections, owners and who may do what

A collection belongs to a user or to the tenant and lives in **one embedding space**: it records the
opaque id of the space and the size of its vectors (never a model). Creating one asks the owner's
embedding model for a probe to learn both, so an owner without an embedding model cannot have a
collection (`409 embedding_not_configured`).

| | a user's collection | the tenant's collection |
| --- | --- | --- |
| **see and search** | that user | everybody in the tenant, and shared devices |
| **create, add and delete documents, re-index, rename, delete** | that user | the tenant's administrators |
| a shared device (acts as the tenant) | nothing | search only |

Administration is done through a person's own session, never through a device (ADR 0004). A
collection somebody may not see is **not found** (`404`), never "forbidden": it tells them nothing
about what exists. One they may see and may not change is `403`. The same collection name is
allowed for different owners, not twice for one.

The same file is kept once per collection (its SHA-256), and the original is kept in the database
(up to the upload limit), so a document can be read again without asking for it again.

## Re-indexing

When the embedding model behind an owner's role changes, every vector of the old model is in the
wrong space. `POST /v1/collections/{id}/reindex` queues a job that asks the owner's model where its
vectors live now, moves the collection there, forgets the old vectors and embeds every piece again,
resumably. Meanwhile the collection is searched by words. If the model changes once more while it
runs, or it is given up after it began, the collection stays marked; it is never made active with
half its vectors.

## The HTTP API

Everything is under `/v1` and needs an authenticated caller. Interactive documentation is at `/docs`
outside production.

| | |
| --- | --- |
| `POST /v1/collections` | create: `{"name", "description", "owner": "user" or "tenant"}` |
| `GET /v1/collections` | the collections the caller may see |
| `GET`, `PATCH`, `DELETE /v1/collections/{id}` | read, rename or describe, delete with everything in it |
| `POST /v1/collections/{id}/reindex` | embed the collection again with the model its owner has now (`202`) |
| `POST /v1/collections/{id}/documents` | upload a file (multipart: `file`, optional `title`, `language`) (`202`) |
| `POST /v1/collections/{id}/documents/text` | add a text: `{"title", "text", "language"}` (`202`) |
| `GET /v1/collections/{id}/documents?limit&offset` | the documents, newest first |
| `GET`, `DELETE /v1/documents/{id}` | one document and how far it is; delete it |
| `POST /v1/documents/{id}/retry` | try again a document that failed (`202`) |
| `POST /v1/search` | `{"query", "collection_ids", "limit"}` → `{"passages", "warnings"}` |
| `GET /healthz`, `GET /readyz` | liveness; readiness (the database answers) |

Errors always have one shape, `{"error": {"code", "message", "details"}}`, with the stable code of the
typed error, and never echo what was sent. `404` a collection or document that does not exist *or
may not be seen*, `403` seen and not allowed, `409` a name or a document already there (or no
embedding model), `413` too big, `415` a format that is not supported, `422` a request that breaks
the contract, `503` the gateway is not answering.

```bash
# The development user, with the gateway running (see docs/model-gateway.md):
curl -s localhost:8082/v1/collections -H 'content-type: application/json' -d '{"name": "Manuals"}'
curl -s localhost:8082/v1/collections/$ID/documents -F file=@router.pdf -F language=en
curl -s localhost:8082/v1/documents/$DOC          # status, progress
curl -s localhost:8082/v1/search -H 'content-type: application/json' \
     -d '{"query": "how do I restore the router to factory settings?"}'
```

## Configuration

Read from the environment (`FACTOTUM_*`) and, in development, from `.env`; every variable is in
[`.env.example`](../.env.example) and a test says so. The database is the one of `docker compose`
(`FACTOTUM_POSTGRES_*`).

| Variable | Default | |
| --- | --- | --- |
| `FACTOTUM_KNOWLEDGE_HOST`, `..._PORT` | `127.0.0.1`, `8082` | loopback unless told otherwise |
| `FACTOTUM_KNOWLEDGE_GATEWAY_URL` | `http://127.0.0.1:8081` | where the model gateway is |
| `FACTOTUM_KNOWLEDGE_MAX_UPLOAD_MB` | `25` | the largest file (1 to 100) |
| `FACTOTUM_KNOWLEDGE_WORKERS` | `2` | documents read at once in this process; `0` = only answer requests |
| `FACTOTUM_KNOWLEDGE_CHILD_TOKENS`, `..._PARENT_TOKENS` | `320`, `1200` | size of a piece and of its section; smaller for a model with a small context |
| `FACTOTUM_KNOWLEDGE_EMBED_BATCH` | `32` | pieces embedded in one request |
| `FACTOTUM_KNOWLEDGE_INDEX_THRESHOLD` | `2000` | pieces with a vector from which a collection gets an index |

## Storage

Postgres 18 with pgvector, schema `knowledge`, migrations next to the code (`0001_initial.sql`,
applied when the service starts, each once, never edited afterwards).

* `collections`, `documents` (and `document_data`: the file, apart from the row so a list never drags
  it along), `parents` (sections), `chunks` (pieces), `jobs`. **Every table carries its tenant**, every
  query filters by it, and every transaction sets `app.tenant_id`; a test checks that all of them
  have the column. Row-level security is a later migration on those columns (slice 10).
* `chunks.embedding` has no size in the column: each collection has the size of its model. A
  collection with more than the threshold of pieces gets a partial HNSW index for its size (`vector`
  up to 2000 dimensions, `halfvec` up to 4000, none above), built without blocking anyone, by one
  builder at a time and by one process (a parallel build keeps its graph in shared memory, more than
  Docker's default 64 MB: it failed that way on a real book; compose also gives Postgres 256 MB).
  Below the threshold, and above 4000 dimensions, the search is exact. Queries are written the way the
  index is, with the collection as a literal: a test asks the planner for its plan and finds the
  index in it. Building the index is an optimization and never a reason to fail a document: if it
  cannot be built, the document is ready and searched exactly, and the next document or re-index tries
  again. Whether Postgres then uses the index for a search is its call, by cost, and it can only
  judge that from what it knows of the table: the ingestion puts the pieces in before their vectors,
  so the statistics of a table just filled say they have none, and the planner then sorts a whole
  collection however large. The service therefore analyzes the three columns that matter
  (`collection_id`, `tenant_id`, `embedding`, a matter of milliseconds) as soon as it has made an
  index. A test checks the statistics; on 12,000 pieces of 1024 dimensions the planner uses the index
  with them and reads every piece without them (found by a real book, whose 2956 pieces it reads in
  full even with them, being cheaper at that size).
* The text search configurations are shipped by the migration, one per language Postgres knows, with
  accents ignored where the language has them, and a `simple` one for a document in no language the
  service knows.
* Jobs are claimed with `FOR UPDATE SKIP LOCKED` and a lease that a worker renews after each batch; a
  worker that lost its claim cannot finish, retry or fail the job somebody else now has.

## Trying it

```bash
FACTOTUM_POSTGRES_PASSWORD=dev docker compose up -d --wait                       # the database
FACTOTUM_POSTGRES_PASSWORD=dev docker compose --profile services up -d --build   # gateway + knowledge
```

The gateway needs an `embedding` binding (see [`model-gateway.md`](model-gateway.md#configuring-bindings)),
for instance Ollama with `nomic-embed-text`.

**Tests.** The unit tests need nothing: the store in memory and an embedder in which a word makes a
direction stand in for the database and the model, and the same tests that keep the store's promises
run against both stores. `uv run pytest -m integration` runs them against a real Postgres too, and the
service against a real database and a gateway that answers. The live tests use everything for real:

```bash
FACTOTUM_POSTGRES_PASSWORD=dev FACTOTUM_LIVE_TESTS=1 \
    uv run pytest -m live services/knowledge/tests/live -s
# and to read a whole book of your own (a PDF; it is never copied anywhere):
FACTOTUM_LIVE_BOOK=/path/to/book.pdf  ...same command...
```

## Measured

From the live tests (`services/knowledge/tests/live`, the command is under *Trying it*): the real
gateway code, Ollama, Postgres 18 with pgvector 0.8.6 in Docker, on one developer's PC (Ryzen 5 5600G,
RTX 3060 Ti, Windows 11), with two embedding models: `nomic-embed-text` (768 dimensions, 274 MB, made
for English) and `bge-m3` (1024 dimensions, 1.16 GB, multilingual). The book is an English PDF of 931
pages about Linux that its owner keeps out of the repository: the test reads whatever
`FACTOTUM_LIVE_BOOK` points to and skips itself when it is not set. Which model to use is
`FACTOTUM_LIVE_OLLAMA_EMBED_MODEL`.

**Reading the book**: 2956 pieces in 784 sections, from upload to `ready` in 80 to 120 seconds with
`nomic-embed-text` and 100 to 125 with `bge-m3` (two workers), the vector index made just after. The
test asks the planner for its plan with the index asked for and finds it in it (the query is written
so that the index can answer); left to itself, the planner reads every piece at this size, which is
cheaper.

**Finding**, over that book: 44 questions in English, the same kind in Italian (34), and 22 names as
they are written (`ssh-keygen`, `lvcreate`). A section *answers* when it, whole (what a caller gets
as `context`), holds the command that does what was asked. *hit@5* is the share of questions with an
answer among the first five sections; *precision@5* the share of those five that answer.

| Asked | Model | by meaning | by words | both (the service) |
| --- | --- | --- | --- | --- |
| in English (44) | `nomic-embed-text` | 84% / 55% | 84% / 48% | **93% / 55%** |
| | `bge-m3` | 91% / 54% | 84% / 48% | **93% / 56%** |
| in Italian (34) | `nomic-embed-text` | 47% / 22% | 32% / 14% | **47% / 23%** |
| | `bge-m3` | 82% / 49% | 32% / 14% | **82% / 46%** |
| by name (22) | `nomic-embed-text` | 86% / 59% | **91% / 83%** | 86% / 78% |
| | `bge-m3` | 86% / 65% | **91% / 83%** | 86% / 77% |

(hit@5 / precision@5; the search by words does not depend on the model.) What it says:

* In the language of the book, the two together find an answer for as many questions as the better
  of the two, or more (93% with both models).
* By name, the words are what finds: the meaning gets the right section into the first five almost
  as often (86% and 91%), and then fills the rest with sections that are only alike (precision 59% to
  65%, against 83% by words and 77% to 78% by both).
* **Across languages is the model's doing.** Questions in Italian over the English book find an
  answer 47% of the time with `nomic-embed-text` and 82% with `bge-m3` (91% for questions in English),
  and the words alone, which share only accidental words with the passage, 32%. Nothing in the
  service changes: bind the multilingual model to `embedding` and re-index. The small library of the
  other live test (four documents in English, the same four in Italian) says the same: with
  `bge-m3` the right document is among the first two for all sixteen questions, across languages too
  (the other of the two is the same subject in the other language), with `nomic-embed-text` it
  went down to seventh place across languages.
* With a model that already understands the question, the words add little: 82% with both and with
  the meaning alone in Italian, a precision a little lower (46% against 49%).

**How much the words count, and how much is looked at** were tuned on these questions with
`nomic-embed-text` (with a script that is not in the repository). At a full weight for every question
the words displaced what the meaning had found (in Italian 38% instead of 47%); at half for every
question a name found only by the words would land below the sixty-first passage, hence full weight
for names and codes and half for questions. Looking at twenty pieces for each search instead of
forty cost English questions nine points (84% instead of 93%): a section holds several pieces, and
the fusion needs sections. The table above is with those weights for both models.

**Speed**: a search, the embedding of the question included, over that book: median 87 to 95 ms, 90th
percentile 106 to 130 ms, slowest 136 to 459 ms (100 searches in each of three runs, index in use). On
a small collection the parts were 43 ms for the embedding of the question through the gateway, 7 ms
for the search by meaning and 7 ms for the search by words.

**What a real run found that the unit tests could not**, all fixed: every call to a model made a new
HTTP client, and creating its TLS settings takes about 300 ms on Windows, even for a model on the
same machine over plain HTTP (a search went from 341 ms to 60 ms once the settings were shared, on a
small collection), and the gateway now keeps the connections it makes between calls
([`model-gateway.md`](model-gateway.md)); building the vector index with parallel workers ran out of
Docker's 64 MB of shared memory and failed the document (now one process builds it, Postgres gets
256 MB, and an index that cannot be built never fails a document); the planner, told by the
statistics of a table just filled that no piece had a vector, sorted whole collections instead of
using the index (now analyzed after the index is made); and the two points above about the words.

## Known limits

* Scanned pages need OCR, which is not done. The message says so.
* Once `FACTOTUM_HUB_TOKEN_SECRET` is set, the service acts for a collection's owner through a real
  token the gateway checks the same way as everything else (`HubDelegation`,
  [`hub.md`](hub.md#known-limits)); without it, this is still the gateway's development
  authentication (a header), and the service, like the gateway, refuses to start outside development
  and test.
* Query-side prefixes some embedding models want (`search_query:`) are not modelled: the gateway does
  not offer them yet (open question in the design).
* The language of a document is one for the whole document; a bilingual book is read in its dominant
  language (its words are still found by meaning).
* Token counts are estimates. The chunk sizes leave room for that on purpose.
* Collections are searched together by default and their pieces compete on equal terms: a
  collection of short, generic notes can push down the passages of a book (the live tests found this
  out by searching two at once). Name the collections to look in when it matters.
