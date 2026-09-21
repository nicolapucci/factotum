"""What crosses to the runner service: a script, what it is given, where it may go, what it did."""

from typing import Any

import pytest
from pydantic import ValidationError

from factotum.contracts.runner import (
    MAX_JOB_BYTES,
    MAX_JOB_FILE_BYTES,
    MAX_JOB_FILES,
    MAX_OUTPUT_CHARS,
    MAX_STDIN_CHARS,
    Job,
    JobFile,
    JobResult,
    RunResult,
    SkillOutput,
)
from factotum.contracts.skills import is_reserved_env_name


def job(**changes: Any) -> Job:
    fields: dict[str, Any] = {
        "label": "lamp",
        "files": (JobFile.of("scripts/run.py", b"print(1)"),),
        "entrypoint": "scripts/run.py",
        "timeout_s": 10,
    }
    return Job.model_validate({**fields, **changes})


class TestAFileOfTheFolder:
    def test_what_goes_in_comes_out(self) -> None:
        file = JobFile.of("scripts/run.py", b"print(1)\n")

        assert (file.path, file.data) == ("scripts/run.py", b"print(1)\n")

    @pytest.mark.parametrize(
        "path", ["run.py", "scripts/run.py", "a/b/c.txt", "data-1.json", "_x", "a.b.c"]
    )
    def test_a_path_inside_the_folder_is_fine(self, path: str) -> None:
        assert JobFile.of(path, b"x").path == path

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "../outside.py",
            "a/../../b",
            "a/./b",
            "./a",
            "a//b",
            "a/",
            "a\\b",
            "a b",
            "a\x00b",
            "é.py",
            "a/..",
            "..",
            ".",
        ],
    )
    def test_a_way_out_of_the_folder_is_not_a_path(self, path: str) -> None:
        with pytest.raises(ValidationError, match="path says where"):
            JobFile(path=path, content="eA==")

    def test_the_content_is_base64_and_nothing_else(self) -> None:
        with pytest.raises(ValidationError, match="base64"):
            JobFile(path="a.py", content="not base64!")

    def test_a_file_is_not_bigger_than_a_folder_may_hold(self) -> None:
        JobFile.of("a.py", b"x" * MAX_JOB_FILE_BYTES)
        with pytest.raises(ValidationError, match="at most"):
            JobFile.of("a.py", b"x" * (MAX_JOB_FILE_BYTES + 100))


class TestAJob:
    def test_it_is_a_script_and_what_it_is_given(self) -> None:
        made = job(
            stdin='{"params": {}}',
            env={"HA_TOKEN": "secret"},
            hosts=["homeassistant.local", "*.example.org"],
        )

        assert made.entrypoint == "scripts/run.py"
        assert made.env == {"HA_TOKEN": "secret"}
        assert made.hosts == ("homeassistant.local", "*.example.org")

    def test_the_script_to_run_is_one_of_the_files(self) -> None:
        with pytest.raises(ValidationError, match="one of the files"):
            job(entrypoint="scripts/other.py")

    @pytest.mark.parametrize("entrypoint", ["run.sh", "run", "run.py.txt", "run.js"])
    def test_only_python_scripts_are_run(self, entrypoint: str) -> None:
        with pytest.raises(ValidationError, match="only Python"):
            job(files=[JobFile.of(entrypoint, b"x")], entrypoint=entrypoint)

    def test_a_file_is_there_once(self) -> None:
        with pytest.raises(ValidationError, match="once"):
            job(files=[JobFile.of("a.py", b"1"), JobFile.of("a.py", b"2")], entrypoint="a.py")

    def test_a_folder_has_at_least_its_script_and_at_most_a_few_files(self) -> None:
        with pytest.raises(ValidationError):
            job(files=[])
        files = [JobFile.of(f"f{i}.py", b"x") for i in range(MAX_JOB_FILES + 1)]
        with pytest.raises(ValidationError):
            job(files=files, entrypoint="f0.py")

    def test_the_files_are_not_more_than_a_folder_may_hold_in_all(self) -> None:
        big = b"x" * MAX_JOB_FILE_BYTES
        files = [
            JobFile.of(f"f{i}.py", big) for i in range(MAX_JOB_BYTES // MAX_JOB_FILE_BYTES + 1)
        ]

        with pytest.raises(ValidationError, match="in all"):
            job(files=files, entrypoint="f0.py")

    @pytest.mark.parametrize("timeout_s", [0, -1, 121])
    def test_it_has_a_time_limit_of_a_couple_of_minutes_at_most(self, timeout_s: int) -> None:
        with pytest.raises(ValidationError):
            job(timeout_s=timeout_s)

    def test_what_it_reads_is_not_a_book(self) -> None:
        job(stdin="x" * MAX_STDIN_CHARS)
        with pytest.raises(ValidationError):
            job(stdin="x" * (MAX_STDIN_CHARS + 1))

    def test_an_unknown_field_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            job(privileged=True)


class TestWhatItIsGiven:
    @pytest.mark.parametrize("name", ["HA_TOKEN", "A", "ROUTER_PASSWORD", "API_KEY_2"])
    def test_a_secret_is_given_by_a_name_in_capitals(self, name: str) -> None:
        assert job(env={name: "x"}).env == {name: "x"}

    @pytest.mark.parametrize(
        "name", ["ha_token", "1TOKEN", "HA-TOKEN", "HA TOKEN", "", "TOKEN=1", "É", "X" * 65]
    )
    def test_a_name_that_is_not_one_is_refused(self, name: str) -> None:
        with pytest.raises(ValidationError, match="name in capitals"):
            job(env={name: "x"})

    @pytest.mark.parametrize(
        "name",
        [
            "PATH",
            "HOME",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "FACTOTUM_RUNNER_KEY",
            "TMPDIR",
        ],
    )
    def test_a_secret_cannot_be_called_what_changes_how_a_program_starts(self, name: str) -> None:
        assert is_reserved_env_name(name)
        with pytest.raises(ValidationError, match="not the runner's"):
            job(env={name: "x"})

    def test_a_value_is_text_and_not_a_long_one(self) -> None:
        with pytest.raises(ValidationError, match="not a long one"):
            job(env={"A": "x" * 9000})
        with pytest.raises(ValidationError, match="not a long one"):
            job(env={"A": "a\x00b"})

    def test_not_more_than_twenty(self) -> None:
        job(env={f"S{i}": "x" for i in range(20)})
        with pytest.raises(ValidationError):
            job(env={f"S{i}": "x" for i in range(21)})


class TestWhereItMayGo:
    @pytest.mark.parametrize(
        "host", ["api.example.org", "*.example.org", "homeassistant.local", "192.168.1.20", "a"]
    )
    def test_a_host_is_a_name_a_name_with_a_wildcard_or_an_address(self, host: str) -> None:
        assert job(hosts=[host]).hosts == (host,)

    @pytest.mark.parametrize(
        "host",
        [
            "http://example.org",
            "example.org/x",
            "example.org:443",
            "a b",
            "*",
            "*.",
            "",
            "-a.b",
            "a..b",
        ],
    )
    def test_anything_else_is_refused(self, host: str) -> None:
        with pytest.raises(ValidationError, match="a host is"):
            job(hosts=[host])

    def test_hosts_are_lower_case_and_listed_once(self) -> None:
        assert job(hosts=["API.Example.org", "api.example.org", "b.org"]).hosts == (
            "api.example.org",
            "b.org",
        )

    def test_not_more_than_twenty(self) -> None:
        job(hosts=[f"h{i}.org" for i in range(20)])
        with pytest.raises(ValidationError):
            job(hosts=[f"h{i}.org" for i in range(21)])

    def test_no_hosts_is_no_network(self) -> None:
        assert job().hosts == ()


class TestWhatItDid:
    def test_it_ended_by_itself_or_it_did_not(self) -> None:
        assert JobResult(exit_code=0, stdout="{}").exit_code == 0
        stopped = JobResult(timed_out=True)
        assert (stopped.exit_code, stopped.timed_out, stopped.truncated) == (None, True, False)

    def test_what_it_wrote_is_kept_up_to_a_limit(self) -> None:
        JobResult(stdout="x" * MAX_OUTPUT_CHARS, stderr="y" * MAX_OUTPUT_CHARS)
        with pytest.raises(ValidationError):
            JobResult(stdout="x" * (MAX_OUTPUT_CHARS + 1))
        with pytest.raises(ValidationError):
            JobResult(stderr="x" * (MAX_OUTPUT_CHARS + 1))

    def test_it_cannot_take_no_time_at_all_backwards(self) -> None:
        with pytest.raises(ValidationError):
            JobResult(duration_ms=-1)


class TestWhatAScriptPrints:
    def test_a_message_data_and_commands_are_what_it_says(self) -> None:
        output = SkillOutput.model_validate(
            {
                "message": "Volume is 40.",
                "data": {"level": 40},
                "commands": [{"name": "set_volume", "level": 40}],
            }
        )

        assert output.message == "Volume is 40."
        assert output.data == {"level": 40}
        assert [command.name for command in output.commands] == ["set_volume"]

    def test_it_may_say_nothing_at_all(self) -> None:
        output = SkillOutput.model_validate({})

        assert (output.message, output.data, output.commands) == (None, {}, ())

    @pytest.mark.parametrize(
        "printed",
        [
            {"messsage": "typo"},
            {"dry_run": True},
            {"message": ""},
            {"commands": [{"name": "open_the_pod_bay_doors"}]},
            {"commands": [{"name": "stop"}] * 21},
        ],
        ids=["unknown", "dry-run", "empty", "no-such-command", "too-many"],
    )
    def test_what_the_platform_does_not_hear_is_refused(self, printed: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            SkillOutput.model_validate(printed)

    def test_a_result_is_an_output_that_can_also_say_nothing_was_run(self) -> None:
        assert RunResult.model_validate({"dry_run": True}).dry_run
        assert not RunResult.model_validate({}).dry_run
