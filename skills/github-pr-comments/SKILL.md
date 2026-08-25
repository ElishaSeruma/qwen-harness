---
name: github-pr-comments
description: Address review comments on the current GitHub pull request with evidence and user approval for changes.
source: OpenAI Skills repository (reviewed adaptation from gh-address-comments)
tools: github_status,run_shell,read_file,edit_file
---
Verify GitHub authentication and the current branch. Summarize comments, propose a plan, and make code changes only after approval. Verify each accepted fix.
