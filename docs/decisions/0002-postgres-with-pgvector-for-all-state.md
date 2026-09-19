# 0002. Postgres with pgvector for all state

Status: accepted, 2026-09-19

## Context

The prototype kept vectors in Chroma next to a Postgres and a Redis that nothing used. Two things
went wrong silently. The volume was mounted where the image no longer persisted data, so vectors
lived only in the container. And the embedding function stored with a collection could not be rebuilt,
so queries were embedded with a different model than the one that built the index: both had 384
dimensions, so nothing failed and the results were just worse.

## Decision

Postgres with the pgvector extension holds metadata, chunks, vectors, full-text search and jobs, with
one schema per service. Vectors sit behind a `VectorStore` interface.

Checked on the compose image (Postgres 18.6, pgvector 0.8.6):

- A `vector` column without a fixed dimension works with a partial HNSW expression index per
  collection: `CREATE INDEX ... USING hnsw ((embedding::vector(3)) vector_cosine_ops) WHERE
  (collection_id = 1)`.
- Beyond 2000 dimensions, which `vector` cannot index, an `halfvec` expression index works (up to
  4000): `((embedding::halfvec(3072)) halfvec_cosine_ops)`.

## Consequences

- One datastore to run and back up, transactional deletes by document, per-tenant isolation in SQL
  with row-level security, and hybrid retrieval (vector plus full-text) in a single query.
- The embedding model of a collection is recorded as data (an embedding space) and reused at query
  time, not inferred from an object persisted by a library.
- Per-collection indexes mean dynamic DDL. With many tenants pgvector's README recommends list
  partitioning by tenant and iterative index scans (0.8.0 and later); the `VectorStore` interface
  keeps that change local.
- Redis is not used until something needs it.
