"""``factotum-device``, as a person runs it: what it writes, what it tries, and the exit code that
says how it ended, with a sound card and an edge that a test plays."""

import asyncio
import hashlib
import io
import logging
import runpy
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import numpy as np
import pytest

from factotum.client_audio import OpenWakeWordEngine, WakeWordEngine, WakeWordModels
from factotum.client_audio.backend import AudioDevice
from factotum.client_audio.testing import FakeBackend, FakeWakeWord, KeyPresses
from factotum.client_audio.wakeword import ModelFile
from factotum.contracts.capabilities import Capability
from factotum.contracts.edge import CloseCode, ErrorFrame, PartFrame, TurnEnded, TurnStatus
from factotum.contracts.response import Speech
from factotum.device.app import EngineFactory, engine_of
from factotum.device.cli import EXAMPLE, main, main_async
from factotum.device.config import CONFIG_VARIABLE, TOKEN_VARIABLE, ListeningSettings
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


def a_word_that_is_a_loud_sound(
    score: float = 0.9, made: list[OpenWakeWordEngine] | None = None
) -> EngineFactory:
    """What listens for the word, made up: any frame that is loud is the word (the made-up models
    answer with the mean of what they are given: silence is an embedding of 2, a loud frame one of
    about 48). The engines that are made are kept in ``made``, which is how a test knows that it
    was the device that asked for this one."""

    def engine(listening: ListeningSettings) -> Callable[[], WakeWordEngine]:
        def make() -> WakeWordEngine:
            word = FakeWakeWord(lambda features: score if features[0, -1, 0] > 20 else 0.0).engine
            if made is not None:
                made.append(word)
            return word

        return make

    return engine


async def ran(
    *arguments: str,
    environ: dict[str, str] | None = None,
    edge: Edge | None = None,
    backend: FakeBackend | None = None,
    presses: KeyPresses | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    engine: EngineFactory = engine_of,
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
        transport=transport,
        engine=engine,
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


MELSPECTROGRAM = b"the spectrogram"
WORD = b"the word of a model"


def models_of(directory: Path | None) -> WakeWordModels:
    """A catalogue of two little models, known by size and hash as the real ones are."""

    def known(name: str, content: bytes) -> ModelFile:
        return ModelFile(name, len(content), hashlib.sha256(content).hexdigest())

    return WakeWordModels(
        directory,
        words={"hey_jarvis": known("hey_jarvis_v0.1.onnx", WORD)},
        shared=(known("melspectrogram.onnx", MELSPECTROGRAM),),
    )


class Release:
    """What serves the models, as a release of GitHub does: each file at its own address, with an
    answer for what it does not have."""

    def __init__(self, files: dict[str, bytes] | None = None, status: int = 200) -> None:
        self.files = {"melspectrogram.onnx": MELSPECTROGRAM, "hey_jarvis_v0.1.onnx": WORD}
        self.files.update(files or {})
        self.status = status
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(str(request.url))
        name = request.url.path.rsplit("/", 1)[-1]
        if self.status != 200 or name not in self.files:
            return httpx.Response(self.status if self.status != 200 else 404)
        return httpx.Response(200, content=self.files[name])

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def small_catalogue(monkeypatch: pytest.MonkeyPatch) -> Callable[[Path], WakeWordModels]:
    """The real models are not in this repository, so what is fetched is two little files."""
    monkeypatch.setattr("factotum.device.cli.WakeWordModels", models_of)
    return models_of


class TestFetchingTheModelsOfAWord:
    async def test_the_files_of_the_word_are_fetched_and_kept_where_they_were_asked_to_be(
        self, tmp_path: Path, small_catalogue: Callable[[Path], WakeWordModels]
    ) -> None:
        release = Release()
        folder = tmp_path / "models"

        code, said = await ran(
            "wake-word", "download", "--dir", str(folder), transport=release.transport
        )

        assert code == 0
        assert (folder / "melspectrogram.onnx").read_bytes() == MELSPECTROGRAM
        assert (folder / "hey_jarvis_v0.1.onnx").read_bytes() == WORD
        assert not list(folder.glob("*.part"))
        assert [url.rsplit("/", 1)[-1] for url in release.asked] == [
            "melspectrogram.onnx",
            "hey_jarvis_v0.1.onnx",
        ]
        assert all(
            url.startswith("https://github.com/dscripka/openWakeWord/") for url in release.asked
        )
        assert "melspectrogram.onnx" in said
        assert str(folder) in said

    async def test_the_licence_of_the_models_is_said_for_they_are_not_the_projects_own(
        self, tmp_path: Path, small_catalogue: Callable[[Path], WakeWordModels]
    ) -> None:
        _, said = await ran(
            "wake-word", "download", "--dir", str(tmp_path), transport=Release().transport
        )

        assert "CC BY-NC-SA 4.0" in said
        assert "not commercial" in said

    async def test_the_word_is_hey_jarvis_unless_another_is_asked_for(
        self, tmp_path: Path, small_catalogue: Callable[[Path], WakeWordModels]
    ) -> None:
        release = Release()

        await ran("wake-word", "download", "--dir", str(tmp_path), transport=release.transport)

        assert any("hey_jarvis" in url for url in release.asked)

    async def test_what_is_there_already_is_not_fetched_again(
        self, tmp_path: Path, small_catalogue: Callable[[Path], WakeWordModels]
    ) -> None:
        await ran("wake-word", "download", "--dir", str(tmp_path), transport=Release().transport)
        again = Release()

        code, said = await ran(
            "wake-word", "download", "--dir", str(tmp_path), transport=again.transport
        )

        assert code == 0
        assert again.asked == []
        assert "there already" in said

    async def test_a_word_that_there_is_no_model_for_is_said_before_anything_is_fetched(
        self, tmp_path: Path
    ) -> None:
        release = Release()

        code, said = await ran(
            "wake-word", "download", "computer", "--dir", str(tmp_path), transport=release.transport
        )

        assert code == 1
        assert "no model for 'computer'" in said
        assert "alexa, hey_jarvis, hey_mycroft, hey_rhasspy" in said
        assert release.asked == []  # and so nothing could have been kept

    async def test_a_file_that_is_not_the_one_expected_is_refused_and_nothing_is_kept(
        self, tmp_path: Path, small_catalogue: Callable[[Path], WakeWordModels]
    ) -> None:
        release = Release({"hey_jarvis_v0.1.onnx": b"something else"})

        code, said = await ran(
            "wake-word", "download", "--dir", str(tmp_path), transport=release.transport
        )

        assert code == 1
        assert "factotum-device: hey_jarvis_v0.1.onnx: what came is not the file" in said
        assert not (tmp_path / "hey_jarvis_v0.1.onnx").exists()

    async def test_a_server_that_does_not_have_it_is_said_and_is_exit_code_1(
        self, tmp_path: Path, small_catalogue: Callable[[Path], WakeWordModels]
    ) -> None:
        code, said = await ran(
            "wake-word", "download", "--dir", str(tmp_path), transport=Release(status=503).transport
        )

        assert code == 1
        assert "answered 503" in said

    async def test_it_needs_no_file_of_settings_and_no_sound_card(
        self, tmp_path: Path, small_catalogue: Callable[[Path], WakeWordModels]
    ) -> None:
        backend = FakeBackend()
        backend.refuses["devices"] = "PortAudio could not be loaded"

        code, _ = await ran(
            "--config",
            str(tmp_path / "nothing.yaml"),
            "wake-word",
            "download",
            "--dir",
            str(tmp_path / "models"),
            backend=backend,
            transport=Release().transport,
        )

        assert code == 0

    async def test_the_folder_is_the_one_next_to_the_voices_unless_it_is_said(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        small_catalogue: Callable[[Path], WakeWordModels],
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        code, _ = await ran("wake-word", "download", transport=Release().transport)

        assert code == 0
        assert (tmp_path / ".cache" / "factotum" / "openwakeword" / "melspectrogram.onnx").exists()

    def test_a_command_with_no_action_is_the_usage_of_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as stopped:
            asyncio.run(main_async(["wake-word"], environ={}, out=io.StringIO()))

        assert stopped.value.code == 2
        assert "download" in capsys.readouterr().err


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

    async def test_a_device_that_waits_for_a_word_whose_models_are_not_there_is_told_what_to_run(
        self, tmp_path: Path
    ) -> None:
        text = SETTINGS.replace(
            "listening:\n", f"listening:\n  mode: wake_word\n  models_dir: {tmp_path / 'models'}\n"
        )
        edge = Edge()
        edge.connection.say(ready())

        code, said = await ran("--config", str(settings_in(tmp_path, text)), "doctor", edge=edge)

        assert code == 1
        assert "FAIL  wake word" in said
        assert "wake-word download hey_jarvis --dir" in said

    async def test_the_word_can_be_asked_about_whatever_the_device_is_woken_by(
        self, tmp_path: Path
    ) -> None:
        text = SETTINGS.replace(
            "listening:\n", f"listening:\n  models_dir: {tmp_path / 'models'}\n"
        )
        edge = Edge()
        edge.connection.say(ready())

        code, said = await ran(
            "--config", str(settings_in(tmp_path, text)), "doctor", "--word", edge=edge
        )

        assert code == 1
        assert "FAIL  wake word" in said
        assert "FAIL  word" not in said  # nothing was heard: the models are not there
        assert "ok    word" not in said

    async def test_it_listens_for_the_word_when_asked_to_and_says_when_to_say_it(
        self, tmp_path: Path
    ) -> None:
        backend = FakeBackend()
        edge = Edge()
        edge.connection.say(ready())
        text = SETTINGS.replace("listening:\n", "listening:\n  mode: wake_word\n")
        task = asyncio.create_task(
            ran(
                "--config",
                str(settings_in(tmp_path, text)),
                "doctor",
                "--word",
                edge=edge,
                backend=backend,
                engine=a_word_that_is_a_loud_sound(),
            )
        )
        await until(lambda: backend.inputs)
        for at in range(70):  # five seconds are 62 frames
            backend.inputs[0].hear(FakeWakeWord.frame(6_000 if at == 20 else 0))

        code, said = await asyncio.wait_for(task, 3)

        assert code == 0
        assert said.splitlines()[0] == "say 'hey jarvis' now: listening for 5 seconds"
        assert "ok    wake word" in said
        assert "ok    word" in said
        assert "'hey_jarvis' was heard" in said


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

    async def test_porcupine_with_no_key_is_said_before_anything_is_tried(
        self, tmp_path: Path
    ) -> None:
        edge = Edge()
        text = SETTINGS.replace(
            "listening:\n  silence_seconds: 0.1\n",
            "listening:\n  silence_seconds: 0.1\n  mode: wake_word\n  engine: porcupine\n",
        )

        code, said = await ran("--config", str(settings_in(tmp_path, text)), edge=edge)

        assert code == 1
        assert "access key" in said
        assert edge.asked == []

    async def test_a_word_whose_models_are_not_there_is_said_with_what_to_run_and_is_exit_code_1(
        self, tmp_path: Path
    ) -> None:
        edge = Edge()
        edge.connection.say(ready())
        folder = tmp_path / "models"
        text = SETTINGS.replace(
            "listening:\n", f"listening:\n  mode: wake_word\n  models_dir: {folder}\n"
        )

        code, said = await asyncio.wait_for(
            ran("--config", str(settings_in(tmp_path, text)), edge=edge), 2
        )

        assert code == 1
        assert f'factotum-device wake-word download hey_jarvis --dir "{folder}"' in said
        assert "Traceback" not in said
        assert len(edge.connection.frames("bye")) == 1  # it did not leave the edge hanging

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
        self, tmp_path: Path
    ) -> None:
        edge = Edge()
        edge.connection.say(ready())
        text = SETTINGS.replace("listening:\n", "listening:\n  mode: wake_word\n")
        made: list[OpenWakeWordEngine] = []
        task = asyncio.create_task(
            ran(
                "--config",
                str(settings_in(tmp_path, text)),
                edge=edge,
                engine=a_word_that_is_a_loud_sound(made=made),
            )
        )
        await edge.connection.wait_for("hello")
        await until(lambda: made)  # it is the device that asked for the engine that it was given

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
