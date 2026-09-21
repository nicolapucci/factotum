"""Where a run may go: which hosts, and which of the addresses they name."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.egress.policy import (
    Match,
    is_address,
    match_host,
    normalize_host,
    refusal_of_address,
)


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


class TestWhichAddressesAreFine:
    @pytest.mark.parametrize("match", list(Match))
    @pytest.mark.parametrize("address", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"])
    def test_a_public_address_is_fine_for_a_host_that_is_listed_either_way(
        self, address: str, match: Match
    ) -> None:
        assert refusal_of_address(address, match) is None

    @pytest.mark.parametrize("address", ["10.0.0.5", "172.16.3.4", "192.168.1.20", "fd12::5"])
    def test_a_private_address_is_for_a_host_that_is_named(self, address: str) -> None:
        assert refusal_of_address(address, Match.EXACT) is None
        assert "private" in str(refusal_of_address(address, Match.WILDCARD))

    @pytest.mark.parametrize("match", list(Match))
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "127.1.2.3",
            "::1",
            "0.0.0.0",  # noqa: S104 - an address that is refused
            "::",
            "::ffff:127.0.0.1",
            "64:ff9b::7f00:1",
        ],
    )
    def test_this_machine_is_never_reached_whatever_the_grant_says(
        self, address: str, match: Match
    ) -> None:
        assert refusal_of_address(address, match) == "it is this machine"

    @pytest.mark.parametrize("match", list(Match))
    @pytest.mark.parametrize(
        "address",
        [
            "169.254.169.254",
            "169.254.0.1",
            "fe80::1",
            "224.0.0.1",
            "ff02::1",
            "240.0.0.1",
            "100.100.100.200",
            "fd00:ec2::254",
            "::ffff:169.254.169.254",
        ],
    )
    def test_what_is_not_a_place_a_skill_can_go_is_never_reached(
        self, address: str, match: Match
    ) -> None:
        assert refusal_of_address(address, match) == "it is not a place a skill can go"

    def test_an_address_that_wraps_a_private_one_is_the_private_one(self) -> None:
        assert refusal_of_address("::ffff:10.0.0.5", Match.EXACT) is None
        assert "private" in str(refusal_of_address("::ffff:10.0.0.5", Match.WILDCARD))

    @pytest.mark.parametrize("address", ["", "not-an-address", "1.2.3", "1.2.3.4.5", "999.1.1.1"])
    def test_what_is_not_an_address_is_not_reached(self, address: str) -> None:
        assert refusal_of_address(address, Match.EXACT) == "it is not an address"

    @given(
        octets=st.tuples(*[st.integers(min_value=0, max_value=255)] * 4),
        match=st.sampled_from(list(Match)),
    )
    def test_no_address_of_this_machine_is_reached_by_any_way_of_writing_it(
        self, octets: tuple[int, int, int, int], match: Match
    ) -> None:
        address = ".".join(str(part) for part in octets)
        wrapped = f"::ffff:{address}"
        if octets[0] == 127 or octets == (0, 0, 0, 0):
            assert refusal_of_address(address, match) is not None
            assert refusal_of_address(wrapped, match) is not None


class TestNamesAndAddresses:
    @pytest.mark.parametrize("host", ["1.2.3.4", "::1", "2606:4700::1111"])
    def test_an_address_is_one(self, host: str) -> None:
        assert is_address(host)

    @pytest.mark.parametrize("host", ["example.org", "1.2.3", "a.1", "", "1.2.3.4.example.org"])
    def test_a_name_is_not_one(self, host: str) -> None:
        assert not is_address(host)

    def test_a_host_is_compared_in_lower_case_and_without_the_last_dot(self) -> None:
        assert normalize_host(" Example.ORG. ") == "example.org"
