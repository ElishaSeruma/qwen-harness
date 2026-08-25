---
name: code-review
description: Review changed code for correctness, regressions, maintainability, and missing tests.
source: OpenAI Skills repository (reviewed adaptation)
tools: read_file,search_files,run_shell
---
Inspect the relevant code and tests first. Report findings by impact with file and line evidence. Do not change files unless the user asks for a fix.
