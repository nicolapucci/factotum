"""What is asked to be confirmed.

When the decision is to ask for a plain yes or no, what is asked is kept until the answer comes: the
skill and the version that was chosen, the values, the device it is for, the plan a model
wrote for it if the skill is instructions, and the decision as it was.
The answer confirms *this*, exactly; whatever the person says next is either a yes to it, a no, or
something else (``confirmations``), and something else drops it.
"""

from typing import Literal

from pydantic import Field

from factotum.contracts.authorization import Decision
from factotum.contracts.base import Contract
from factotum.contracts.ids import DeviceId, SkillId
from factotum.contracts.instructions import InstructionPlan
from factotum.contracts.skills import ParamValue


class Proposal(Contract):
    skill_id: SkillId
    skill_name: str
    version: int
    values: dict[str, ParamValue]
    device_params: dict[str, ParamValue] = Field(default_factory=dict)
    target_id: DeviceId | None = None
    target_kind: Literal["system", "external"] | None = None
    language: str
    question: str | None = None
    decision: Decision
    untrusted_content_in_turn: bool = False
    plan: InstructionPlan | None = None
    """For a skill that is instructions: what a model wrote for it and the checks accepted.
    What is confirmed is this, and it is what runs: the model is not asked again."""
