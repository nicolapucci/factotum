"""The documentation must not promise something the code would reject."""

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from factotum.contracts.skills import SkillManifest

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_skill_example_in_the_design_document_is_a_valid_manifest() -> None:
    text = (REPO_ROOT / "docs" / "design.md").read_text(encoding="utf-8")
    match = re.search(r"```yaml\n---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "the skill example is missing from docs/design.md"

    manifest = SkillManifest.model_validate(yaml.safe_load(match.group(1)))

    assert manifest.name == "living-room-lights"
    assert manifest.device_parameters, "the example should show per-device parameters"


def test_unquoted_on_and_off_in_a_real_yaml_file_are_rejected_with_a_clear_error() -> None:
    frontmatter = yaml.safe_load(
        "name: lights\n"
        "description: Switch the lights.\n"
        "effect: reversible\n"
        "parameters:\n"
        "  state: {type: string, enum: [on, off]}\n"
    )

    with pytest.raises(ValidationError, match="do not match type string"):
        SkillManifest.model_validate(frontmatter)


def test_every_decision_record_in_the_index_exists() -> None:
    index = (REPO_ROOT / "docs" / "decisions" / "README.md").read_text(encoding="utf-8")
    linked = re.findall(r"\]\((\d{4}-[a-z0-9-]+\.md)\)", index)

    assert len(linked) >= 9
    missing = [name for name in linked if not (REPO_ROOT / "docs" / "decisions" / name).exists()]
    assert missing == []
