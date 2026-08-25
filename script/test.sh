#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

node --check gui/app.js
node --check gui/agent-loop.js
node --check gui/capabilities.js
node --check gui/chats.js
node --check gui/effort.js
node --check gui/chats.js
node --check gui/runs.js
node --check gui/filetree.js
node --check gui/build.cjs
# Generated avatar modules had no syntax coverage at all — a bad splice in
# build.cjs would have shipped silently.
node --check gui/engine/avatar-shared.js
node --check gui/engine/avatars/strobi.avatar.js
node --input-type=module -e "import('./gui/engine/avatar-shared.js').then(m => { if (typeof m.createAvatarFactory !== 'function') { console.error('avatar-shared.js does not export createAvatarFactory'); process.exit(1); } })"
node --input-type=module -e "import { inferEffort, parseEffortEscalation } from './gui/effort.js'; if (inferEffort('hello') !== 1) process.exit(1); if (inferEffort('Implement a production database migration, update files, add tests, then audit security and deploy. '.repeat(18)) !== 4) process.exit(1); const request=parseEffortEscalation('[[REQUEST_MAX_EFFORT: verify risky migration]]'); if (request?.level !== 4 || request.reason !== 'verify risky migration') process.exit(1);"
python3 -m py_compile gui/server.py agent.py orchestrator.py tools/*.py script/render_app_icon.py
python3 -m unittest discover -s tests -v
git diff --check
