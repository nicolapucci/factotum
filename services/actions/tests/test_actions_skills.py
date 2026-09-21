import sys
from pathlib import Path

import pytest

from factotum.actions.skills import (
    MAX_FILE_BYTES,
    MAX_FILES,
    SKILL_FILE,
    load_folder,
    parse_skill,
    read_skill_folder,
)
from factotum.contracts.actions import MAX_SKILL_SOURCE_CHARS
from factotum.contracts.effects import Effect
from factotum.contracts.errors import SkillInvalidError
from factotum.contracts.skills import SkillMode, SkillTarget

LAMP = """\
---
name: lamp
description: Turn a lamp on or off, or set its brightness.
examples:
  - turn on the lamp
target: external
parameters:
  state: {type: string, enum: ["on", "off"]}
  brightness: {type: integer, minimum: 0, maximum: 100}
required: [state]
device_parameters:
  entity_id: {type: string}
  host: {type: string}
permissions:
  network: ["${device.host}"]
effect: reversible
entrypoint: scripts/run.py
---
Switches the lamp through its hub.
"""

PLAIN = """\
---
name: weather
description: Say what the weather is like.
effect: read_only
---
"""


def frontmatter(**lines: str) -> str:
    body = "\n".join(f"{key}: {value}" for key, value in lines.items())
    return f"---\n{body}\n---\nText.\n"


class TestReadingASkill:
    def test_the_frontmatter_is_the_manifest_and_what_follows_is_the_instructions(self) -> None:
        skill = parse_skill(LAMP, files={"scripts/run.py": b"print(1)"})

        assert skill.manifest.name == "lamp"
        assert skill.manifest.target is SkillTarget.EXTERNAL
        assert skill.manifest.mode is SkillMode.SCRIPT
        assert skill.instructions == "Switches the lamp through its hub."

    def test_a_skill_with_nothing_after_its_frontmatter_has_no_instructions(self) -> None:
        skill = parse_skill(PLAIN)

        assert (skill.manifest.effect, skill.instructions) == (Effect.READ_ONLY, "")

    def test_line_ends_and_a_byte_order_mark_make_no_difference(self) -> None:
        crlf = chr(0xFEFF) + PLAIN.replace("\n", "\r\n")

        assert parse_skill(crlf).content_sha256 == parse_skill(PLAIN).content_sha256

    def test_a_skill_that_has_no_entrypoint_is_in_instructions_mode(self) -> None:
        assert parse_skill(PLAIN).manifest.mode is SkillMode.INSTRUCTIONS


class TestWhatIsNotASkill:
    @pytest.mark.parametrize(
        ("source", "message"),
        [
            ("just some text", "starts with a line of three dashes"),
            ("", "starts with a line of three dashes"),
            ("---\nname: x\n", "not closed by a line of three dashes"),
            ("---\nname: [unclosed\n---\n", "not valid YAML (line "),
            ("---\n- a list\n---\n", "must be a mapping"),
            ("---\n---\n", "must be a mapping"),
            ("---\nname: lamp\n---\n", "description: Field required"),
            (
                "---\nname: lamp\ndescription: Lamp.\n---\n",
                "effect: Field required",
            ),
            (
                "---\nname: Not A Slug\ndescription: x\neffect: read_only\n---\n",
                "name: String should match pattern",
            ),
            (
                "---\nname: lamp\ndescription: x\neffect: read_only\ncolour: red\n---\n",
                "colour: Extra inputs are not permitted",
            ),
            (
                "---\nname: lamp\ndescription: x\neffect: severe\n---\n",
                "effect: Input should be",
            ),
        ],
    )
    def test_it_is_refused_and_says_where_and_why(self, source: str, message: str) -> None:
        with pytest.raises(SkillInvalidError) as error:
            parse_skill(source, where="lamps/lamp")

        assert error.value.message.startswith("lamps/lamp: ")
        assert message in error.value.message

    def test_an_unquoted_on_and_off_is_told_what_is_wrong_with_it(self) -> None:
        source = (
            "---\nname: lamp\ndescription: x\neffect: reversible\n"
            "parameters:\n  state: {type: string, enum: [on, off]}\n---\n"
        )

        with pytest.raises(SkillInvalidError, match="do not match type string"):
            parse_skill(source)

    def test_what_was_written_is_not_said_when_it_is_wrong(self) -> None:
        secret = "sk-this-is-a-key-that-was-pasted-in-the-wrong-place"

        with pytest.raises(SkillInvalidError) as error:
            parse_skill(frontmatter(name="lamp", description="x", effect=secret))

        assert secret not in error.value.message

    def test_a_skill_is_at_most_so_long(self) -> None:
        with pytest.raises(SkillInvalidError, match=f"at most {MAX_SKILL_SOURCE_CHARS}"):
            parse_skill(PLAIN + "x" * MAX_SKILL_SOURCE_CHARS)


class TestTheEntrypoint:
    def test_a_script_that_is_not_in_the_folder_is_refused(self) -> None:
        with pytest.raises(SkillInvalidError, match=r"entrypoint scripts/run\.py is not in"):
            parse_skill(LAMP, files={"other.py": b""})

    def test_a_skill_given_as_text_cannot_have_a_script(self) -> None:
        with pytest.raises(SkillInvalidError, match="given as text cannot have an entrypoint"):
            parse_skill(LAMP)

    def test_a_script_cannot_be_outside_the_skill(self) -> None:
        source = LAMP.replace("scripts/run.py", "../run.py")

        with pytest.raises(SkillInvalidError, match="relative path inside the skill folder"):
            parse_skill(source, files={"../run.py": b""})


class TestWhichVersionItIs:
    def test_the_same_skill_has_the_same_hash_and_another_has_another(self) -> None:
        files = {"scripts/run.py": b"print(1)"}

        assert parse_skill(LAMP, files=files).content_sha256 == (
            parse_skill(LAMP, files=dict(files)).content_sha256
        )
        assert parse_skill(LAMP, files=files).content_sha256 != (
            parse_skill(LAMP.replace("brightness", "level"), files=files).content_sha256
        )

    def test_changing_a_script_is_a_new_version(self) -> None:
        one = parse_skill(LAMP, files={"scripts/run.py": b"print(1)"})
        two = parse_skill(LAMP, files={"scripts/run.py": b"print(2)"})

        assert one.content_sha256 != two.content_sha256
        assert len(one.content_sha256) == 64

    def test_a_file_moved_is_another_version_even_with_the_same_content(self) -> None:
        one = parse_skill(LAMP, files={"scripts/run.py": b"x", "a.txt": b"y"})
        two = parse_skill(LAMP, files={"scripts/run.py": b"x", "b.txt": b"y"})

        assert one.content_sha256 != two.content_sha256


def write(folder: Path, text: str = LAMP, files: dict[str, str] | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / SKILL_FILE).write_text(text, encoding="utf-8")
    for path, content in (files or {}).items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return folder


class TestAFolder:
    def test_a_skill_folder_brings_its_scripts_with_it(self, tmp_path: Path) -> None:
        folder = write(tmp_path / "lamp", files={"scripts/run.py": "print('hi')"})

        skill = read_skill_folder(folder, where="lamp")

        assert skill.files == {"scripts/run.py": b"print('hi')"}

    def test_hidden_files_and_caches_stay_out_of_it(self, tmp_path: Path) -> None:
        folder = write(tmp_path / "lamp")
        (folder / "scripts").mkdir()
        (folder / "scripts" / "run.py").write_text("x", encoding="utf-8")
        (folder / ".git").mkdir()
        (folder / ".git" / "config").write_text("secret", encoding="utf-8")
        (folder / "scripts" / "__pycache__").mkdir()
        (folder / "scripts" / "__pycache__" / "run.pyc").write_bytes(b"\0")
        (folder / ".env").write_text("KEY=1", encoding="utf-8")

        assert set(read_skill_folder(folder, where="lamp").files) == {"scripts/run.py"}

    @pytest.mark.skipif(sys.platform == "win32", reason="links need a privilege on Windows")
    def test_a_link_is_not_followed_out_of_the_folder(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        folder = write(tmp_path / "lamp")
        (folder / "scripts").mkdir()
        (folder / "scripts" / "run.py").write_text("x", encoding="utf-8")
        (folder / "link.txt").symlink_to(outside)

        assert set(read_skill_folder(folder, where="lamp").files) == {"scripts/run.py"}

    def test_a_file_that_is_too_big_is_refused(self, tmp_path: Path) -> None:
        folder = write(tmp_path / "lamp", PLAIN)
        (folder / "data.bin").write_bytes(b"0" * (MAX_FILE_BYTES + 1))

        with pytest.raises(SkillInvalidError, match=r"data\.bin is more than"):
            read_skill_folder(folder, where="lamp")

    def test_a_folder_with_too_many_files_is_refused(self, tmp_path: Path) -> None:
        folder = write(tmp_path / "lamp", PLAIN)
        for number in range(MAX_FILES + 1):
            (folder / f"f{number}.txt").write_text("x", encoding="utf-8")

        with pytest.raises(SkillInvalidError, match=f"at most {MAX_FILES} files"):
            read_skill_folder(folder, where="lamp")

    def test_a_skill_file_that_is_not_text_is_refused(self, tmp_path: Path) -> None:
        folder = tmp_path / "lamp"
        folder.mkdir()
        (folder / SKILL_FILE).write_bytes(b"---\nname: \xff\xfe\n---\n")

        with pytest.raises(SkillInvalidError, match="cannot be read as text"):
            read_skill_folder(folder, where="lamp")


class TestAFolderOfSkills:
    def test_every_skill_in_it_is_read_wherever_it_is(self, tmp_path: Path) -> None:
        write(tmp_path / "home" / "lamp", files={"scripts/run.py": "x"})
        write(tmp_path / "weather", PLAIN)

        skills, problems = load_folder(tmp_path)

        assert sorted(s.manifest.name for s in skills) == ["lamp", "weather"]
        assert problems == []

    def test_a_skill_that_cannot_be_read_is_reported_and_the_others_are_read(
        self, tmp_path: Path
    ) -> None:
        write(tmp_path / "good", PLAIN)
        write(tmp_path / "broken", "not a skill")
        write(tmp_path / "script-missing", LAMP)

        skills, problems = load_folder(tmp_path)

        assert [s.manifest.name for s in skills] == ["weather"]
        assert sorted(p.where for p in problems) == ["broken", "script-missing"]
        assert all(p.message.startswith(p.where) for p in problems)

    def test_a_name_that_two_skills_share_is_a_problem_for_the_second(self, tmp_path: Path) -> None:
        write(tmp_path / "a", PLAIN)
        write(tmp_path / "b", PLAIN)

        skills, problems = load_folder(tmp_path)

        assert len(skills) == 1
        [problem] = problems
        assert problem.where == "b"
        assert "already the skill of a" in problem.message

    def test_a_folder_with_no_skills_is_no_problem(self, tmp_path: Path) -> None:
        assert load_folder(tmp_path) == ([], [])
