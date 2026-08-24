# qwen-harness — command runbook

Every command needed to set up, run, and manage this harness, in order.
Run these yourself in a terminal — nothing here downloads or starts anything automatically.

## 1. One-time setup (installs Ollama, Python deps, Playwright)

```bash
cd /Applications/qwen-harness
./setup.sh
```

This installs Ollama (if missing), starts `ollama serve`, and pulls the light model
roster below. You can skip it and run the pulls manually instead — see §2.

## 2. Pull the light model roster (8 GB RAM friendly)

One primary model plus four very light models for sub-agents that just route,
extract, or do narrow tool-calling in `orchestrator.py`.

```bash
ollama pull qwen3:4b      # ~2.5 GB — primary model
ollama pull qwen3:0.6b    # ~0.5 GB — lightest agent
ollama pull gemma3:1b     # ~0.8 GB — light agent
ollama pull llama3.2:1b   # ~1.3 GB — light agent
ollama pull gemma2:2b     # ~1.6 GB — light agent
```

Watch progress live — each command prints a download bar and blocks until done.

## 3. Remove models you no longer need

```bash
ollama list                # see what's actually on disk
ollama rm qwen3:8b         # drop a model you've replaced
du -sh ~/.ollama/models    # check total disk usage
```

`ollama rm` garbage-collects the blobs that model was using — no manual blob
cleanup needed. An interrupted `ollama pull` just resumes (or restarts) next
time you pull that same model, so there's nothing to "clean up" from a failed pull.

## 4. Start the harness

```bash
cd /Applications/qwen-harness
./wake-up.sh
```

This starts `ollama serve` (if not already running), starts the GUI backend
(`gui/server.py`) on port 8420, and opens `http://127.0.0.1:8420/gui/` in
your browser.

Pass a different port if 8420 is taken: `./wake-up.sh 9000`.

### Start as a desktop app

```bash
cd /Applications/qwen-harness
./script/build_and_run.sh
```

This builds a native macOS wrapper at `dist/Qwen Harness.app`, starts/reuses
Ollama, starts/reuses the same GUI backend, and opens the existing Control Room
inside a desktop window. The browser flow above still works unchanged.

To build, launch, and confirm the app process started:

```bash
./script/build_and_run.sh --verify
```

### Starting pieces manually instead

```bash
# Terminal 1 — model server
ollama serve

# Terminal 2 — GUI backend + static file server (also serves the API the
# Code page's filesystem browser, terminal, and file editor depend on)
cd /Applications/qwen-harness
python3 gui/server.py 8420

# then open http://127.0.0.1:8420/gui/ in a browser
```

## 5. Check everything is actually up

```bash
curl -s http://127.0.0.1:8420/api/health   # GUI backend
curl -s http://localhost:11434/api/tags    # Ollama + which models are pulled
```

The Control Room's topbar shows the same two checks live (Server / Ollama /
active-model status pills), polled automatically every few seconds.

## 6. Run the raw Python examples (no GUI)

```bash
cd /Applications/qwen-harness
python examples/01_hello.py
python examples/02_with_tools.py
python examples/03_multi_agent.py
python examples/04_computer_use.py
python examples/05_swap_models.py
```

## 7. Swap the active model

One line in [config.py](config.py):

```python
ACTIVE_MODEL: str = "qwen3:4b"   # change to any key in MODELS
```

Or per-agent, in the GUI's Agent Hub page (each agent has its own Model
dropdown), or at runtime in Python: `agent.swap_model("gemma3:1b")`.

## 8. Stop everything

```bash
pkill -f "gui/server.py"
pkill -x ollama
```
