"""Searching the collections a person may see: by meaning and by words, and the best of both.

The two searches are good at different things. Meaning finds a paragraph that says it in other
words, or in another language; words find the exact name, the error code, the part number that a
vector blurs. Each gives a ranking, and reciprocal rank fusion merges them (``fusion``): what both
put high is very likely what was asked for, and what only one found still gets its place.

Everything degrades instead of failing. A collection whose vectors are of another model than the one
the owner has now, one that is being indexed again, an owner without an embedding model, a gateway
that does not answer: the collection is still searched by words, and a warning says why it was not
searched by meaning as well. The caller (a model, a person) is told, and gets an answer.

What comes back is *somebody else's words*: every passage is marked untrusted, and whatever it says
is data, never an instruction.
"""

import asyncio
import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from factotum.contracts.errors import (
    CollectionNotFoundError,
    EmbeddingNotConfiguredError,
    FactotumError,
)
from factotum.contracts.identity import Owner, Principal
from factotum.contracts.ids import ChunkId, CollectionId
from factotum.contracts.knowledge import (
    Collection,
    CollectionStatus,
    Location,
    Passage,
    SearchRequest,
    SearchResult,
    SearchWarning,
    SearchWarningCode,
)
from factotum.knowledge.embedder import Embedder
from factotum.knowledge.fusion import DEFAULT_K, reciprocal_rank_fusion
from factotum.knowledge.policy import can_read
from factotum.knowledge.store import Hit, KnowledgeStore

_PIECES_PER_PASSAGE = 8
_MIN_CANDIDATES = 40
_MAX_CANDIDATES = 200
"""Pieces each search looks at, not passages: the pieces of one section are one passage, so a
ranking of twenty pieces holds far fewer than twenty sections. Measured on a book
(docs/knowledge.md): questions found their answer in the first five passages 84% of the time with
twenty pieces and 93% with forty."""

_CODE_LIKE = re.compile(r"\d|\w[-_./:@#\\]\w")
"""A token with a digit in it, or words joined by punctuation: ``E-4021``, ``file.txt``."""

_NAME_WORDS = 2
_QUESTION_WORDS_WEIGHT = 0.5


def words_weight(query: str) -> float:
    """How much the words count against the meaning, for this query.

    A name, a code, a couple of words, as they are written, are what a vector blurs and the words
    find: they count as much as the meaning. A question about something is what a vector understands
    best; the words it shares with a passage are mostly common ones (and, when the passage is in
    another language, only accidental ones), so the words count for half: they lift what the
    meaning also found, and cannot bury it under what only they found. Measured on a book of 931
    pages, with questions in the language of the book and in another: at a full weight the search by
    both is worse than by meaning alone when the model cannot cross languages.

    Half is a strong lever, and that is why it is only for a question: reciprocal rank fusion
    flattens, so a piece that only a list counting half found lands below the sixty-first of the
    other (while one that both found is lifted past the meaning's first), and a name found only by
    the words would never be seen.
    """
    tokens = query.split()
    if len(tokens) <= _NAME_WORDS or any(_CODE_LIKE.search(token) for token in tokens):
        return 1.0
    return _QUESTION_WORDS_WEIGHT


@dataclass(slots=True)
class _ByMeaning:
    """What searching by meaning found for the collections of one owner."""

    space_id: str | None = None
    hits: list[Hit] = field(default_factory=list)
    warnings: list[SearchWarning] = field(default_factory=list)


def _warn(collection: Collection, code: SearchWarningCode, message: str) -> SearchWarning:
    return SearchWarning(collection_id=collection.id, code=code, message=message)


class Retriever:
    def __init__(self, store: KnowledgeStore, embedder: Embedder) -> None:
        self._store = store
        self._embedder = embedder

    async def search(self, principal: Principal, request: SearchRequest) -> SearchResult:
        collections = await self._visible(principal, request.collection_ids)
        if not collections:
            return SearchResult()
        candidates = min(max(request.limit * _PIECES_PER_PASSAGE, _MIN_CANDIDATES), _MAX_CANDIDATES)

        owners: dict[Owner, list[Collection]] = {}
        for collection in collections:
            owners.setdefault(collection.owner, []).append(collection)

        by_words, by_meaning = await asyncio.gather(
            self._store.search_text(
                principal.tenant_id, [c.id for c in collections], request.query, limit=candidates
            ),
            asyncio.gather(
                *(
                    self._by_meaning(owner, owned, request.query, candidates)
                    for owner, owned in owners.items()
                )
            ),
        )
        warnings = [warning for found in by_meaning for warning in found.warnings]
        return SearchResult(
            passages=self._passages(
                by_words, list(by_meaning), request.limit, words_weight(request.query)
            ),
            warnings=tuple(warnings),
        )

    async def _visible(
        self, principal: Principal, wanted: Sequence[CollectionId] | None
    ) -> list[Collection]:
        everything = await self._store.list_collections(principal.tenant_id, counts=False)
        visible = [c for c in everything if can_read(principal, c)]
        if wanted is None:
            return visible
        by_id = {c.id: c for c in visible}
        if any(identity not in by_id for identity in wanted):
            raise CollectionNotFoundError("a collection does not exist")
        return [by_id[identity] for identity in dict.fromkeys(wanted)]

    async def _by_meaning(
        self, owner: Owner, collections: list[Collection], query: str, candidates: int
    ) -> _ByMeaning:
        found = _ByMeaning()
        searchable: list[Collection] = []
        for collection in collections:
            if collection.status is CollectionStatus.ACTIVE:
                searchable.append(collection)
            else:
                found.warnings.append(
                    _warn(
                        collection,
                        SearchWarningCode.NEEDS_REINDEX,
                        "the collection is being indexed again: until it is done it is searched "
                        "by words only"
                        if collection.status is CollectionStatus.REINDEXING
                        else "the embedding model has changed since the collection was indexed: "
                        "it is searched by words only until it is indexed again",
                    )
                )
        if not searchable:
            return found

        try:
            embedded = await self._embedder.embed(owner, [query])
        except EmbeddingNotConfiguredError:
            found.warnings.extend(
                _warn(
                    c,
                    SearchWarningCode.EMBEDDING_NOT_CONFIGURED,
                    "no embedding model is set up for its owner: it is searched by words only",
                )
                for c in searchable
            )
            return found
        except FactotumError:
            found.warnings.extend(
                _warn(
                    c,
                    SearchWarningCode.VECTOR_UNAVAILABLE,
                    "the embedding model could not be reached: it is searched by words only",
                )
                for c in searchable
            )
            return found

        current: list[Collection] = []
        for collection in searchable:
            if collection.space_id == embedded.space_id:
                current.append(collection)
                continue
            # The model behind the owner is not the one that made these vectors.
            with contextlib.suppress(CollectionNotFoundError):
                await self._store.mark_needs_reindex(collection.tenant_id, collection.id)
            found.warnings.append(
                _warn(
                    collection,
                    SearchWarningCode.NEEDS_REINDEX,
                    "the embedding model has changed since the collection was indexed: "
                    "it is searched by words only until it is indexed again",
                )
            )
        if current:
            lists = await asyncio.gather(
                *(
                    self._store.search_vector(
                        c.tenant_id, c.id, embedded.vectors[0], limit=candidates
                    )
                    for c in current
                )
            )
            # Vectors of one space are comparable: the best of all the collections come first.
            found.space_id = embedded.space_id
            found.hits = sorted((hit for hits in lists for hit in hits), key=lambda h: -h.score)
        return found

    @staticmethod
    def _passages(
        by_words: list[Hit], by_meaning: list[_ByMeaning], limit: int, words: float = 1.0
    ) -> tuple[Passage, ...]:
        # One ranking by words, and one by meaning for each space that was searched.
        spaces: dict[str, list[Hit]] = {}
        for found in by_meaning:
            if found.space_id is not None:
                spaces.setdefault(found.space_id, []).extend(found.hits)
        meaning = [sorted(hits, key=lambda h: -h.score) for hits in spaces.values()]

        rankings = [by_words, *meaning]
        weights = [words, *([1.0] * len(meaning))]
        fused = reciprocal_rank_fusion(
            [[hit.chunk_id for hit in ranking] for ranking in rankings], weights=weights
        )
        hits: dict[ChunkId, Hit] = {hit.chunk_id: hit for ranking in rankings for hit in ranking}
        found_by_words = {hit.chunk_id for hit in by_words}
        found_by_meaning = {hit.chunk_id for ranking in meaning for hit in ranking}
        best = sum(weights) / (DEFAULT_K + 1)

        passages: list[Passage] = []
        parents: set[UUID] = set()
        for identity, score in fused:
            hit = hits[identity]
            if hit.parent_id in parents:
                continue  # its section is already there, in the context of a better piece
            parents.add(hit.parent_id)
            matched: list[Literal["vector", "text"]] = []
            if identity in found_by_meaning:
                matched.append("vector")
            if identity in found_by_words:
                matched.append("text")
            passages.append(
                Passage(
                    chunk_id=hit.chunk_id,
                    collection_id=hit.collection_id,
                    document_id=hit.document_id,
                    document_title=hit.document_title,
                    source=hit.source,
                    text=hit.text,
                    context=hit.context,
                    location=Location(page=hit.page, headings=hit.headings),
                    score=min(1.0, score / best),
                    matched_by=tuple(matched),
                    language=hit.language,
                )
            )
            if len(passages) == limit:
                break
        return tuple(passages)
