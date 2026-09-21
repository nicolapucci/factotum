"""What the runner asks of the engine, and what it makes of the answers."""

import io
import json
import tarfile

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.runner.docker import (
    API,
    DockerEngine,
    DockerError,
    demultiplex,
    parse_host,
    tar_of,
)
from factotum.runner.testing import frame


class TestWhereTheEngineIs:
    def test_a_socket(self) -> None:
        assert parse_host("unix:///var/run/docker.sock") == (
            "/var/run/docker.sock",
            "http://docker",
        )

    @pytest.mark.parametrize(
        ("host", "base"),
        [
            ("tcp://192.168.1.5:2375", "http://192.168.1.5:2375"),
            ("http://engine:2375", "http://engine:2375"),
            ("https://engine:2376", "https://engine:2376"),
        ],
    )
    def test_a_port(self, host: str, base: str) -> None:
        assert parse_host(host) == (None, base)

    @pytest.mark.parametrize(
        "host", ["", "docker.sock", "npipe:////./pipe/docker_engine", "ssh://h"]
    )
    def test_anything_else_is_not_where_an_engine_is(self, host: str) -> None:
        with pytest.raises(ValueError, match="unix:// socket or a tcp://"):
            parse_host(host)


class TestWhatIsPutInAContainer:
    def test_files_and_the_directories_they_are_in_owned_by_root(self) -> None:
        archive = tar_of(
            {"skill": None, "skill/scripts": None, "skill/scripts/run.py": b"print(1)"}
        )

        with tarfile.open(fileobj=io.BytesIO(archive)) as opened:
            members = {member.name: member for member in opened}

        assert set(members) == {"skill", "skill/scripts", "skill/scripts/run.py"}
        assert members["skill"].isdir()
        assert members["skill/scripts/run.py"].isfile()
        assert {(m.uid, m.gid, m.uname) for m in members.values()} == {(0, 0, "root")}

    def test_a_directory_can_be_entered_and_a_file_is_read_by_everybody_and_written_by_nobody(
        self,
    ) -> None:
        archive = tar_of({"d": None, "d/f.py": b"x"})

        with tarfile.open(fileobj=io.BytesIO(archive)) as opened:
            modes = {member.name: member.mode for member in opened}

        assert modes == {"d": 0o755, "d/f.py": 0o644}

    def test_what_it_holds_is_what_was_given(self) -> None:
        archive = tar_of({"a.bin": bytes(range(256)) * 10, "empty.txt": b""})

        with tarfile.open(fileobj=io.BytesIO(archive)) as opened:
            held = {m.name: opened.extractfile(m).read() for m in opened if m.isfile()}  # type: ignore[union-attr]

        assert held == {"a.bin": bytes(range(256)) * 10, "empty.txt": b""}

    def test_the_same_archive_is_made_every_time(self) -> None:
        entries = {"skill": None, "skill/a.py": b"1"}

        assert tar_of(entries) == tar_of(entries)


class TestWhatAContainerWrote:
    def test_each_output_has_its_own_pieces(self) -> None:
        raw = frame(1, b"out one ") + frame(2, b"err") + frame(1, b"out two")

        kept, truncated = demultiplex(raw, limit=100)

        assert kept == {"stdout": b"out one out two", "stderr": b"err"}
        assert not truncated

    def test_no_output_is_no_output(self) -> None:
        assert demultiplex(b"", limit=10) == ({"stdout": b"", "stderr": b""}, False)

    def test_more_than_is_kept_is_cut_and_said(self) -> None:
        kept, truncated = demultiplex(frame(1, b"x" * 50) + frame(2, b"y" * 5), limit=20)

        assert (len(kept["stdout"]), len(kept["stderr"]), truncated) == (20, 5, True)

    def test_a_piece_that_is_not_an_output_is_not_kept(self) -> None:
        kept, _ = demultiplex(frame(0, b"stdin") + frame(3, b"other") + frame(1, b"out"), limit=10)

        assert kept == {"stdout": b"out", "stderr": b""}

    def test_a_stream_that_stops_in_the_middle_of_a_header_gives_what_came_before(self) -> None:
        kept, _ = demultiplex(frame(1, b"whole") + b"\x01\x00\x00", limit=10)

        assert kept["stdout"] == b"whole"

    def test_a_piece_that_says_it_is_longer_than_the_stream_gives_what_is_there(self) -> None:
        kept, _ = demultiplex(b"\x01\x00\x00\x00" + (1000).to_bytes(4, "big") + b"short", limit=100)

        assert kept["stdout"] == b"short"

    @given(
        pieces=st.lists(st.tuples(st.sampled_from([1, 2]), st.binary(max_size=40)), max_size=20),
        limit=st.integers(min_value=0, max_value=300),
    )
    def test_what_is_kept_is_the_start_of_what_was_written_and_never_more_than_the_limit(
        self, pieces: list[tuple[int, bytes]], limit: int
    ) -> None:
        raw = b"".join(frame(stream, data) for stream, data in pieces)
        written = {
            "stdout": b"".join(d for s, d in pieces if s == 1),
            "stderr": b"".join(d for s, d in pieces if s == 2),
        }

        kept, truncated = demultiplex(raw, limit=limit)

        for name, data in kept.items():
            assert len(data) <= limit
            assert written[name].startswith(data)
        assert truncated == any(len(data) > limit for data in written.values())


def engine_with(handler: httpx.MockTransport) -> DockerEngine:
    return DockerEngine(httpx.AsyncClient(transport=handler, base_url="http://docker"))


def answering(status: int = 200, **body: object) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler), seen


class TestSpeakingToTheEngine:
    async def test_every_call_is_to_a_version_of_the_api_that_was_asked_for(self) -> None:
        transport, seen = answering(201, Id="abc")

        await engine_with(transport).create("name", {"Image": "x"})

        assert seen[0].url.path == f"{API}/containers/create"
        assert seen[0].url.params["name"] == "name"
        assert json.loads(seen[0].content) == {"Image": "x"}

    async def test_a_container_is_made_and_its_id_is_kept(self) -> None:
        transport, _ = answering(201, Id="abc123")

        assert await engine_with(transport).create("n", {}) == "abc123"

    @pytest.mark.parametrize("body", [{}, {"Id": ""}, {"Id": 7}])
    async def test_a_container_that_is_made_without_saying_which_is_an_error(
        self, body: dict[str, object]
    ) -> None:
        transport, _ = answering(201, **body)

        with pytest.raises(DockerError, match="did not say which"):
            await engine_with(transport).create("n", {})

    async def test_files_are_put_at_the_root_of_the_container(self) -> None:
        transport, seen = answering(200)

        await engine_with(transport).put("abc", b"tar bytes")

        assert (seen[0].method, seen[0].url.path) == ("PUT", f"{API}/containers/abc/archive")
        assert seen[0].url.params["path"] == "/"
        assert seen[0].content == b"tar bytes"

    async def test_a_container_is_started_and_started_again_is_not_an_error(self) -> None:
        for status in (204, 304):
            transport, seen = answering(status)

            await engine_with(transport).start("abc")

            assert seen[0].url.path == f"{API}/containers/abc/start"

    async def test_the_exit_code_is_what_the_engine_says_when_it_has_ended(self) -> None:
        transport, seen = answering(200, StatusCode=3)

        assert await engine_with(transport).wait("abc") == 3
        assert seen[0].url.params["condition"] == "not-running"

    async def test_waiting_is_not_given_up_by_the_client(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(request.extensions["timeout"])
            return httpx.Response(200, json={"StatusCode": 0})

        await engine_with(httpx.MockTransport(handler)).wait("abc")

        assert captured == {"connect": None, "read": None, "write": None, "pool": None}

    async def test_what_is_wrong_with_the_exit_code_is_minus_one(self) -> None:
        transport, _ = answering(200, StatusCode="soon")

        assert await engine_with(transport).wait("abc") == -1

    @pytest.mark.parametrize("status", [204, 404, 409])
    async def test_a_container_that_is_killed_may_be_gone_or_over(self, status: int) -> None:
        transport, seen = answering(status)

        await engine_with(transport).kill("abc")

        assert seen[0].url.path == f"{API}/containers/abc/kill"

    async def test_how_it_ended_is_read_from_its_state(self) -> None:
        transport, _ = answering(200, State={"ExitCode": 137, "OOMKilled": True, "Error": "x"})

        exited = await engine_with(transport).inspect("abc")

        assert (exited.exit_code, exited.oom_killed, exited.error) == (137, True, "x")

    async def test_a_state_that_says_nothing_is_a_failure_and_not_an_oom(self) -> None:
        transport, _ = answering(200)

        exited = await engine_with(transport).inspect("abc")

        assert (exited.exit_code, exited.oom_killed) == (-1, False)

    async def test_what_a_container_wrote_is_read_from_its_log(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["stdout"] == request.url.params["stderr"] == "1"
            assert request.url.params["follow"] == "0"
            return httpx.Response(200, content=frame(1, b"out") + frame(2, b"err"))

        kept, cut = await engine_with(httpx.MockTransport(handler)).logs("abc", limit=10)

        assert (kept, cut) == ({"stdout": b"out", "stderr": b"err"}, False)

    async def test_it_is_removed_with_what_it_made_and_removing_what_is_gone_is_fine(self) -> None:
        for status in (204, 404):
            transport, seen = answering(status)

            await engine_with(transport).remove("abc")

            assert seen[0].method == "DELETE"
            assert (seen[0].url.params["force"], seen[0].url.params["v"]) == ("1", "1")

    async def test_the_containers_that_carry_a_label_are_listed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["all"] == "1"
            assert json.loads(request.url.params["filters"]) == {"label": ["factotum.sandbox"]}
            return httpx.Response(200, json=[{"Id": "a"}, "garbage", {"Id": "b"}])

        found = await engine_with(httpx.MockTransport(handler)).leftovers("factotum.sandbox")

        assert [item["Id"] for item in found] == ["a", "b"]

    async def test_the_image_is_there_or_it_is_not(self) -> None:
        transport, seen = answering(200)
        assert await engine_with(transport).has_image("python:3.13-slim")
        assert seen[0].url.raw_path.decode().endswith("/images/python%3A3.13-slim/json")

        transport, _ = answering(404, message="No such image")
        assert not await engine_with(transport).has_image("python:3.13-slim")

    async def test_an_engine_that_fails_to_say_is_not_an_engine_without_the_image(self) -> None:
        transport, _ = answering(500, message="boom")

        with pytest.raises(DockerError, match="500"):
            await engine_with(transport).has_image("python:3.13-slim")

    async def test_the_engine_answers_a_ping(self) -> None:
        transport, seen = answering(200)

        await engine_with(transport).ping()

        assert seen[0].url.path == f"{API}/_ping"


class TestWhenTheEngineDoesNotDoIt:
    @pytest.mark.parametrize("status", [400, 403, 404, 409, 500])
    async def test_what_it_says_is_the_error_with_its_status(self, status: int) -> None:
        transport, _ = answering(status, message="something is wrong with it")

        with pytest.raises(DockerError, match=f"{status}: something is wrong") as raised:
            await engine_with(transport).start("abc")

        assert raised.value.status == status

    async def test_what_is_not_json_is_the_reason_that_goes_with_the_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="<html>not json</html>")

        with pytest.raises(DockerError, match="500: Internal Server Error"):
            await engine_with(httpx.MockTransport(handler)).ping()

    async def test_an_engine_that_cannot_be_reached_is_an_error_that_says_only_that(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[Errno 2] No such file: /var/run/docker.sock")

        with pytest.raises(DockerError) as raised:
            await engine_with(httpx.MockTransport(handler)).ping()

        assert str(raised.value) == "the engine could not be reached (ConnectError)"
        assert raised.value.status is None

    async def test_a_message_of_the_engine_is_not_longer_than_a_line(self) -> None:
        transport, _ = answering(500, message="x" * 5000)

        with pytest.raises(DockerError) as raised:
            await engine_with(transport).ping()

        assert len(str(raised.value)) < 300
