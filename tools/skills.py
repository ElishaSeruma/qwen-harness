"""Portable, instruction-only Agent Skills registry for Qwen Harness."""
from __future__ import annotations

import re
from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"
USER_SKILLS_ROOT = Path.home() / ".qwen-harness" / "skills"
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip('"\'')
    return fields, match.group(2).strip()


def list_skills(include_instructions: bool = False) -> list[dict[str, object]]:
    """Return the installed, portable skill catalog without executing skill code."""
    records = []
    # Built-in skills are bundled with the app; user skills live in app state
    # so “Add skill” works even when the app bundle itself is read-only.
    for path in sorted([*SKILLS_ROOT.glob("*/SKILL.md"), *USER_SKILLS_ROOT.glob("*/SKILL.md")]):
        fields, body = _frontmatter(path.read_text(encoding="utf-8"))
        name = fields.get("name", path.parent.name)
        if not _NAME.fullmatch(name) or any(record["name"] == name for record in records):
            continue
        item: dict[str, object] = {
            "name": name,
            "description": fields.get("description", "No description provided."),
            "command": f"/{name}",
            "source": fields.get("source", "Local Qwen Harness skill"),
            "safety": fields.get("safety", "Reviewed instruction-only skill; no bundled executable code."),
            "tools": [tool.strip() for tool in fields.get("tools", "").split(",") if tool.strip()],
        }
        if include_instructions:
            item["instructions"] = body
        records.append(item)
    return records


def get_skill(name: str) -> dict[str, object] | None:
    return next((skill for skill in list_skills(True) if skill["name"] == name), None)


def create_skill(name: str, description: str, instructions: str, tools: list[str] | None = None) -> dict[str, object]:
    """Create one user-authored, instruction-only skill without executable files."""
    name = str(name or "").strip().lower()
    description = " ".join(str(description or "").split())
    instructions = str(instructions or "").strip()
    if not _NAME.fullmatch(name):
        raise ValueError("Skill name must use lowercase letters, numbers, and hyphens.")
    if not description or len(description) > 500:
        raise ValueError("Skill description is required and must be 500 characters or fewer.")
    if not instructions or len(instructions) > 12000:
        raise ValueError("Skill instructions are required and must be 12,000 characters or fewer.")
    if get_skill(name):
        raise ValueError(f"A skill named /{name} is already installed.")
    selected_tools = [str(tool).strip() for tool in (tools or []) if re.fullmatch(r"[a-z][a-z0-9_]*", str(tool).strip())]
    USER_SKILLS_ROOT.mkdir(parents=True, exist_ok=True)
    folder = USER_SKILLS_ROOT / name
    folder.mkdir(mode=0o700)
    content = "\n".join([
        "---", f"name: {name}", f"description: {description}",
        "source: Added locally in Qwen Harness", "safety: User-authored instruction-only skill; review its instructions before use.",
        f"tools: {','.join(selected_tools)}", "---", instructions, "",
    ])
    path = folder / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return get_skill(name) or {}
