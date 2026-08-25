---
name: test-and-verify
description: Verify a change with focused tests, static checks, and clear evidence of what passed or failed.
source: OpenAI Skills repository (reviewed adaptation)
tools: read_file,search_files,run_shell
---
Identify the narrowest relevant checks, run them where safe, and report commands, results, and any coverage gap. Do not represent unrun checks as passed.
