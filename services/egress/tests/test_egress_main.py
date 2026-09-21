"""Starting the proxy the way a container does: from the environment, on a port."""

import asyncio
import contextlib
import socket

import pytest

from factotum.egress.__main__ import main, serve
from factotum.egress.settings import EgressSettings

KEY = "k" * 32


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestStarting:
    def test_without_a_key_it_does_not_start(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("FACTOTUM_EGRESS_KEY", raising=False)

        with pytest.raises(SystemExit) as raised:
            main()

        assert raised.value.code == 2
        assert "FACTOTUM_EGRESS_KEY is not set" in capsys.readouterr().err

    def test_with_a_key_that_is_too_short_it_does_not_start(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("FACTOTUM_EGRESS_KEY", "short")

        with pytest.raises(SystemExit) as raised:
            main()

        assert raised.value.code == 2
        assert "at least 32 characters" in capsys.readouterr().err

    def test_a_port_that_is_taken_is_said_and_it_does_not_start(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            monkeypatch.setenv("FACTOTUM_EGRESS_KEY", KEY)
            monkeypatch.setenv("FACTOTUM_EGRESS_PORT", str(taken.getsockname()[1]))

            with pytest.raises(SystemExit) as raised:
                main()

        assert raised.value.code == 2
        assert "cannot start" in capsys.readouterr().err


class TestServing:
    async def test_it_listens_where_it_is_told_and_asks_a_run_for_its_grant(self) -> None:
        port = free_port()
        settings = EgressSettings(
            _env_file=None,
            egress_port=port,
            egress_key=KEY,  # type: ignore[arg-type]
            egress_max_mb=1,
            egress_connect_ports="443, 8443",  # type: ignore[arg-type]
        )
        serving = asyncio.create_task(serve(settings))
        try:
            for _ in range(50):
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            else:  # pragma: no cover - it did not start
                pytest.fail("the proxy did not start")
            writer.write(b"GET http://api.example.org/ HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(), 5)
            writer.close()
        finally:
            serving.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await serving

        assert response.startswith(b"HTTP/1.1 407 ")
