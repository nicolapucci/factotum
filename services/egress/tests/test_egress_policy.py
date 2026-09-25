"""Where a run may go: which hosts, and which of the addresses they name."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.common.hosts import Match
from factotum.egress.policy import refusal_of_address


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
