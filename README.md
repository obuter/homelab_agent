# Homelab Agent CLI — v1

A terminal-based homelab assistant powered by a local Ollama model.
Every message goes to a tool-enabled agent that can SSH into your hosts,
query APIs, search the web, and log its actions to a local SQLite database.

---

## Requirements

```
pip install requests ddgs prompt_toolkit paramiko python-dotenv
```

---

## Files

| File | Purpose |
|------|---------|
| `homelab_agent.py` | Main CLI — run this |
| `tools.py` | Agent tool implementations (imported automatically) |
| `homelab.db` | SQLite database — created on first run |
| `.env` | Credentials — SSH, OPNsense API, Home Assistant |
| `.ollama_history` | Prompt history (prompt_toolkit) |
| `skill/` | Directory for `.md` skill files |

---

## Quick Start

```bash
python homelab_agent.py
```

Type anything — it goes straight to the agent. No prefix needed.

```
ollama_cli ▸ is caddy running on debian
ollama_cli ▸ check if crowdsec has any active bans
ollama_cli ▸ show me disk usage on all hosts
```

---

## Configuration

Edit the `CONFIG` section at the top of `homelab_agent.py`:

```python
HOST  = "http://localhost:11434"   # Ollama server address
MODEL = "qwen3:8b"                 # Model to use
```

Tune agent behaviour in `OPTIONS` (temperature, context size, etc.) and
`CHAT_OPTIONS` (used for non-agent tasks like /diff and /compact).

---

## Credentials — `.env`

Create a `.env` file next to `homelab_agent.py`:

```env
SSH_USER=your_user
SSH_PASS=your_password

SSH_HOST_DEBIAN=0.0.0.0
SSH_HOST_PBS=0.0.0.0
SSH_HOST_HAOS=0.0.0.0
SSH_HOST_OPNSENSE=0.0.0.0

OPNSENSE_HOST=0.0.0.0
OPNSENSE_KEY=your_key
OPNSENSE_SECRET=your_secret

HA_HOST=0.0.0.0
HA_TOKEN=your_token

BROWSERLESS_URL=http://localhost:3000
```

Per-host credential overrides: `SSH_USER_PBS`, `SSH_PASS_PBS`, etc.

---

## Agent Tools

The agent picks tools automatically based on what the task requires.

| Tool | What it does |
|------|-------------|
| `ssh_exec` | Run commands on debian, pbs, haos, opnsense |
| `shell_exec` | Run commands on the local machine |
| `read_file` | Read a local file |
| `write_file` | Write a local file (requires confirmation) |
| `http_get` | Call local/private APIs (Home Assistant, Proxmox, Docker Hub) |
| `browse_url` | Fetch a public web page (Browserless → direct HTTP fallback) |
| `web_search` | DuckDuckGo search |
| `opnsense` | OPNsense REST API |
| `query_db` | Read-only SELECT against `homelab.db` |
| `log_action` | Write a changelog entry after significant actions |

**Safety:** destructive commands (`rm -rf`, `shutdown`, `mkfs`, etc.) are
blocked outright. Commands that mutate state (`docker restart`, `systemctl`,
`apt`, etc.) require your confirmation before running.

---

## Commands

| Command | Description |
|---------|-------------|
| `<anything>` | Send to agent (tool-enabled) |
| `/model [name]` | Show or switch model |
| `/models` | List models available on Ollama |
| `/unload` | Unload current model from VRAM |
| `/think [on\|off]` | Show or hide `<think>` reasoning blocks |
| `/history` | Show conversation and pinned files |
| `/compact` | Summarise history to free up context |
| `/clear` | Wipe conversation and unpin all files |
| `/save [name]` | Save session to database |
| `/sessions` | List saved sessions |
| `/load [id\|name]` | Restore a session |
| `/delete [id\|name]` | Delete a session |
| `/log [N]` | Show last N changelog entries (default 20) |
| `/log agent [N]` | Show agent tool call log |
| `/search <query>` | Web search → answer (no tool loop) |
| `/fetch <url> [prompt]` | Fetch a URL → answer (no tool loop) |
| `/read <file> [prompt]` | Pin a local file into context; `'.'` = silent pin |
| `/unread <file>` | Unpin a file |
| `/files` | List pinned files |
| `/run <cmd> [-- prompt]` | Run a local command and send output to model |
| `/diff <file> [instr]` | Ask agent to edit a file, review diff, apply |
| `/skill [name]` | List skills or load one as system prompt |
| `/system [text\|default]` | Show, set, or reset the system prompt |
| `/quit` | Exit |

### Notes on specific commands

**`/run`** — handles `cd` transparently:
```
/run cd /opt/docker
/run ls -- what containers are here
```

**`/read`** — pins a file into every subsequent request until `/clear` or `/unread`:
```
/read /opt/docker/caddy/Caddyfile .          # silent pin
/read tools.py what tools does the agent have
```

**`/diff`** — sends file to model, shows a unified diff, lets you apply/edit/discard:
```
/diff docker-compose.yml add a healthcheck to the caddy service
```

**`/skill`** — loads a `.md` file from the `skill/` directory as the system prompt.
Useful for switching the agent's focus to a specific domain.

---

## Dynamic Skill Routing

The system uses `.md` files in the `skill/` directory to build context-specific system prompts.

* **Auto-Routing:** Automatically detects keywords (e.g., "docker", "firewall") to inject relevant skills into the turn.
* **Manual Lock:** Use `/skill <name>` to pin specific homelab skills or load a Persona (e.g., `coder.md`) that replaces the default system logic.
* **Skill List:** `/skill` displays all available skill files and current routing state.

---

## Pipe Mode

Pass input from stdin for one-shot queries:

```bash
echo "what does this error mean" | python ollama_cli.py
cat /var/log/syslog | python ollama_cli.py "any suspicious entries?"
```

Uses the lightweight chat path — no tool calls.

---

## Database

`homelab.db` stores three tables:

| Table | Contents |
|-------|---------|
| `sessions` | Saved conversation history |
| `agent_log` | Every tool call with result and OK/fail status |
| `changelog` | Human-readable log written by the agent via `log_action()` |

View recent agent activity:
```
/log 20
/log agent 50
```

The agent writes to `changelog` automatically after significant actions.
Categories: `action`, `finding`, `decision`, `error`.

---

## Token Usage

The context bar shown after each response:

```
████░░░░░░░░░░░░░░░░ 7,200/32,768 tokens (~22%)
```

- Each agent tool round costs roughly 500–2,000 tokens
- Use `/compact` when the bar goes above ~70% to summarise history
- Use `/clear` to start fresh
- `num_ctx` can be changed in `OPTIONS` at the top of `ollama_cli.py`
