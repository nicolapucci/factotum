"""``factotum-device``, as a person runs it: what it writes, what it tries, and the exit code that
says how it ended, with a sound card and an edge that a test plays."""

import asyncio
import io
import logging
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from factotum.client_audio.backend import AudioDevice
from factotum.client_audio.testing import FakeBackend, KeyPresses
from factotum.contracts.capabilities import Capability
from factotum.contracts.edge import CloseCode, ErrorFrame, PartFrame, TurnEnded, TurnStatus
from factotum.contracts.response import Speech
from factotum.device.cli import EXAMPLE, main, main_async
from factotum.device.config import CONFIG_VARIABLE, TOKEN_VARIABLE
from factotum.device.testing import Edge, ready, until

SETTINGS = """\
device:
  hardware_id: kitchen-01
  token: kitchen-credential
listening:
  silence_seconds: 0.1
reconnect:
  first_s: 0.01
  max_s: 0.02
"""


def settings_in(tmp_path: Path, text: str = SETTINGS) -> Path:
    path = tmp_path / "device.yaml"
    path.write_bytes(text.encode("utf-8"))
    return path


async def ran(
    *arguments: str,
    environ: dict[str, str] | None = None,
    edge: Edge | None = None,
    backend: FakeBackend | None = None,
    presses: KeyPresses | None = None,
) -> tuple[int, str]:
    out = io.StringIO()
    code = await main_async(
        list(arguments),
        environ={} if environ is None else environ,
        system="linux",
        out=out,
        backend=backend or FakeBackend(),
        presses=presses,
        opening=(edge or Edge()).open,
    )
    return code, out.getvalue()


class TestInit:
    async def test_it_writes_a_file_of_settings_that_can_be_read(self, tmp_path: Path) -> None:
        path = tmp_path / "device.yaml"

        code, said = await ran("--config", str(path), "init")

        assert code == 0
        assert path.read_bytes().decode("utf-8") == EXAMPLE
        assert str(path) in said

    async def test_it_does_not_replace_what_a_person_wrote_unless_told_to(
        self, tmp_path: Path
    ) -> None:
        path = settings_in(tmp_path, "mine")

        code, said = await ran("--config", str(path), "init")

        assert code == 1
        assert "--force" in said
        assert path.read_bytes() == b"mine"

        code, _ = await ran("--config", str(path), "init", "--force")

        assert code == 0
        assert path.read_bytes().decode("utf-8") == EXAMPLE

    async def test_it_writes_where_the_variable_says_and_makes_the_folder(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "somewhere" / "else" / "device.yaml"

        code, _ = await ran("init", environ={CONFIG_VARIABLE: str(path)})

        assert code == 0
        assert path.exists()


class TestDevices:
    async def test_each_is_a_line_with_what_it_is_and_which_is_the_default(self) -> None:
        listed = [
            AudioDevice(0, "Microphone (USB)", 2, 0, 44_100.0, "MME"),
            AudioDevice(1, "Speakers", 0, 2, 48_000.0, "WASAPI"),
        ]

        code, said = await ran("devices", backend=FakeBackend(listed))

        assert code == 0
        assert said.splitlines() == [
            "  0  Microphone (USB)  [MME; in]  <- default microphone",
            "  1  Speakers  [WASAPI; out]  <- default speaker",
        ]

    async def test_a_machine_that_cannot_list_them_says_why(self) -> None:
        backend = FakeBackend()
        backend.refuses["devices"] = "PortAudio could not be loaded"

        code, said = await ran("devices", backend=backend)

        assert code == 1
        assert "PortAudio" in said


class TestDoctor:
    async def test_a_device_that_is_all_right_is_told_so_and_the_exit_code_is_0(
        self, tmp_path: Path
    ) -> None:
        edge = Edge()
        edge.connection.say(ready())

        code, said = await ran("--config", str(settings_in(tmp_path)), "doctor", edge=edge)

        assert code == 0
        assert "FAIL" not in said
        assert "ok    edge" in said

    async def test_what_is_wrong_is_said_and_the_exit_code_is_1(self, tmp_path: Path) -> None:
        code, said = await ran(
            "--config", str(settings_in(tmp_path)), "doctor", edge=Edge(failing=1)
        )

        assert code == 1
        assert "FAIL  edge" in said

    async def test_it_need_not_touch_the_edge(self, tmp_path: Path) -> None:
        edge = Edge()

        code, said = await ran(
            "--config", str(settings_in(tmp_path)), "doctor", "--no-edge", edge=edge
        )

        assert code == 0
        assert "edge" not in said.replace("edge.", "")
        assert edge.asked == []

    async def test_it_plays_a_tone_when_asked_to(self, tmp_path: Path) -> None:
        backend = FakeBackend()
        edge = Edge()
        edge.connection.say(ready())

        code, said = await ran(
            "--config", str(settings_in(tmp_path)), "doctor", "--play", edge=edge, backend=backend
        )

        assert code == 0
        assert "did you hear it" in said
        assert backend.outputs[0].sound

    async def test_it_listens_for_a_moment_when_asked_to(self, tmp_path: Path) -> None:
        backend = FakeBackend()
        edge = Edge()
        edge.connection.say(ready())
        task = asyncio.create_task(
            ran(
                "--config",
                str(settings_in(tmp_path)),
                "doctor",
                "--listen",
                edge=edge,
                backend=backend,
            )
        )
        await until(lambda: backend.inputs)
        for _ in range(70):
            backend.inputs[0].hear(np.full(512, 300, dtype=np.int16))

        code, said = await asyncio.wait_for(task, 2)

        assert code == 0
        assert "ok    listening" in said
        assert "mean level 300" in said

    async def test_a_file_of_settings_that_is_wrong_is_what_it_says_and_nothing_else(
        self, tmp_path: Path
    ) -> None:
        path = settings_in(tmp_path, "device:\n  hardware_id: kitchen-01\n")

        code, said = await ran("--config", str(path), "doctor", environ={})

        assert code == 1
        assert TOKEN_VARIABLE in said


class TestRunning:
    async def run_until_it_has_said_hello(
        self,
        tmp_path: Path,
        edge: Edge,
        presses: KeyPresses,
        text: str = SETTINGS,
        **changes: object,
    ) -> asyncio.Task[tuple[int, str]]:
        task = asyncio.create_task(
            ran("--config", str(settings_in(tmp_path, text)), edge=edge, presses=presses, **changes)  # type: ignore[arg-type]
        )
        await edge.connection.wait_for("hello")
        return task

    async def test_it_is_the_device_until_the_person_leaves_and_that_is_exit_code_0(
        self, tmp_path: Path
    ) -> None:
        edge, presses = Edge(), KeyPresses()
        edge.connection.say(ready())
        task = await self.run_until_it_has_said_hello(tmp_path, edge, presses)

        presses.leave()
        code, said = await asyncio.wait_for(task, 2)

        assert code == 0
        assert "connected as Kitchen speaker" in said
        assert "press Enter to speak" in said

    async def test_a_device_with_no_microphone_is_not_told_to_press_anything(
        self, tmp_path: Path
    ) -> None:
        edge, presses = Edge(), KeyPresses()
        edge.connection.say(ready(frozenset({Capability.AUDIO_OUT})))
        text = SETTINGS.replace(
            "  token: kitchen-credential\n",
            "  token: kitchen-credential\n  capabilities: [audio_out]\n",
        )
        task = await self.run_until_it_has_said_hello(tmp_path, edge, presses, text)

        edge.connection.drop(int(CloseCode.REPLACED))
        code, said = await asyncio.wait_for(task, 2)

        assert code == 4
        assert "press Enter" not in said
        assert "no microphone" in said

    @pytest.mark.parametrize(
        ("at_a_keyboard", "said_so", "not_said"),
        [(True, "press Enter to speak", "wake word"), (False, "wake word", "press Enter")],
    )
    async def test_a_device_that_nobody_sits_at_is_told_that_push_to_talk_has_no_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        at_a_keyboard: bool,
        said_so: str,
        not_said: str,
    ) -> None:
        class Input(io.StringIO):
            """A terminal, or what a service has for standard input: the end of a file."""

            def isatty(self) -> bool:
                return at_a_keyboard

        monkeypatch.setattr(sys, "stdin", Input(""))
        edge = Edge()
        edge.connection.say(ready())

        code, said = await asyncio.wait_for(
            ran("--config", str(settings_in(tmp_path)), edge=edge), 2
        )

        assert code == 0  # nobody pressed anything, the input is over, and that is leaving
        assert said_so in said
        assert not_said not in said

    @pytest.mark.parametrize(
        ("closed_with", "code"),
        [
            (CloseCode.REPLACED, 4),
            (CloseCode.UNSUPPORTED_VERSION, 5),
            (CloseCode.POLICY, 6),
        ],
    )
    async def test_an_edge_that_makes_clear_it_is_no_use_asking_again_is_an_exit_code_that_says_why(
        self, tmp_path: Path, closed_with: CloseCode, code: int
    ) -> None:
        edge, presses = Edge(), KeyPresses()
        edge.connection.say(ready())
        task = await self.run_until_it_has_said_hello(tmp_path, edge, presses)

        edge.connection.drop(int(closed_with), "go away")
        exit_code, said = await asyncio.wait_for(task, 2)

        assert exit_code == code
        assert "go away" in said
        assert len(edge.asked) == 1

    async def test_a_device_that_the_edge_does_not_accept_is_exit_code_3(
        self, tmp_path: Path
    ) -> None:
        edge = Edge()
        edge.connection.say(ErrorFrame(code="auth_failed", message="who are you?", fatal=True))

        code, said = await ran("--config", str(settings_in(tmp_path)), edge=edge)

        assert code == 3
        assert "refused: " in said
        assert "auth_failed" in said

    async def test_settings_that_are_wrong_are_said_and_nothing_is_connected_to(
        self, tmp_path: Path
    ) -> None:
        edge = Edge()
        path = settings_in(
            tmp_path, "device:\n  hardware_id: kitchen-01\n  token: x\naudio:\n  volume: 500\n"
        )

        code, said = await ran("--config", str(path), edge=edge)

        assert code == 1
        assert "audio.volume" in said
        assert edge.asked == []

    async def test_a_file_that_is_not_there_says_how_to_make_one(self, tmp_path: Path) -> None:
        code, said = await ran("--config", str(tmp_path / "nothing.yaml"))

        assert code == 1
        assert "init" in said

    async def test_a_word_to_wake_it_with_and_no_key_for_it_is_said_before_anything_is_tried(
        self, tmp_path: Path
    ) -> None:
        edge = Edge()
        text = SETTINGS.replace(
            "listening:\n  silence_seconds: 0.1\n",
            "listening:\n  silence_seconds: 0.1\n  mode: wake_word\n",
        )

        code, said = await ran("--config", str(settings_in(tmp_path, text)), edge=edge)

        assert code == 1
        assert "access key" in said
        assert edge.asked == []

    async def test_a_sound_card_that_fails_when_the_person_speaks_is_said_and_is_exit_code_1(
        self, tmp_path: Path
    ) -> None:
        edge, presses, backend = Edge(), KeyPresses(), FakeBackend()
        backend.refuses["open_input"] = "Unanticipated host error"
        edge.connection.say(ready())
        task = asyncio.create_task(
            ran("--config", str(settings_in(tmp_path)), edge=edge, presses=presses, backend=backend)
        )
        await edge.connection.wait_for("hello")

        presses.press()
        code, said = await asyncio.wait_for(task, 2)

        assert code == 1
        assert "sound hardware failed" in said
        assert "Unanticipated host error" in said

    @pytest.mark.parametrize(("flags", "written"), [([], True), (["--quiet"], False)])
    async def test_a_device_that_was_told_to_be_quiet_does_not_write_what_is_said(
        self, tmp_path: Path, flags: list[str], written: bool
    ) -> None:
        edge, presses, backend = Edge(), KeyPresses(), FakeBackend()
        edge.connection.say(ready(features=frozenset()))
        task = asyncio.create_task(
            ran(
                "--config",
                str(settings_in(tmp_path)),
                *flags,
                edge=edge,
                presses=presses,
                backend=backend,
            )
        )
        await edge.connection.wait_for("hello")
        presses.press()
        await until(lambda: backend.inputs and not backend.inputs[-1].closed)
        for block in [np.full(512, 1000, dtype=np.int16)] * 4 + [np.zeros(512, dtype=np.int16)] * 6:
            backend.hearing.hear(block)
        [started] = await edge.connection.wait_for("audio_start")
        await edge.connection.wait_for("audio_end")
        edge.connection.say(
            PartFrame(utterance_id=started.id, seq=0, part=Speech(text="what is said"))
        )
        edge.connection.say(TurnEnded(utterance_id=started.id, status=TurnStatus.FINISHED))
        presses.leave()

        code, said = await asyncio.wait_for(task, 2)

        assert code == 0
        assert ("[say]    what is said" in said) is written
        assert "connected as" in said  # what is not about the answer is written all the same

    async def test_a_device_that_waits_for_a_word_is_not_told_to_press_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Engine:
            sample_rate, frame_length = 16_000, 512

            def process(self, frame: Any) -> int:
                return -1

            def delete(self) -> None:
                """Nothing to let go of."""

        monkeypatch.setitem(
            sys.modules, "pvporcupine", SimpleNamespace(create=lambda **given: Engine())
        )
        edge = Edge()
        edge.connection.say(ready())
        text = SETTINGS.replace(
            "listening:\n", "listening:\n  mode: wake_word\n  access_key: a-key\n"
        )
        task = asyncio.create_task(ran("--config", str(settings_in(tmp_path, text)), edge=edge))
        await edge.connection.wait_for("hello")

        edge.connection.drop(int(CloseCode.REPLACED))
        code, said = await asyncio.wait_for(task, 2)

        assert code == 4
        assert "press Enter" not in said
        assert "wake word" not in said  # it is not told that there is no key, either

    async def test_the_log_level_of_the_settings_is_the_one_that_is_used(
        self, tmp_path: Path
    ) -> None:
        root = logging.getLogger()
        before = root.level
        try:
            text = SETTINGS + "log_level: DEBUG\n"

            code, _ = await ran("--config", str(settings_in(tmp_path, text)), "doctor", "--no-edge")

            assert code == 0
            assert root.level == logging.DEBUG
        finally:
            root.setLevel(before)


class TestMain:
    def test_python_dash_m_is_the_same_as_the_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "device.yaml"
        monkeypatch.setattr(sys, "argv", ["factotum-device", "--config", str(path), "init"])

        with pytest.raises(SystemExit) as exit_:
            runpy.run_module("factotum.device", run_name="__main__")

        assert exit_.value.code == 0
        assert path.exists()

    def test_it_exits_with_the_code_of_what_was_done(self, tmp_path: Path) -> None:
        path = tmp_path / "device.yaml"

        with pytest.raises(SystemExit) as exit_:
            main(["--config", str(path), "init"])

        assert exit_.value.code == 0
        assert path.exists()

    def test_a_person_who_presses_control_c_has_left_and_that_is_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupted(coroutine: object) -> None:
            coroutine.close()  # type: ignore[attr-defined]
            raise KeyboardInterrupt

        monkeypatch.setattr("factotum.device.cli.asyncio.run", interrupted)

        with pytest.raises(SystemExit) as exit_:
            main(["devices"])

        assert exit_.value.code == 0
