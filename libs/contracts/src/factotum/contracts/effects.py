"""How much an action changes the world, from harmless to impossible to undo."""

from enum import StrEnum
from typing import Self

_RANK = {"read_only": 0, "reversible": 1, "irreversible": 2}


class Effect(StrEnum):
    """The severity of what an action can do.

    Ordered by ``rank``; compare with :meth:`exceeds` rather than with ``<`` (a ``StrEnum`` would
    compare alphabetically).
    """

    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"

    @property
    def rank(self) -> int:
        return _RANK[self.value]

    def exceeds(self, other: Self) -> bool:
        """True when this effect is strictly more severe than ``other``."""
        return self.rank > other.rank

    @classmethod
    def highest(cls, first: Self, *rest: Self) -> Self:
        """The most severe of the given effects."""
        return max((first, *rest), key=lambda effect: effect.rank)
