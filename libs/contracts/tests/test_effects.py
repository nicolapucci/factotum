import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.contracts.effects import Effect

ORDERED = [Effect.READ_ONLY, Effect.REVERSIBLE, Effect.IRREVERSIBLE]


def test_rank_orders_effects_from_harmless_to_irreversible() -> None:
    assert [effect.rank for effect in ORDERED] == [0, 1, 2]


@pytest.mark.parametrize(
    ("effect", "other", "expected"),
    [
        (Effect.IRREVERSIBLE, Effect.REVERSIBLE, True),
        (Effect.IRREVERSIBLE, Effect.READ_ONLY, True),
        (Effect.REVERSIBLE, Effect.READ_ONLY, True),
        (Effect.READ_ONLY, Effect.REVERSIBLE, False),
        (Effect.REVERSIBLE, Effect.IRREVERSIBLE, False),
    ],
)
def test_exceeds_compares_severity_not_alphabet(
    effect: Effect, other: Effect, expected: bool
) -> None:
    assert effect.exceeds(other) is expected


@pytest.mark.parametrize("effect", list(Effect))
def test_an_effect_does_not_exceed_itself(effect: Effect) -> None:
    assert not effect.exceeds(effect)


def test_highest_picks_the_most_severe() -> None:
    assert Effect.highest(Effect.READ_ONLY) is Effect.READ_ONLY
    assert Effect.highest(Effect.READ_ONLY, Effect.IRREVERSIBLE, Effect.REVERSIBLE) is (
        Effect.IRREVERSIBLE
    )


@given(st.lists(st.sampled_from(list(Effect)), min_size=1))
def test_highest_is_never_exceeded_by_any_input(effects: list[Effect]) -> None:
    top = Effect.highest(effects[0], *effects[1:])

    assert not any(effect.exceeds(top) for effect in effects)
    assert top in effects


def test_effects_serialize_as_their_value() -> None:
    assert str(Effect.REVERSIBLE) == "reversible"
    assert Effect("irreversible") is Effect.IRREVERSIBLE
