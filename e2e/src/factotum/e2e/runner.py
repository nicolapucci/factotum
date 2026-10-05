"""The runner of the house: the scripts and the plans of the skills, run for real in a process of
their own, except for what this machine cannot be.

A skill that reaches the weather does it over https, and the web server that stands for every
host here speaks plain http. Such a skill is given what it would have printed (``canned``) and the
job it was given is kept like any other, so that what a test looks at is what it can look at: that
the right skill was run, with the values that were said, the hosts and the secrets that it declared
and no others. What the script does with them is the script's test (``services/actions/tests``).
"""

import json
from collections.abc import Callable
from typing import Any

from factotum.actions.instructions.testing import LocalRunner
from factotum.contracts.runner import Job, JobResult

Canned = Callable[[Job], dict[str, Any]]
"""What a skill prints, as the output of a skill reads it (``message``, ``data``, ``commands``),
for the job that it was given."""


class HouseRunner(LocalRunner):
    """The runner of the tests, with a way to say what a skill that cannot run here prints."""

    def __init__(self, *, proxy: str | None = None) -> None:
        super().__init__(proxy=proxy)
        self.canned: dict[str, Canned] = {}
        """What a skill prints instead of being run, by its name. The jobs are kept either way."""

    def run(self, job: Job) -> JobResult:
        printed = self.canned.get(job.label)
        if printed is None:
            return super().run(job)
        return JobResult(exit_code=0, stdout=json.dumps(printed(job)), duration_ms=0)
