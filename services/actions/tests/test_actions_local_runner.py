"""The runner that runs jobs in this process, which the tests use where what a script does has to be
real: it has to read and say the way the sandbox does, on any machine."""

from factotum.actions.instructions.testing import LocalRunner
from factotum.contracts.runner import Job, JobFile


def job(source: str, *, stdin: str = "", timeout_s: int = 20) -> Job:
    return Job(
        label="local-test",
        files=(JobFile.of("script.py", source.encode("utf-8")),),
        entrypoint="script.py",
        stdin=stdin,
        timeout_s=timeout_s,
    )


class TestALocalRunner:
    def test_what_a_script_is_told_and_says_is_utf_8_whatever_the_code_page_of_the_machine_is(
        self,
    ) -> None:
        source = "import sys\nprint(sys.stdin.read().upper())\n"

        result = LocalRunner().run(job(source, stdin="la cena è pronta \U0001f600"))

        assert (result.exit_code, result.stdout) == (0, "LA CENA È PRONTA \U0001f600")

    def test_what_is_not_utf_8_is_replaced_and_is_not_a_crash(self) -> None:
        source = "import sys\nsys.stdout.buffer.write(b'caf\\xe8\\n')\n"

        result = LocalRunner().run(job(source))

        assert (result.exit_code, result.stdout) == (0, "caf\ufffd")

    def test_a_script_that_takes_too_long_is_stopped(self) -> None:
        result = LocalRunner().run(job("import time\ntime.sleep(30)\n", timeout_s=1))

        assert result.timed_out
