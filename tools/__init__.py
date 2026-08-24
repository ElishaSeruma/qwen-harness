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

    # ── Web search / scraping ─────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web and return the top results (title, url, snippet).",
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
            "description": "Fetch a URL and return its readable text content (HTML stripped).",
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
]


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

        # Web tools
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

        else:
            return {"error": f"Unknown tool: {name}"}

    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}
