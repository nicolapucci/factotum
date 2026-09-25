"""Which host a list of hosts names, the way a grant lists them."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.common.hosts import Match, is_address, match_host, normalize_host


class TestWhichHostsAGrantLists:
    def test_a_host_is_listed_when_it_is_named(self) -> None:
        assert match_host(["api.example.org", "b.org"], "api.example.org") is Match.EXACT

    @pytest.mark.parametrize("asked", ["API.Example.ORG", "api.example.org.", " api.example.org "])
    def test_case_and_the_dot_that_ends_a_name_make_no_difference(self, asked: str) -> None:
        assert match_host(["api.example.org"], asked) is Match.EXACT

    def test_a_host_that_is_not_listed_is_not_matched(self) -> None:
        assert match_host(["api.example.org"], "evil.org") is None
        assert match_host([], "api.example.org") is None

    def test_a_sibling_or_a_parent_of_a_listed_host_is_not_it(self) -> None:
        assert match_host(["api.example.org"], "example.org") is None
        assert match_host(["api.example.org"], "www.example.org") is None
        assert match_host(["api.example.org"], "x.api.example.org") is None
        assert match_host(["api.example.org"], "api.example.org.evil.org") is None

    def test_a_domain_lists_what_is_in_it(self) -> None:
        assert match_host(["*.example.org"], "api.example.org") is Match.WILDCARD
        assert match_host(["*.example.org"], "a.b.example.org") is Match.WILDCARD

    def test_a_domain_does_not_list_itself_or_what_only_ends_like_it(self) -> None:
        assert match_host(["*.example.org"], "example.org") is None
        assert match_host(["*.example.org"], ".example.org") is None
        assert match_host(["*.example.org"], "badexample.org") is None
        assert match_host(["*.example.org"], "example.org.evil.org") is None

    def test_a_host_that_is_named_is_better_than_a_domain_that_has_it(self) -> None:
        assert match_host(["*.example.org", "api.example.org"], "api.example.org") is Match.EXACT

    def test_an_address_is_listed_only_by_itself(self) -> None:
        assert match_host(["192.168.1.20"], "192.168.1.20") is Match.EXACT
        assert match_host(["192.168.1.20"], "192.168.1.21") is None
        assert match_host(["*.1.20"], "192.168.1.20") is None
        assert match_host(["*.168.1.20"], "10.168.1.20") is None

    def test_nothing_is_listed_by_the_name_of_nothing(self) -> None:
        assert match_host(["*.example.org", ""], "") is None
        assert match_host(["a.org"], "  ") is None

    @given(host=st.text(alphabet="abcdefghij0123456789.-*", max_size=30))
    def test_a_grant_with_no_hosts_lists_nothing(self, host: str) -> None:
        assert match_host([], host) is None


class TestNamesAndAddresses:
    @pytest.mark.parametrize("host", ["1.2.3.4", "::1", "2606:4700::1111"])
    def test_an_address_is_one(self, host: str) -> None:
        assert is_address(host)

    @pytest.mark.parametrize("host", ["example.org", "1.2.3", "a.1", "", "1.2.3.4.example.org"])
    def test_a_name_is_not_one(self, host: str) -> None:
        assert not is_address(host)

    def test_a_host_is_compared_in_lower_case_and_without_the_last_dot(self) -> None:
        assert normalize_host(" Example.ORG. ") == "example.org"
