"""
tools/__init__.py – central tool registry.

TOOL_DEFINITIONS   → list of OpenAI-format tool schemas (passed to the API)
execute_tool()     → dispatches a tool call to its implementation

Add new tools here in three steps:
  1. Implement the function in the appropriate tools/*.py file
  2. Add a JSON schema entry to TOOL_DEFINITIONS
  3. Add a case to the execute_tool() dispatcher
"""

from __future__ import annotations
import traceback
from typing import Any

# ── Tool schema definitions ───────────────────────────────────────────────────

TOOL_DEFINITIONS: list[dict] = [

    # ── Computer control ──────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": (
                "Capture a screenshot of the entire screen. "
                "Returns the file path of the saved PNG."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click the mouse at screen coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate in pixels"},
                    "y": {"type": "integer", "description": "Y coordinate in pixels"},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Mouse button (default: left)",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "double_click",
            "description": "Double-click at screen coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text at the current cursor position.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": (
                "Press a key or keyboard shortcut. "
                "Examples: 'enter', 'tab', 'escape', 'cmd+c', 'cmd+shift+t'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key or combo to press"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll up or down at screen coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "clicks": {
                        "type": "integer",
                        "description": "Number of scroll steps (default 3)",
                    },
                },
                "required": ["x", "y", "direction"],
            },
        },
    },

    # ── Browser control ───────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Open a URL in the browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to navigate to"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current browser page. Returns the file path.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element in the browser using a CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for the element",
                    },
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Clear an input field and type text into it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector"},
                    "text": {"type": "string", "description": "Text to type"},
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_text",
            "description": "Get all visible text from the current browser page.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_evaluate",
            "description": "Run JavaScript in the browser and return the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "JS expression to evaluate"},
                },
                "required": ["script"],
            },
        },
    },

    # ── File system ────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file (creates or overwrites).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run a shell command and return its stdout/stderr. "
                "Use carefully – no sudo or destructive commands."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                },
                "required": ["command"],
            },
        },
    },


    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Make a surgical edit to an existing file: replace old_string with "
                "new_string, leaving the rest untouched. PREFER THIS over write_file "
                "when changing existing code. old_string must match exactly (including "
                "indentation) and be unique unless replace_all is true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "Exact text to find (include enough context to be unique)"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Append content to the end of a file (creates it if missing).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search file contents recursively for a string or regex (like grep -rn). "
                "Returns file:line matches. Use this to locate code before editing it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "String or extended regex to search for"},
                    "path": {"type": "string", "description": "Directory to search (default: current dir)"},
                    "glob": {"type": "string", "description": "Optional filename filter, e.g. *.py"},
                    "max_results": {"type": "integer", "description": "default 30"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files by name pattern (glob, e.g. *.py or test_*) recursively.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Directory to search under (default: current dir)"},
                },
                "required": ["pattern"],
            },
        },
    },

    # ── Date and time ─────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "date_time",
            "description": (
                "Get the current date and time. Call this for ANY question about "
                "today's date, the day of the week, the current time, or how long "
                "until/since something — you have no clock of your own and your "
                "training data is stale, so never answer these from memory. "
                "Defaults to the user's own region; pass a city, country, or IANA "
                "timezone for somewhere else."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City, country, or IANA timezone (e.g. 'Tokyo', 'Europe/London'). Omit for the user's local region.",
                    },
                    "format": {"type": "string", "description": "Optional strftime pattern for an extra formatted field."},
                },
                "required": [],
            },
        },
    },

    # ── Web search / scraping ─────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the live internet and return the top results (title, url, "
                "snippet). Call this whenever the user asks you to search, google, "
                "look something up, go online, or find information about a person, "
                "company, product, event, price, or news item — and whenever you are "
                "not certain of a fact. You have real internet access through this "
                "tool: never reply that you cannot browse the web. Follow up with "
                "scrape_url on a result to read the full page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Max results to return (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scrape_url",
            "description": (
                "Read a web page and return its text. Use this on a url from "
                "web_search, or any link the user gives you. It handles "
                "JavaScript-rendered sites (Instagram, X, Reddit, most modern apps) "
                "automatically by rendering them in a headless browser, so you do "
                "not need to reach for the browser_* tools yourself. If it returns "
                "an error saying the page could not be read, report that plainly — "
                "never guess or invent what the page contained."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
                "required": ["url"],
            },
        },
    },

    # ── GitHub (read-only; connect via the GUI's Connectors panel) ──
    {
        "type": "function",
        "function": {
            "name": "github_status",
            "description": "Check whether GitHub is connected, and which account.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_list_repos",
            "description": "List repositories the connected GitHub account owns or collaborates on.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "description": "Max repos to return (default 30)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_list_dir",
            "description": "List a directory's contents in a GitHub repo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "path": {"type": "string", "description": "Directory path (empty for repo root)"},
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_get_file",
            "description": "Read a file's contents from a GitHub repo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["owner", "repo", "path"],
            },
        },
    },

    # ── Memory (persists across runs — ~/.qwen-harness/memory.json) ──
    {
        "type": "function",
        "function": {
            "name": "memory_write",
            "description": (
                "Remember something for later, across sessions — a fact, a user "
                "preference, or a correction the user made to your output. "
                "Use category='user_edit' for corrections, 'preference' for stated "
                "preferences, or 'fact' (default) for anything else."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short identifier to recall this by later"},
                    "value": {"type": "string", "description": "What to remember"},
                    "category": {"type": "string", "description": "fact | preference | user_edit | anything else (default: fact)"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_read",
            "description": "Recall a specific remembered value by its exact key.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Search everything remembered for a keyword or phrase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "description": "default 10"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_list",
            "description": "List remembered entries, optionally filtered to one category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Optional: fact | preference | user_edit | ..."},
                    "max_results": {"type": "integer", "description": "default 50"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_forget",
            "description": "Delete a remembered entry by key.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },

    # ── Sandbox (staging + verify + promote) ──────────────────────
    {
        "type": "function",
        "function": {
            "name": "sandbox_status",
            "description": (
                "Show what you have staged in this run's sandbox: the files changed, "
                "whether tests have passed, and what confinement is active. Your "
                "write_file/edit_file/append_file calls do NOT touch the user's real "
                "files — they are staged here until you verify and promote."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_verify",
            "description": (
                "Run the project's tests against your staged changes, inside the "
                "sandbox. Call this after editing and before sandbox_promote — "
                "promotion is refused until the tests pass. Pass `command` only to "
                "override the auto-detected test command."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Test command to run (optional)"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_promote",
            "description": (
                "Copy your verified staged changes into the user's real files. This "
                "is the only tool that writes outside the sandbox. Requires a passing "
                "sandbox_verify first; a .qh.bak copy is kept of anything overwritten."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_discard",
            "description": "Throw away every staged change in this run's sandbox without applying any of it.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },

    # ── Generic API access ────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "fetch_data",
            "description": (
                "Call a URL or JSON API directly (GET/POST/etc.) and return its "
                "response, parsed as JSON when possible. Use this for APIs — use "
                "scrape_url instead for HTML pages you want as readable text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string", "description": "GET, POST, etc. (default GET)"},
                    "headers": {"type": "object", "description": "Optional extra request headers"},
                    "body": {"type": "object", "description": "Optional JSON request body"},
                },
                "required": ["url"],
            },
        },
    },

    # ── Human interaction / display ───────────────────────────
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Pause and ask the human a clarifying question when you're blocked "
                "or genuinely unsure how to proceed. Terminal/CLI use only — blocks "
                "for input, so only call this when running interactively."
            ),
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_display",
            "description": (
                "Render something visual for the user — a table, small report, or "
                "formatted summary — as an HTML page, and open it. Use instead of a "
                "wall of plain text when a table or structured layout would read better."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {"type": "string", "description": "HTML fragment to render (e.g. a <table> or <div>...</div>)"},
                    "title": {"type": "string", "description": "Page title (default 'Agent display')"},
                },
                "required": ["html"],
            },
        },
    },

    # ── Tool discovery (for when the full list above gets long) ──
    {
        "type": "function",
        "function": {
            "name": "tool_search",
            "description": (
                "Search for relevant tools by keyword instead of scanning the full "
                "tool list — useful once many tools are available. Returns the best-"
                "matching tool names and descriptions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What you're trying to do"},
                    "max_results": {"type": "integer", "description": "default 5"},
                },
                "required": ["query"],
            },
        },
    },
]


# ── Tool discovery ─────────────────────────────────────────────────────────
# Implemented here (not tools/discovery.py) since it needs TOOL_DEFINITIONS
# in scope and tools/*.py modules don't import back from tools/__init__.py.

def _tool_search(query: str, max_results: int = 5) -> dict:
    words = [w for w in query.lower().split() if w]
    scored = []
    for entry in TOOL_DEFINITIONS:
        fn = entry["function"]
        if fn["name"] == "tool_search":
            continue
        haystack = f"{fn['name']} {fn['description']}".lower()
        score = sum(haystack.count(w) for w in words)
        if score > 0:
            scored.append((score, fn["name"], fn["description"]))
    scored.sort(key=lambda t: t[0], reverse=True)
    return {
        "status": "ok",
        "query": query,
        "results": [{"name": n, "description": d} for _, n, d in scored[:max_results]],
    }


# ── Tool dispatcher ───────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict[str, Any]) -> Any:
    """Route a tool call to its implementation and return the result."""
    try:
        # Computer tools
        if name == "screenshot":
            from tools.computer import screenshot
            return screenshot()
        elif name == "click":
            from tools.computer import click
            return click(**args)
        elif name == "double_click":
            from tools.computer import double_click
            return double_click(**args)
        elif name == "type_text":
            from tools.computer import type_text
            return type_text(**args)
        elif name == "press_key":
            from tools.computer import press_key
            return press_key(**args)
        elif name == "scroll":
            from tools.computer import scroll
            return scroll(**args)

        # Browser tools
        elif name == "browser_navigate":
            from tools.browser import navigate
            return navigate(**args)
        elif name == "browser_screenshot":
            from tools.browser import browser_screenshot
            return browser_screenshot()
        elif name == "browser_click":
            from tools.browser import click_element
            return click_element(**args)
        elif name == "browser_type":
            from tools.browser import type_in_element
            return type_in_element(**args)
        elif name == "browser_get_text":
            from tools.browser import get_page_text
            return get_page_text()
        elif name == "browser_evaluate":
            from tools.browser import evaluate_js
            return evaluate_js(**args)

        # File tools
        elif name == "read_file":
            from tools.files import read_file
            return read_file(**args)
        elif name == "write_file":
            from tools.files import write_file
            return write_file(**args)
        elif name == "list_dir":
            from tools.files import list_dir
            return list_dir(**args)
        elif name == "run_shell":
            from tools.files import run_shell
            return run_shell(**args)
        elif name == "edit_file":
            from tools.files import edit_file
            return edit_file(**args)
        elif name == "append_file":
            from tools.files import append_file
            return append_file(**args)
        elif name == "search_files":
            from tools.files import search_files
            return search_files(**args)
        elif name == "find_files":
            from tools.files import find_files
            return find_files(**args)

        # Web tools
        elif name == "date_time":
            from tools.datetime_tool import get_datetime
            return get_datetime(**args)
        elif name == "web_search":
            from tools.web import web_search
            return web_search(**args)
        elif name == "scrape_url":
            from tools.web import scrape_url
            return scrape_url(**args)

        # GitHub tools
        elif name == "github_status":
            from tools.github import gh_status
            return gh_status()
        elif name == "github_list_repos":
            from tools.github import gh_list_repos
            return gh_list_repos(**args)
        elif name == "github_list_dir":
            from tools.github import gh_list_dir
            return gh_list_dir(**args)
        elif name == "github_get_file":
            from tools.github import gh_get_file
            return gh_get_file(**args)

        # Sandbox tools. agent_id matters: sandboxes are keyed on (run, agent)
        # so parallel agents cannot overwrite each other's staged work, and a
        # status/verify/promote without it would act on the wrong tree.
        elif name == "sandbox_status":
            from tools.sandbox import get_sandbox, sandboxes_for_run
            sandbox = get_sandbox(args.get("run_id", ""), args.get("cwd"),
                                  agent_id=args.get("agent_id"), create=False)
            if sandbox:
                summary = sandbox.summary()
                # Name the siblings, so an agent can see it is not working
                # alone without being able to reach into their trees.
                others = [box.agent_id for box in sandboxes_for_run(args.get("run_id", ""))
                          if box.agent_id != sandbox.agent_id]
                if others:
                    summary["other_agents_in_run"] = others
                return summary
            return {"error": "no sandbox for this run"}
        elif name == "sandbox_verify":
            from tools.sandbox import get_sandbox
            sandbox = get_sandbox(args.get("run_id", ""), args.get("cwd"),
                                  agent_id=args.get("agent_id"), create=False)
            if not sandbox:
                return {"error": "no sandbox for this run — nothing staged yet"}
            return sandbox.verify(args.get("command"))
        elif name == "sandbox_promote":
            from tools.sandbox import get_sandbox
            sandbox = get_sandbox(args.get("run_id", ""), args.get("cwd"),
                                  agent_id=args.get("agent_id"), create=False)
            if not sandbox:
                return {"error": "no sandbox for this run — nothing staged to promote"}
            return sandbox.promote(force=bool(args.get("force")))
        elif name == "sandbox_discard":
            from tools.sandbox import get_sandbox
            sandbox = get_sandbox(args.get("run_id", ""), args.get("cwd"),
                                  agent_id=args.get("agent_id"), create=False)
            return sandbox.discard() if sandbox else {"status": "ok", "discarded": 0}
        elif name == "fetch_data":
            from tools.web import fetch_data
            return fetch_data(**args)

        # Memory tools
        elif name == "memory_write":
            from tools.memory import memory_write
            return memory_write(**args)
        elif name == "memory_read":
            from tools.memory import memory_read
            return memory_read(**args)
        elif name == "memory_search":
            from tools.memory import memory_search
            return memory_search(**args)
        elif name == "memory_list":
            from tools.memory import memory_list
            return memory_list(**args)
        elif name == "memory_forget":
            from tools.memory import memory_forget
            return memory_forget(**args)

        # Human interaction / display
        elif name == "ask_user":
            from tools.interaction import ask_user
            return ask_user(**args)
        elif name == "create_display":
            from tools.interaction import create_display
            return create_display(**args)

        # Tool discovery
        elif name == "tool_search":
            return _tool_search(**args)

        else:
            return {"error": f"Unknown tool: {name}"}

    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}
