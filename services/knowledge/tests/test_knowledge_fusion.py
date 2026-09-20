import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.knowledge.fusion import DEFAULT_K, reciprocal_rank_fusion


def order(rankings: list[list[str]], **kwargs: object) -> list[str]:
    return [item for item, _ in reciprocal_rank_fusion(rankings, **kwargs)]  # type: ignore[arg-type]


def test_one_list_keeps_its_order() -> None:
    assert order([["a", "b", "c"]]) == ["a", "b", "c"]


def test_something_both_lists_found_beats_something_only_one_did() -> None:
    assert order([["a", "b", "c"], ["c", "d"]])[0] == "c"


def test_the_score_of_an_item_is_the_sum_of_one_over_k_plus_its_place() -> None:
    fused = dict(reciprocal_rank_fusion([["a", "b"], ["b", "a"]]))

    assert fused["a"] == pytest.approx(1 / (DEFAULT_K + 1) + 1 / (DEFAULT_K + 2))
    assert fused["a"] == pytest.approx(fused["b"])


def test_the_first_place_of_a_short_list_and_of_a_long_one_are_worth_the_same() -> None:
    fused = dict(reciprocal_rank_fusion([["x"], ["y", "a", "b", "c"]]))

    assert fused["x"] == fused["y"]


def test_an_item_that_only_one_list_found_still_appears() -> None:
    assert set(order([["a"], ["b"]])) == {"a", "b"}


def test_a_tie_goes_to_the_list_that_came_first() -> None:
    assert order([["a"], ["b"]]) == ["a", "b"]
    assert order([["b"], ["a"]]) == ["b", "a"]


def test_items_that_took_the_same_places_keep_the_order_they_were_first_seen() -> None:
    assert order([["a", "b"], ["b", "a"]]) == ["a", "b"]
    assert order([["b", "a"], ["a", "b"]]) == ["b", "a"]


def test_the_same_lists_always_give_the_same_order() -> None:
    lists = [["a", "b", "c", "d"], ["d", "c", "b", "a"], ["b", "d"]]

    assert order(lists) == order(lists)


def test_an_item_repeated_in_one_list_counts_once_at_its_best_place() -> None:
    once = dict(reciprocal_rank_fusion([["a", "b"]]))
    repeated = dict(reciprocal_rank_fusion([["a", "a", "a", "b"]]))

    assert repeated == once


def test_a_weight_makes_a_list_count_more() -> None:
    lists = [["a"], ["b"]]

    assert order(lists, weights=[3.0, 1.0]) == ["a", "b"]
    assert order(lists, weights=[1.0, 3.0]) == ["b", "a"]


def test_nothing_fused_is_nothing() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_k_must_be_positive() -> None:
    with pytest.raises(ValueError, match="k must be"):
        reciprocal_rank_fusion([["a"]], k=0)


def test_there_must_be_a_weight_for_each_list() -> None:
    with pytest.raises(ValueError, match="one weight"):
        reciprocal_rank_fusion([["a"], ["b"]], weights=[1.0])


def test_a_larger_k_flattens_the_lead_of_the_first_places() -> None:
    sharp = dict(reciprocal_rank_fusion([["a", "b"]], k=1))
    flat = dict(reciprocal_rank_fusion([["a", "b"]], k=1000))

    assert sharp["a"] / sharp["b"] > flat["a"] / flat["b"] > 1


rankings = st.lists(st.lists(st.sampled_from("abcdefgh"), max_size=8), max_size=4)


@given(rankings)
def test_every_item_of_every_list_is_in_the_result_exactly_once(lists: list[list[str]]) -> None:
    result = order(lists)

    assert sorted(result) == sorted({item for ranking in lists for item in ranking})


@given(rankings)
def test_the_result_is_best_first(lists: list[list[str]]) -> None:
    scores = [score for _, score in reciprocal_rank_fusion(lists)]

    assert scores == sorted(scores, reverse=True)


@given(st.lists(st.sampled_from("abcdefgh"), min_size=1, max_size=8, unique=True))
def test_one_list_without_repeats_comes_out_as_it_went_in(ranking: list[str]) -> None:
    assert order([ranking]) == ranking
