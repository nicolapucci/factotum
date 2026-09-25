"""Reading a skill: a folder with a ``SKILL.md`` and, optionally, the scripts it runs.

A ``SKILL.md`` is YAML frontmatter between two lines of three dashes, and markdown after it: the
instructions, or notes for humans. The frontmatter is a ``SkillManifest`` (``factotum.contracts.
skills``), and this is where it is read, checked and given the hash that says which version it is.

What is wrong with a skill is said where and why and never with what was written: a skill may be
somebody's, and a value in it may be a secret in the wrong place. A folder of skills is read
skill by skill, and one that cannot be read is a problem to report, not a reason to read none.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from factotum.contracts.actions import MAX_SKILL_SOURCE_CHARS, SkillLoadProblem
from factotum.contracts.errors import SkillInvalidError
from factotum.contracts.skills import (
    MAX_INSTRUCTIONS_CHARS,
    SkillManifest,
    SkillMode,
    is_skill_path,
)

SKILL_FILE = "SKILL.md"

MAX_FILES = 50
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 5_000_000
"""What a skill folder may hold besides its ``SKILL.md``: its scripts and what they read."""

_SKIPPED_DIRECTORIES = {"__pycache__", "node_modules"}
_BOM = chr(0xFEFF)


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    manifest: SkillManifest
    instructions: str
    source: str
    """The text of the ``SKILL.md``, its line ends made ``\\n``."""

    files: dict[str, bytes] = field(default_factory=dict)
    """The other files of the folder by their path in it (with ``/``), never a path outside it."""

    @property
    def content_sha256(self) -> str:
        """What identifies this version: the ``SKILL.md`` and every file, so that changing a
        script is a new version, and the same skill read again on another system is the same."""
        digest = hashlib.sha256()
        digest.update(self.source.encode("utf-8"))
        for path in sorted(self.files):
            digest.update(b"\0" + path.encode("utf-8") + b"\0")
            digest.update(self.files[path])
        return digest.hexdigest()


def parse_skill(
    source: str, *, where: str = "the skill", files: dict[str, bytes] | None = None
) -> LoadedSkill:
    """A skill from the text of its ``SKILL.md`` and the files beside it. Raises
    ``SkillInvalidError`` when it is not one. Without ``files`` (a skill given as text) it cannot
    have an entrypoint: there is nothing to run."""
    text = source.removeprefix(_BOM).replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_SKILL_SOURCE_CHARS:
        raise SkillInvalidError(
            f"{where}: a {SKILL_FILE} is at most {MAX_SKILL_SOURCE_CHARS} characters"
        )
    lines = text.split("\n")
    if lines[0].strip() != "---":
        raise SkillInvalidError(
            f"{where}: a {SKILL_FILE} starts with a line of three dashes, then its frontmatter"
        )
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise SkillInvalidError(f"{where}: the frontmatter is not closed by a line of three dashes")
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = f" (line {mark.line + 2})" if mark is not None else ""
        raise SkillInvalidError(f"{where}: the frontmatter is not valid YAML{line}") from None
    if not isinstance(frontmatter, dict):
        raise SkillInvalidError(f"{where}: the frontmatter must be a mapping of names to values")
    try:
        manifest = SkillManifest.model_validate(frontmatter)
    except ValidationError as error:
        raise SkillInvalidError(f"{where}: {_problems(error)}") from None

    held = files or {}
    if manifest.entrypoint is not None and manifest.entrypoint not in held:
        reason = (
            f"the entrypoint {manifest.entrypoint} is not in the skill's folder"
            if files is not None
            else "a skill given as text cannot have an entrypoint: its scripts come with a folder"
        )
        raise SkillInvalidError(f"{where}: {reason}")
    instructions = "\n".join(lines[end + 1 :]).strip()
    if manifest.mode is SkillMode.INSTRUCTIONS and len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise SkillInvalidError(
            f"{where}: the instructions of a skill that has no script are at most "
            f"{MAX_INSTRUCTIONS_CHARS} characters (a model reads them whole every time), and these "
            f"are {len(instructions)}"
        )
    return LoadedSkill(manifest=manifest, instructions=instructions, source=text, files=held)


def _problems(error: ValidationError) -> str:
    """What is wrong with a manifest, field by field, and never what it was given."""
    return "; ".join(
        f"{'.'.join(str(part) for part in problem['loc']) or '(frontmatter)'}: {problem['msg']}"
        for problem in error.errors(include_input=False, include_context=False, include_url=False)
    )


def load_folder(directory: Path) -> tuple[list[LoadedSkill], list[SkillLoadProblem]]:
    """Every skill under ``directory`` (a folder with a ``SKILL.md`` in it is one), and what could
    not be read. A name that two skills of the folder share is a problem for the second."""
    skills: list[LoadedSkill] = []
    problems: list[SkillLoadProblem] = []
    names: dict[str, str] = {}
    for skill_file in sorted(directory.rglob(SKILL_FILE)):
        folder = skill_file.parent
        where = str(folder.relative_to(directory)).replace("\\", "/") or "."
        try:
            loaded = read_skill_folder(folder, where=where)
        except SkillInvalidError as error:
            problems.append(SkillLoadProblem(where=where, message=error.message[:500]))
            continue
        earlier = names.get(loaded.manifest.name)
        if earlier is not None:
            problems.append(
                SkillLoadProblem(
                    where=where,
                    message=(
                        f"{where}: the name {loaded.manifest.name} is already the skill of "
                        f"{earlier}"
                    ),
                )
            )
            continue
        names[loaded.manifest.name] = where
        skills.append(loaded)
    return skills, problems


def read_skill_folder(folder: Path, *, where: str) -> LoadedSkill:
    """The skill in one folder. Raises ``SkillInvalidError``."""
    try:
        source = (folder / SKILL_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise SkillInvalidError(f"{where}: {SKILL_FILE} cannot be read as text") from None
    return parse_skill(source, where=where, files=_files_of(folder, where))


def _files_of(folder: Path, where: str) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    total = 0
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if any(part.startswith(".") or part in _SKIPPED_DIRECTORIES for part in relative.parts):
            continue
        if path.is_symlink() or not path.is_file() or relative.as_posix() == SKILL_FILE:
            continue  # a link could point out of the folder
        if not is_skill_path(relative.as_posix()):
            raise SkillInvalidError(
                f"{where}: the name of {relative.as_posix()!r} has more than letters, digits, "
                "dots, dashes and underscores: a script cannot be run with it"
            )
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise SkillInvalidError(
                f"{where}: {relative.as_posix()} is more than {MAX_FILE_BYTES} bytes"
            )
        total += size
        if len(found) >= MAX_FILES or total > MAX_TOTAL_BYTES:
            raise SkillInvalidError(
                f"{where}: a skill has at most {MAX_FILES} files and {MAX_TOTAL_BYTES} bytes"
            )
        try:
            found[relative.as_posix()] = path.read_bytes()
        except OSError:
            raise SkillInvalidError(f"{where}: {relative.as_posix()} cannot be read") from None
    return found
