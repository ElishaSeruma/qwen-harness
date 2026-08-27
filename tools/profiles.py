"""
tools/profiles.py — tool profiles and capability tiers, for the Python runtime.

The GUI's agent loop (`gui/agent-loop.js`) and weighbridge
(`gui/weighbridge.js`) already encode two policies:

  * **which tools a surface offers** — the prompt budget. All 40 schemas cost
    ~3k tokens, enough to stall a 4B model; a profile costs ~1.4k.
  * **which tools a capability tier may be given** — below ~1.5B parameters
    tool arguments come back malformed, so a light model never gets
    `edit_file` or `run_shell`.

Both are enforced in the browser. The headless CLI (`qh.py`) does not run the
browser, so without this module it would hand every model all 40 tools and
none of the gating — which is not "the same harness, no UI", it is a different
and worse harness.

**JavaScript stays the source of truth for the GUI; this file mirrors it, and
tests/test_profiles.py parses both and fails if they disagree.** A shared JSON
file was the alternative and was rejected: the browser cannot import JSON
synchronously, so it would have added a fetch to the hot path of every turn to
solve a problem a drift test solves for free.
"""

from __future__ import annotations

from typing import Any

# ── capability tiers (mirrors gui/weighbridge.js) ────────────────────────
TIERS = {"none": 0, "light": 1, "standard": 2, "strong": 3}
NONE, LIGHT, STANDARD, STRONG = 0, 1, 2, 3


def tier_for(param_billions: float | None, capabilities: list[str] | None = None) -> int:
    """Capability tier from parameter count plus Ollama's reported capabilities."""
    caps = capabilities or []
    if "tools" not in caps:
        return NONE
    if param_billions is None:
        return LIGHT          # unknown: assume the worst
    if param_billions < 1.5:
        return LIGHT
    if param_billions < 3.5:
        return STANDARD
    return STRONG


def tier_name(tier: int) -> str:
    for name, value in TIERS.items():
        if value == tier:
            return name
    return "unknown"


# ── minimum tier per tool (mirrors gui/weighbridge.js TOOL_MIN_TIER) ─────
# Anything unlisted defaults to `standard`: a new tool is assumed non-trivial
# until someone decides otherwise, which fails safe.
TOOL_MIN_TIER: dict[str, int] = {
    # Zero or one trivial argument.
    "date_time": LIGHT,
    "read_file": LIGHT,
    "list_dir": LIGHT,
    "tool_search": LIGHT,
    "web_search": LIGHT,
    "scrape_url": LIGHT,
    "memory_search": LIGHT,
    "memory_list": LIGHT,
    "sandbox_status": LIGHT,
    "github_status": LIGHT,
    "skill_read": LIGHT,

    # Structured arguments, or a mistake costs something.
    "search_files": STANDARD,
    "find_files": STANDARD,
    "write_file": STANDARD,
    "append_file": STANDARD,
    "memory_write": STANDARD,
    "fetch_data": STANDARD,
    "sandbox_verify": STANDARD,
    "create_display": STANDARD,
    "read_pdf": STANDARD,
    "read_document": STANDARD,
    "describe_image": STANDARD,

    # Exact-match edits, shell, promotion to real files, driving a browser or
    # the machine itself.
    "edit_file": STRONG,
    "run_shell": STRONG,
    "sandbox_promote": STRONG,
    "sandbox_discard": STRONG,
    "memory_forget": STRONG,
    "browser_navigate": STRONG,
    "browser_click": STRONG,
    "browser_type": STRONG,
    "browser_evaluate": STRONG,
    "click": STRONG,
    "double_click": STRONG,
}
DEFAULT_MIN_TIER = STANDARD


# ── surface profiles (mirrors gui/agent-loop.js TOOL_PROFILES) ───────────
TOOL_PROFILES: dict[str, list[str]] = {
    "code": [
        "read_file", "write_file", "edit_file", "append_file", "list_dir",
        "search_files", "find_files", "run_shell",
        "github_status", "github_list_repos", "github_list_dir", "github_get_file",
        "memory_write", "memory_search", "tool_search", "date_time",
        "sandbox_status", "sandbox_verify", "sandbox_promote",
    ],
    "project": [
        "read_file", "write_file", "edit_file", "append_file", "list_dir",
        "search_files", "find_files", "run_shell",
        "github_status", "github_list_repos", "github_list_dir", "github_get_file",
        "web_search", "scrape_url", "memory_write", "memory_search", "tool_search", "date_time",
        "sandbox_status", "sandbox_verify", "sandbox_promote",
    ],
    "home": [
        "read_file", "write_file", "edit_file", "list_dir", "search_files",
        "find_files", "run_shell", "web_search", "scrape_url", "fetch_data",
        "memory_write", "memory_search", "memory_list", "create_display", "tool_search", "date_time",
        "sandbox_status", "sandbox_verify", "sandbox_promote",
    ],
    "hub": [
        "read_file", "write_file", "edit_file", "list_dir", "search_files",
        "run_shell", "web_search", "scrape_url", "memory_write", "memory_search", "tool_search", "date_time",
        "sandbox_status", "sandbox_verify", "sandbox_promote",
    ],
    "minimal": [
        "read_file", "edit_file", "list_dir", "search_files", "run_shell",
        "tool_search", "date_time",
    ],
}


def select_tools(
    all_tools: list[dict[str, Any]],
    profile: str = "home",
    tier: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """(tools, withheld) for a profile, optionally gated by capability tier.

    Returns the withheld names too rather than silently dropping them: the GUI
    prints "N tool(s) it can't reliably drive were withheld", and a headless
    run that quietly had half its tools removed would be far harder to explain
    than one that says so.
    """
    wanted = TOOL_PROFILES.get(profile)
    if wanted is None:
        # Falling back to all 40 schemas would reintroduce the ~3k-token prompt
        # this mechanism exists to avoid, and a typo'd profile would be
        # invisible. Fail loudly, degrade to minimal — same as the JS.
        wanted = TOOL_PROFILES["minimal"]
    wanted_set = set(wanted)

    picked, withheld = [], []
    for tool in all_tools:
        name = (tool.get("function") or {}).get("name")
        if name not in wanted_set:
            continue
        if tier is not None and TOOL_MIN_TIER.get(name, DEFAULT_MIN_TIER) > tier:
            withheld.append(name)
            continue
        picked.append(tool)
    return picked, withheld
