"""Configuration of the actions service, read from the environment.

The database is the one of ``DatabaseSettings`` (``FACTOTUM_POSTGRES_*``, the names compose uses to
create it). Nothing here names a model, a provider or a key: the small model that reads a message is
asked for through the gateway, in the role it has there (``chat.fast``), and whatever a skill needs
in order to run is the runner's (slice 5b).
"""

from datetime import timedelta
from pathlib import Path
from typing import Self

from pydantic import AnyHttpUrl, Field, model_validator

from factotum.actions.service import ActionSettings
from factotum.common.config import Environment
from factotum.db.settings import DatabaseSettings


class ActionsSettings(DatabaseSettings):
    actions_host: str = "127.0.0.1"
    """The interface to listen on. Loopback unless told otherwise: the service is for the other
    services and is not published outside the deployment."""

    actions_port: int = Field(default=8085, ge=1, le=65535)

    actions_gateway_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8081")
    """Where the model gateway is: it is asked to read every message and to embed every skill."""

    actions_devices_file: Path | None = None
    """The devices, and which of them each skill drives (development only, until the hub registers
    them): the file the edge reads too. A file that is named and is not there stops the service;
    without one there are no devices."""

    actions_skills_dir: Path | None = None
    """A folder of skills (each a folder with a ``SKILL.md`` and its scripts) that is read again on
    ``POST /v1/skills/reload``: the way to install a skill that has scripts."""

    actions_enable_skills_dir: bool = False
    """Enable what is read from the folder as it is read, as if it had been reviewed. For
    development, where the folder is one's own: it is refused anywhere else."""

    actions_match_threshold: float = Field(default=0.45, ge=0, le=1)
    """How near, by meaning, a message must be to what a skill says about itself for the skill to
    be a candidate. Models differ in how near unrelated texts are: tune it to yours."""

    actions_match_candidates: int = Field(default=6, ge=1, le=20)
    """How many skills the small model is shown for a message."""

    actions_match_timeout_s: float = Field(default=4.0, gt=0, le=60)
    """How long the embedding of a message is waited for. Without it, the skills that could not be
    scored are all the model is shown."""

    actions_index_timeout_s: float = Field(default=8.0, gt=0, le=120)
    """How long the embedding of a skill is waited for, in the background, when it is enabled."""

    actions_planner_timeout_s: float = Field(default=10.0, gt=0, le=120)
    """How long the small model is waited for to read a message. One that is not read in time is
    taken to be a question."""

    actions_reader_timeout_s: float = Field(default=6.0, gt=0, le=120)
    """How long the small model is waited for to read a yes or a no that the words did not
    settle."""

    actions_confirmation_seconds: int = Field(default=60, ge=5, le=3600)
    """How long a yes or a no is waited for. What was understood is not kept longer: a yes that
    comes after a while is a yes to something else."""

    actions_approval_wait_s: float = Field(default=20.0, ge=0, le=600)
    """How long a turn waits for an approver."""

    actions_approval_minutes: int = Field(default=10, ge=1, le=24 * 60)
    """How long an approval request is open, as the approver is told."""

    actions_run_margin_s: float = Field(default=5.0, ge=0, le=60)
    """What is added to the time limit of a skill for everything that is not the skill."""

    actions_max_devices_shown: int = Field(default=10, ge=1, le=50)
    """How many other devices the small model is shown by name."""

    @model_validator(mode="after")
    def _a_folder_is_enabled_by_reading_only_in_development(self) -> Self:
        if self.actions_enable_skills_dir and self.environment not in {
            Environment.DEVELOPMENT,
            Environment.TEST,
        }:
            raise ValueError(
                "FACTOTUM_ACTIONS_ENABLE_SKILLS_DIR is for development: a skill runs when an "
                "administrator has reviewed and enabled it"
            )
        return self

    @property
    def action(self) -> ActionSettings:
        return ActionSettings(
            confirmation_ttl=timedelta(seconds=self.actions_confirmation_seconds),
            approval_wait_s=self.actions_approval_wait_s,
            approval_ttl=timedelta(minutes=self.actions_approval_minutes),
            run_margin_s=self.actions_run_margin_s,
            max_devices_shown=self.actions_max_devices_shown,
        )
