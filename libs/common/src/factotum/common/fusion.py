"""Merging ranked lists: what a search by meaning and a search by words each found.

The scores of the two are not comparable: a distance between vectors and a count of matching words
are different things, and tuning a weight between them for every collection is a chore nobody
does well. What is comparable is the *rank*: something both put near the top is probably right, and
something only one of them found still gets its chance. Reciprocal rank fusion adds
``1 / (k + rank)`` for every list an item is in; ``k`` softens the lead of the very first places (60
is the value the method was published with, and it works).
"""

from collections.abc import Hashable, Sequence

DEFAULT_K = 60


def reciprocal_rank_fusion[K: Hashable](
    rankings: Sequence[Sequence[K]],
    *,
    k: int = DEFAULT_K,
    weights: Sequence[float] | None = None,
) -> list[tuple[K, float]]:
    """The items of all the rankings, best first, with their fused score.

    Each ranking is best first. An item repeated inside one ranking counts once, at its best place.
    Ties are broken by the best place the item took anywhere, then by which list came first, so the
    same lists always give the same order.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    if weights is not None and len(weights) != len(rankings):
        raise ValueError("there must be one weight for each ranking")

    scores: dict[K, float] = {}
    best_place: dict[K, int] = {}
    first_seen: dict[K, int] = {}
    for index, ranking in enumerate(rankings):
        weight = 1.0 if weights is None else weights[index]
        seen: set[K] = set()
        place = 0
        for item in ranking:
            if item in seen:
                continue
            seen.add(item)
            place += 1
            scores[item] = scores.get(item, 0.0) + weight / (k + place)
            best_place[item] = min(best_place.get(item, place), place)
            first_seen.setdefault(item, len(first_seen))
    order = sorted(scores, key=lambda item: (-scores[item], best_place[item], first_seen[item]))
    return [(item, scores[item]) for item in order]
