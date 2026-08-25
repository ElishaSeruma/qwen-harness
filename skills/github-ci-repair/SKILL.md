---
name: github-ci-repair
description: Diagnose failing GitHub Actions checks and prepare a safe, verified repair.
source: OpenAI Skills repository (reviewed adaptation from gh-fix-ci)
tools: github_status,run_shell,read_file,edit_file
---
Inspect check output before proposing a change. Explain the failure, request approval before edits, then run the closest local verification possible.
