"""The SQL of the Postgres store, as text: no database needed to see that the index and the search
say the same thing, which is what makes the planner use the index."""

import math
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.contracts.ids import CollectionId
from factotum.knowledge.store.postgres import (
    VectorForm,
    vector_index_name,
    vector_index_sql,
    vector_search_sql,
    vector_text,
)


class TestVectorForm:
    def test_up_to_two_thousand_numbers_are_vectors(self) -> None:
        assert VectorForm(3).type_name == "vector"
        assert VectorForm(2000).type_name == "vector"

    def test_beyond_that_they_are_half_precision_vectors(self) -> None:
        assert VectorForm(2001).type_name == "halfvec"

    def test_only_up_to_four_thousand_numbers_can_be_indexed(self) -> None:
        assert VectorForm(2000).indexable
        assert VectorForm(4000).indexable
        assert not VectorForm(4001).indexable

    def test_the_column_and_the_parameter_both_say_the_size(self) -> None:
        assert VectorForm(1024).column() == "embedding::vector(1024)"
        assert VectorForm(1024).column("c") == "c.embedding::vector(1024)"
        assert VectorForm(3072).parameter(2) == "$2::text::halfvec(3072)"

    def test_the_operator_class_is_that_of_the_type(self) -> None:
        assert VectorForm(8).operator_class == "vector_cosine_ops"
        assert VectorForm(3072).operator_class == "halfvec_cosine_ops"


class TestSql:
    collection = CollectionId(uuid4())

    def test_an_index_is_for_one_collection_and_its_size(self) -> None:
        sql = vector_index_sql(self.collection, 1024)

        assert "CREATE INDEX CONCURRENTLY" in sql
        assert vector_index_name(self.collection) in sql
        assert "USING hnsw ((embedding::vector(1024)) vector_cosine_ops)" in sql
        assert f"WHERE collection_id = '{self.collection}'" in sql

    def test_the_search_says_it_the_way_the_index_does(self) -> None:
        sql = vector_search_sql(self.collection, 1024)

        assert f"c.collection_id = '{self.collection}'" in sql
        assert "c.embedding::vector(1024) <=> $2::text::vector(1024)" in sql
        assert "ORDER BY c.embedding::vector(1024)" in sql

    def test_the_name_of_an_index_is_short_enough_for_postgres(self) -> None:
        assert len(vector_index_name(self.collection)) <= 63


class TestVectorText:
    def test_it_is_written_as_pgvector_reads_it(self) -> None:
        assert vector_text((1.0, 0.5, -2.0)) == "[1.0,0.5,-2.0]"

    def test_integers_are_numbers_too(self) -> None:
        assert vector_text((1, 2, 3)) == "[1.0,2.0,3.0]"

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_a_number_that_is_not_finite_is_not_a_vector(self, bad: float) -> None:
        with pytest.raises(ValueError, match="finite"):
            vector_text((1.0, bad))

    @given(
        st.lists(
            st.floats(allow_nan=False, allow_infinity=False, width=32), min_size=1, max_size=20
        )
    )
    def test_what_is_written_can_be_read_back(self, numbers: list[float]) -> None:
        text = vector_text(numbers)

        assert [float(part) for part in text[1:-1].split(",")] == numbers
