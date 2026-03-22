"""
ollama_cli.py — Homelab Agent CLI  v1
Edit CONFIG, then: python ollama_cli.py

Requires: pip install requests ddgs prompt_toolkit paramiko python-dotenv

Commands:
  /help /model /models /unload /think /history /compact /clear
  /save /load /sessions /delete /log /search /fetch
  /read /unread /files /run /diff /skill /system /quit

Pipe mode: echo "prompt" | python ollama_cli.py [extra text]
           cat file.log | python ollama_cli.py "what is wrong here?"

All normal input goes to the agent (tool-enabled).
Skills are auto-detected from each message and injected into context.
/search, /fetch, /compact, /diff use a lighter non-tool chat call.
"""

import json
import sqlite3
import datetime
import difflib
import re
import time
import sys
import os
import subprocess
import tempfile
from pathlib import Path

import requests
from ddgs import DDGS
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML

from tools import tool_browse_url as _fetch_page
from tools import BLOCKED_CMDS as RUN_BLOCKED, CONFIRM_CMDS as RUN_CONFIRM

# ═══════════════════════════════════════════════════════════
#  CONFIG — edit here
# ═══════════════════════════════════════════════════════════
HOST  = "http://localhost:11434"
MODEL = "qwen3:8b"

# Fallback system prompt used if skill/core.md is missing
CORE_FALLBACK = (
    "You are an autonomous homelab assistant. "
    "Use ssh_exec() for all remote hosts. Be concise. "
    "Log significant actions with log_action()."
)

# ── Skill auto-routing ──────────────────────────────────────
# Maps route key → (trigger keywords, skill filename stem)
# A message matching keywords from multiple routes injects all matched skills.
# 'crowdsec' intentionally appears in both docker and opnsense — correct behaviour,
# since CrowdSec spans both hosts and both skill files are needed for full context.
SKILL_ROUTES: dict[str, tuple[list[str], str]] = {
    "docker": (
        ["docker", "caddy", "container", "compose", "image", "volume",
         "/opt/docker", "caddyfile", "dockerfile", "crowdsec"],
        "skill_docker",
    ),
    "opnsense": (
        ["opnsense", "pfctl", "firewall", "crowdsec", "vtnet", "cscli",
         "bouncer", "blocklist", "filter.log", "pf table", "pf rule",
         "crowdsec_blocklists", "acquis"],
        "skill_opnsense",
    ),
    "proxmox": (
        ["pbs", "proxmox", "backup", "datastore", "restore", "snapshot",
         "proxmox-backup"],
        "skill_proxmox",
    ),
    "haos": (
        ["haos", "home assistant", "ha core", "automation", "supervisor",
         "homeassistant", "ha addon", "ha os"],
        "skill_haos",
    ),
}

# Route keys that belong to the homelab routing system (vs persona skills)
_ROUTED_SKILL_NAMES = set(SKILL_ROUTES.keys())

# Agent model parameters — precise, deterministic
OPTIONS = {
    "temperature":   0.0,
    "num_ctx":       32768,
    "num_predict":   4096,
    "repeat_last_n": 256,
    "min_p":         0.02,
    "top_p":         0.85,
    "top_k":         20,
}

# Slightly warmer — used for /compact, /diff, /search, /fetch, /read prompts
CHAT_OPTIONS = {**OPTIONS, "temperature": 0.2, "top_k": 40, "min_p": 0.05}

# ─── tuneables ──────────────────────────────────────────────
SKILLS_DIR      = Path("skill")
MAX_HISTORY     = 8
KEEP_ALIVE      = "10m"
REQUEST_TIMEOUT = 120.0
RETRY_COUNT     = 3
RETRY_DELAY     = 1.5
MAX_TOOL_ROUNDS = 6
READ_MAX_CHARS  = 12_000
RUN_MAX_CHARS   = 6_000
SEARCH_RESULTS  = 4
SEARCH_FETCH    = 2
SEARCH_CHARS    = 2_500
AGENT_LOG_KEEP  = 500
CHANGELOG_KEEP  = 200
EDITOR          = os.environ.get("EDITOR", "nano")

# ─── persistence ────────────────────────────────────────────
DB_FILE      = Path("homelab.db")
HISTORY_FILE = Path(".ollama_history")

# ─── terminal colours ───────────────────────────────────────
CYAN    = "\033[96m"
YELLOW  = "\033[93m"
GREEN   = "\033[92m"
RED     = "\033[91m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
DIM     = "\033[2m"
RESET   = "\033[0m"
BOLD    = "\033[1m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*[mABCDEFGHJKSTfnsulhp]")

TEXT_EXTS = {
    ".txt", ".md", ".py", ".sh", ".bash", ".zsh", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".json", ".env", ".log",
    ".js", ".ts", ".html", ".css", ".xml", ".csv", ".dockerfile",
    "dockerfile", ".service", ".timer",
}

SEARCH_PROMPT = """\
Today is {today}. You have been given live web search results below.
Use them to answer accurately and concisely. Cite the URL when referencing a result.

--- SEARCH RESULTS ---
{results}
--- END RESULTS ---"""


# ═══════════════════════════════════════════════════════════
#  SKILL ROUTING
# ═══════════════════════════════════════════════════════════
def detect_skills(text: str) -> list[str]:
    """
    Return list of route keys whose keywords match the user's message.
    May return multiple keys for cross-domain queries — e.g. a crowdsec
    question matches both 'docker' and 'opnsense', injecting both skill files.
    """
    lower = text.lower()
    return [
        key for key, (keywords, _) in SKILL_ROUTES.items()
        if any(k in lower for k in keywords)
    ]


def build_system_prompt(skill_keys: list[str]) -> str:
    """
    Concatenate core.md with any requested skill files.
    Falls back to CORE_FALLBACK if core.md is missing.
    """
    parts: list[str] = []
    label, core = load_skill("core")
    parts.append(core if label is not None else CORE_FALLBACK)

    for key in skill_keys:
        _, filename = SKILL_ROUTES[key]
        slabel, content = load_skill(filename)
        if slabel is not None:
            parts.append(content)

    return "\n\n".join(parts)


def skill_token_estimate(skill_keys: list[str]) -> int:
    """Rough token estimate for the assembled system prompt."""
    return len(build_system_prompt(skill_keys)) // 4


def _skill_prompt_tag(
    persona_system: "str | None",
    manual_skills:  "list[str] | None",
    last_auto_skills: list[str],
) -> str:
    """Return a prompt_toolkit HTML fragment showing active skill state."""
    if persona_system is not None:
        return " <ansimagenta>[persona]</ansimagenta>"
    if manual_skills is not None:
        if manual_skills:
            return f" <ansicyan>[{'|'.join(manual_skills)}*]</ansicyan>"
        return ""
    if last_auto_skills:
        return f" <ansiblue>[{'|'.join(last_auto_skills)}]</ansiblue>"
    return ""


# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════
def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def today() -> str:
    return datetime.date.today().isoformat()


def ctx_bar(used: int) -> str:
    total  = OPTIONS.get("num_ctx", 32768)
    pct    = min(used / total, 1.0)
    filled = int(pct * 20)
    color  = GREEN if pct < 0.6 else (YELLOW if pct < 0.85 else RED)
    bar    = color + "█" * filled + DIM + "░" * (20 - filled) + RESET
    return f"{bar} {color}{used:,}{RESET}{DIM}/{total:,} tokens (~{pct:.0%}){RESET}"


def trim_history(messages: list, max_turns: int = MAX_HISTORY) -> list:
    sys_msgs = [m for m in messages if m["role"] == "system"]
    turns    = [m for m in messages if m["role"] != "system"]
    keep     = max_turns * 2
    return sys_msgs + turns[-keep:] if len(turns) > keep else list(messages)


def build_file_ctx(loaded_files: dict) -> list:
    if not loaded_files:
        return []
    blocks  = [f"File: {k}\n```\n{v}\n```" for k, v in loaded_files.items()]
    content = "The following files are loaded and available for reference:\n\n" + \
              "\n\n---\n\n".join(blocks)
    return [{"role": "system", "content": content}]


def apply_system(messages: list, text: str) -> list:
    filtered = [m for m in messages if m["role"] != "system"]
    return ([{"role": "system", "content": text}] + filtered) if text else filtered


# ═══════════════════════════════════════════════════════════
#  SQLITE
# ═══════════════════════════════════════════════════════════
def prune_db(conn: sqlite3.Connection):
    deleted = 0
    for table, limit in (("agent_log", AGENT_LOG_KEEP), ("changelog", CHANGELOG_KEEP)):
        cur = conn.execute(f"""
            DELETE FROM {table} WHERE id NOT IN (
                SELECT id FROM {table} ORDER BY id DESC LIMIT ?
            )
        """, (limit,))
        deleted += cur.rowcount
    conn.commit()
    if deleted > 0:
        conn.execute("VACUUM")


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, model TEXT NOT NULL,
        updated TEXT NOT NULL, messages TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, session TEXT, tool TEXT NOT NULL,
        host TEXT, command TEXT, result_head TEXT, ok INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS changelog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, session TEXT, category TEXT,
        summary TEXT NOT NULL, detail TEXT, host TEXT
    )""")
    conn.commit()
    prune_db(conn)
    return conn


def _db_resolve(conn: sqlite3.Connection, ident: str, cols: str):
    try:
        return conn.execute(f"SELECT {cols} FROM sessions WHERE id=?",
                            (int(ident),)).fetchone()
    except ValueError:
        return conn.execute(f"SELECT {cols} FROM sessions WHERE name=?",
                            (ident,)).fetchone()


def db_save(conn: sqlite3.Connection, name: str, model: str, messages: list):
    now  = datetime.datetime.now().isoformat(timespec="seconds")
    blob = json.dumps(messages)
    row  = conn.execute("SELECT id FROM sessions WHERE name=?", (name,)).fetchone()
    if row:
        conn.execute("UPDATE sessions SET model=?,updated=?,messages=? WHERE id=?",
                     (model, now, blob, row[0]))
    else:
        conn.execute("INSERT INTO sessions (name,model,updated,messages) VALUES (?,?,?,?)",
                     (name, model, now, blob))
    conn.commit()


def db_list(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT id,name,model,updated,messages FROM sessions ORDER BY updated DESC"
    ).fetchall()


def db_load(conn: sqlite3.Connection, ident: str):
    row = _db_resolve(conn, ident, "name,model,messages")
    return (row[0], row[1], json.loads(row[2])) if row else None


def db_delete(conn: sqlite3.Connection, ident: str):
    row = _db_resolve(conn, ident, "id,name")
    if not row:
        return None
    conn.execute("DELETE FROM sessions WHERE id=?", (row[0],))
    conn.commit()
    return row[1]


# ═══════════════════════════════════════════════════════════
#  WEB SEARCH  (for /search command)
# ═══════════════════════════════════════════════════════════
def web_search(query: str) -> str:
    try:
        with DDGS() as ddg:
            results = ddg.text(f"{query} {today()}", max_results=SEARCH_RESULTS)
        if not results:
            return "No search results found."
    except Exception as e:
        return f"Search error: {e}"

    blocks = []
    for i, r in enumerate(results):
        entry = f"[{r['title']}]\nSnippet: {r['body']}\nURL: {r['href']}"
        if i < SEARCH_FETCH:
            print(f"{DIM}  fetching {r['href'][:70]}…{RESET}")
            page  = _fetch_page(r["href"])
            entry += f"\n\nPage content:\n{page[:SEARCH_CHARS]}"
        blocks.append(entry)
    return "\n\n---\n\n".join(blocks)


def inject_search(query: str, messages: list) -> list:
    results = web_search(query)
    ctx = {"role": "system",
           "content": SEARCH_PROMPT.format(today=today(), results=results)}
    return [ctx] + messages


# ═══════════════════════════════════════════════════════════
#  FILE READ  (local, for /read command)
# ═══════════════════════════════════════════════════════════
def read_file(path_str: str) -> tuple:
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        return None, f"File not found: {path}"
    if not path.is_file():
        return None, f"Not a file: {path}"

    label = str(path)
    size  = path.stat().st_size
    ext   = path.suffix.lower()

    if ext not in TEXT_EXTS and path.name.lower() not in TEXT_EXTS:
        return label, (f"[binary file, {size} bytes — first 512 bytes as hex]\n"
                       + path.read_bytes()[:512].hex(" ", 1))
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return label, f"[read error: {e}]"

    if len(content) > READ_MAX_CHARS:
        content = (content[:READ_MAX_CHARS]
                   + f"\n[truncated at {READ_MAX_CHARS} chars — {size} bytes total]")
    return label, content


# ═══════════════════════════════════════════════════════════
#  SHELL RUN  (local, for /run command)
# ═══════════════════════════════════════════════════════════
def run_command(cmd: str, cwd: Path) -> tuple:
    lower = cmd.lower().strip()
    for blocked in RUN_BLOCKED:
        if blocked in lower:
            raise ValueError(f"Blocked: '{blocked}' not allowed in /run")
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=str(cwd)
    )
    output = result.stdout + result.stderr
    if not output.strip():
        output = f"[exit {result.returncode}, no output]"
    elif len(output) > RUN_MAX_CHARS:
        output = output[:RUN_MAX_CHARS] + "\n[truncated]"
    return cmd, output


def needs_confirm(cmd: str) -> bool:
    return any(p in cmd.lower() for p in RUN_CONFIRM)


def resolve_cd(cmd: str, cwd: Path):
    stripped = cmd.strip()
    if not re.match(r"^cd(\s+\S+)?$", stripped):
        return None, None
    parts  = stripped.split(maxsplit=1)
    target = parts[1] if len(parts) > 1 else str(Path.home())
    new    = Path(os.path.expanduser(target))
    new    = (cwd / new).resolve() if not new.is_absolute() else new.resolve()
    if not new.is_dir():
        return None, f"cd: {target}: No such directory"
    return new, None


# ═══════════════════════════════════════════════════════════
#  DIFF / EDIT
# ═══════════════════════════════════════════════════════════
def extract_code_block(text: str):
    m = re.search(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", text, re.DOTALL | re.MULTILINE)
    if m:
        return m.group(1)
    stripped = text.strip()
    if stripped.startswith("#!/") or re.match(r"^[A-Z_]+=", stripped):
        return stripped
    return None


def diff_lines(original: str, proposed: str) -> str:
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile="original", tofile="proposed", n=3,
    ))


# ═══════════════════════════════════════════════════════════
#  SKILLS
# ═══════════════════════════════════════════════════════════
def list_skills() -> list:
    return sorted(SKILLS_DIR.glob("*.md")) if SKILLS_DIR.is_dir() else []


def load_skill(name: str) -> tuple:
    if not SKILLS_DIR.is_dir():
        return None, f"Skill directory not found: {SKILLS_DIR.resolve()}"
    stem  = name.removesuffix(".md")
    path  = SKILLS_DIR / f"{stem}.md"
    if not path.exists():
        matches = [p for p in SKILLS_DIR.glob("*.md") if p.stem.startswith(stem)]
        if len(matches) == 1:
            path = matches[0]
        elif len(matches) > 1:
            return None, f"Ambiguous '{stem}': {', '.join(p.stem for p in matches)}"
        else:
            return None, f"Skill not found: '{stem}'  (looked in {SKILLS_DIR.resolve()})"
    try:
        return path.stem, path.read_text(encoding="utf-8").strip()
    except Exception as e:
        return None, f"Could not read {path}: {e}"


# ═══════════════════════════════════════════════════════════
#  CHAT  (internal — /compact, /diff, /search, /fetch, /read, /run)
#  Not the primary interaction path — that is agent_loop().
# ═══════════════════════════════════════════════════════════
def chat(model: str, messages: list, show_think: bool,
         loaded_files: dict = None, session_tokens: list = None,
         options: dict = None) -> str:
    final = build_file_ctx(loaded_files or {}) + trim_history(messages)
    payload = {
        "model":      model,
        "messages":   final,
        "stream":     True,
        "keep_alive": KEEP_ALIVE,
        "options":    options or CHAT_OPTIONS,
    }

    full       = ""
    in_think   = False
    think_done = False
    usage      = {}

    for attempt in range(RETRY_COUNT):
        try:
            resp = requests.post(
                f"{HOST}/api/chat", json=payload, stream=True, timeout=REQUEST_TIMEOUT
            )
            if resp.status_code != 200:
                print(f"{RED}Error: HTTP {resp.status_code}{RESET}")
                return ""

            print(f"\n{CYAN}{BOLD}[{model}]{RESET}\n")

            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                chunk = (data.get("message") or {}).get("content", "")
                if chunk:
                    full += chunk
                    if show_think:
                        print(chunk, end="", flush=True)
                    else:
                        remaining = chunk
                        while remaining:
                            if not in_think and not think_done:
                                if "<think>" in remaining:
                                    before, _, remaining = remaining.partition("<think>")
                                    if before:
                                        print(before, end="", flush=True)
                                    in_think = True
                                else:
                                    print(remaining, end="", flush=True)
                                    break
                            elif in_think:
                                if "</think>" in remaining:
                                    _, _, remaining = remaining.partition("</think>")
                                    in_think   = False
                                    think_done = True
                                else:
                                    break
                            else:
                                print(remaining, end="", flush=True)
                                break

                if data.get("done"):
                    usage = data

            print()
            if usage:
                p          = usage.get("prompt_eval_count", 0)
                c          = usage.get("eval_count", 0)
                t          = usage.get("total_duration", 0) / 1e9
                call_total = p + c
                if session_tokens is not None:
                    session_tokens[0] += call_total
                sess = session_tokens[0] if session_tokens is not None else call_total
                print(f"\n{DIM}call: {YELLOW}{p}{DIM}+{GREEN}{c}{DIM}={BOLD}{call_total}{RESET}"
                      f"  {DIM}({t:.1f}s){RESET}")
                print(f"{ctx_bar(sess)}\n")
            return full

        except KeyboardInterrupt:
            print(f"\n{DIM}[interrupted]{RESET}\n")
            return full
        except requests.exceptions.ConnectionError:
            if attempt < RETRY_COUNT - 1:
                print(f"{YELLOW}Connection failed, retrying ({attempt+1}/{RETRY_COUNT})…{RESET}")
                time.sleep(RETRY_DELAY)
            else:
                print(f"{RED}Cannot connect to {HOST}{RESET}")
        except requests.exceptions.Timeout:
            print(f"{RED}Timeout ({REQUEST_TIMEOUT}s){RESET}")
            return ""
        except Exception as e:
            print(f"{RED}{e}{RESET}")
            return ""
    return ""


# ═══════════════════════════════════════════════════════════
#  AGENT LOOP  (primary interaction path)
# ═══════════════════════════════════════════════════════════
def _agent_confirm(description: str) -> bool:
    return input(
        f"{YELLOW}agent wants to: {description}{RESET}  [y/N] "
    ).strip().lower() == "y"


def agent_loop(messages: list, model: str, show_think: bool,
               loaded_files: dict, cwd: Path,
               conn: sqlite3.Connection, session_name: str,
               session_tokens: list = None) -> str:
    import tools as _tools
    _tools.set_log_conn(conn, session_name)

    working     = build_file_ctx(loaded_files or {}) + trim_history(messages)
    _seen_calls: dict = {}

    for round_n in range(MAX_TOOL_ROUNDS):
        payload = {
            "model":      model,
            "messages":   working,
            "tools":      _tools.TOOL_SCHEMAS,
            "stream":     False,
            "keep_alive": KEEP_ALIVE,
            "options":    OPTIONS,
        }
        try:
            resp = requests.post(f"{HOST}/api/chat", json=payload, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            print(f"{RED}Agent error: {e}{RESET}")
            return ""

        data = resp.json()
        p    = data.get("prompt_eval_count", 0)
        c    = data.get("eval_count", 0)
        t    = data.get("total_duration", 0) / 1e9

        if session_tokens is not None:
            session_tokens[0] += (p + c)
        sess = session_tokens[0] if session_tokens is not None else (p + c)

        msg   = data.get("message", {})
        content = msg.get("content", "")
        calls   = msg.get("tool_calls", [])

        if not calls:
            if content:
                print(f"\n{CYAN}{BOLD}[{model} — agent]{RESET}\n")
                display = content
                if not show_think:
                    display = re.sub(r"<think>.*?</think>", "", content,
                                     flags=re.DOTALL).strip()
                print(display)
                print(f"\n{DIM}call: {YELLOW}{p}{DIM}+{GREEN}{c}{DIM}={BOLD}{p+c}{RESET}"
                      f"  {DIM}({t:.1f}s){RESET}")
                print(f"{ctx_bar(sess)}\n")
            return content

        print(f"{DIM}round {round_n+1} — {ctx_bar(sess)}{RESET}")
        working.append({"role": "assistant", "content": content, "tool_calls": calls})

        for call in calls:
            fn   = call.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}

            call_key = f"{name}|{json.dumps(args, sort_keys=True)}"
            _seen_calls[call_key] = _seen_calls.get(call_key, 0) + 1
            if _seen_calls[call_key] > 1:
                print(f"{RED}[loop guard] repeated call: {name} — stopping.{RESET}")
                working.append({"role": "tool", "content":
                    f"[LOOP GUARD] This exact call to {name} was already made. "
                    "Stop retrying. Use a different approach or report inability.",
                    "name": name})
                continue

            print(f"\n{YELLOW}▶ {name}{RESET}  {DIM}{args}{RESET}")
            result = _tools.dispatch(name, args, cwd, _agent_confirm)
            print(f"{DIM}{result[:800]}{'…' if len(result) > 800 else ''}{RESET}\n")
            working.append({"role": "tool", "content": result, "name": name})

    print(f"{YELLOW}[agent] reached {MAX_TOOL_ROUNDS} tool rounds — stopping.{RESET}")
    return ""


# ═══════════════════════════════════════════════════════════
#  PIPE MODE
# ═══════════════════════════════════════════════════════════
def run_pipe_mode():
    piped   = sys.stdin.read().strip()
    prompt  = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else ""
    content = f"{prompt}\n\n---\n{piped}" if prompt else piped
    messages = [{"role": "system", "content": build_system_prompt([])},
                {"role": "user",   "content": content}]
    if not chat(MODEL, messages, show_think=False, options=CHAT_OPTIONS):
        sys.exit(1)


# ═══════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════
def main():
    if not sys.stdin.isatty():
        run_pipe_mode()
        return

    conn           = init_db()
    model          = MODEL
    show_think     = False
    session_name   = None
    loaded_files   = {}
    cwd            = Path.cwd()
    session_tokens = [0]

    # ── Skill routing state ──────────────────────────────────
    #
    # manual_skills:
    #   None          → auto-route (detect skills per message)
    #   []            → locked to core only, no skills
    #   ["docker"]    → locked to these specific skills
    #
    # persona_system:
    #   None          → homelab mode (core + skill injection)
    #   str           → full system prompt replacement (e.g. coder.md)
    #
    # last_auto_skills: display only — what auto-routing matched last time
    manual_skills:    list[str] | None = None
    persona_system:   str | None       = None
    last_auto_skills: list[str]        = []

    # messages stores conversation history.
    # The system message here is always core-only — skill files are injected
    # fresh at call time and NOT stored in history to keep sessions portable.
    messages = [{"role": "system", "content": build_system_prompt([])}]

    print(f"\n{BOLD}── Homelab Agent ───────────────────────{RESET}")
    print(f"{DIM}model : {CYAN}{model}{RESET}  {DIM}host : {HOST}{RESET}")
    print(f"{DIM}db    : {DB_FILE}  type /help for commands{RESET}\n")

    pt = PromptSession(history=FileHistory(str(HISTORY_FILE)))

    while True:
        skill_tag = _skill_prompt_tag(persona_system, manual_skills, last_auto_skills)
        try:
            user_input = pt.prompt(
                HTML(f"<ansiyellow>{cwd.name or str(cwd)}</ansiyellow>"
                     f"{skill_tag}"
                     f" <ansigreen>▸</ansigreen> ")
            ).strip()
        except (KeyboardInterrupt, EOFError):
            print("Bye.")
            break

        if not user_input:
            continue

        cmd = user_input.lower().split()[0]

        # ── /quit ─────────────────────────────────────────
        if cmd in ("/quit", "/exit", "quit", "exit", "q"):
            print("Bye.")
            break

        # ── /help ─────────────────────────────────────────
        elif cmd == "/help":
            turns  = len([m for m in messages if m["role"] != "system"])
            pinned = (f"  {DIM}pinned : {', '.join(Path(k).name for k in loaded_files)}{RESET}"
                      if loaded_files else "")

            if persona_system is not None:
                skill_line = (f"  {DIM}skills : {MAGENTA}persona active{RESET}"
                              f"  {DIM}(/skill off to return to homelab){RESET}")
            elif manual_skills is not None:
                if manual_skills:
                    est = skill_token_estimate(manual_skills)
                    skill_line = (f"  {DIM}skills : {CYAN}manual → "
                                  f"{', '.join(manual_skills)}{RESET}"
                                  f"  {DIM}(~{est} tokens,  /skill off to auto-route){RESET}")
                else:
                    skill_line = f"  {DIM}skills : core only (homelab skills locked out){RESET}"
            else:
                last = ', '.join(last_auto_skills) if last_auto_skills else "none yet"
                est  = skill_token_estimate(last_auto_skills)
                skill_line = (f"  {DIM}skills : {GREEN}auto-routing{RESET}"
                              f"  {DIM}last detected: {last}  (~{est} tokens){RESET}")

            print(f"""
{BOLD}{CYAN}━━━ Commands ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}
  {YELLOW}/model [name]{RESET}             switch model  (now: {CYAN}{model}{RESET})
  {YELLOW}/models{RESET}                   list available models
  {YELLOW}/unload{RESET}                   unload model from VRAM
  {YELLOW}/think [on|off]{RESET}           show <think> blocks  (now: {"on" if show_think else "off"})
  {YELLOW}/history{RESET}                  show conversation  ({turns} msgs){pinned}
  {YELLOW}/compact{RESET}                  summarise + trim history to save context
  {YELLOW}/clear{RESET}                    wipe conversation + unpin files
  {YELLOW}/save [name]{RESET}              save session
  {YELLOW}/sessions{RESET}                 list saved sessions
  {YELLOW}/load [id|name]{RESET}           restore a session
  {YELLOW}/delete [id|name]{RESET}         delete a session
  {YELLOW}/log [N]{RESET}                  show last N changelog entries  (default 20)
  {YELLOW}/log agent [N]{RESET}            show agent tool call log
  {YELLOW}/search <query>{RESET}           web search → answer (no tool loop)
  {YELLOW}/fetch <url> [prompt]{RESET}     fetch URL → answer (no tool loop)
  {YELLOW}/read <file> [prompt]{RESET}     pin file + optional question  ('.' = silent pin)
  {YELLOW}/unread <file>{RESET}            unpin a file
  {YELLOW}/files{RESET}                    list pinned files
  {YELLOW}/run <cmd> [-- prompt]{RESET}    run local command + inject output
  {YELLOW}/diff <file> [instr]{RESET}      edit file, review diff, apply
  {YELLOW}/skill{RESET}                    list all skills and current routing state
  {YELLOW}/skill <name(s)>{RESET}          lock to homelab skill(s): docker opnsense proxmox haos
{RESET}                            or load a persona skill (coder, architect, …)
  {YELLOW}/skill off{RESET}                return to auto-routing
  {YELLOW}/system [text|default]{RESET}    show, set, or reset system prompt
  {YELLOW}/quit{RESET}                     exit
{BOLD}{CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}
{skill_line}

""")

        # ── /models ───────────────────────────────────────
        elif cmd == "/models":
            try:
                r = requests.get(f"{HOST}/api/tags", timeout=10)
                r.raise_for_status()
                print()
                for m in r.json().get("models", []):
                    size   = m.get("size", 0) / 1e9
                    marker = f" {GREEN}◀ active{RESET}" if m["name"] == model else ""
                    print(f"  {YELLOW}{m['name']:<30}{RESET}  {DIM}{size:.1f} GB{RESET}{marker}")
                print()
            except Exception as e:
                print(f"{RED}{e}{RESET}")

        # ── /unload ───────────────────────────────────────
        elif cmd == "/unload":
            try:
                r      = requests.get(f"{HOST}/api/ps", timeout=10)
                loaded = r.json().get("models", [])
                if not loaded:
                    print("No model currently loaded in VRAM.")
                else:
                    for m in loaded:
                        requests.post(f"{HOST}/api/generate",
                                      json={"model": m["name"], "keep_alive": 0}, timeout=10)
                        print(f"{YELLOW}Unloaded:{RESET} {m['name']}")
            except Exception as e:
                print(f"{RED}{e}{RESET}")

        # ── /model ────────────────────────────────────────
        elif cmd == "/model":
            parts = user_input.split(maxsplit=1)
            if len(parts) > 1:
                model = parts[1].strip()
                print(f"Switched to: {CYAN}{model}{RESET}")
            else:
                print(f"Current model: {CYAN}{model}{RESET}")

        # ── /think ────────────────────────────────────────
        elif cmd == "/think":
            parts = user_input.split(maxsplit=1)
            arg   = parts[1].strip().lower() if len(parts) > 1 else None
            show_think = True if arg == "on" else (False if arg == "off" else not show_think)
            print(f"Think blocks: {'on' if show_think else 'off'}")

        # ── /history ──────────────────────────────────────
        elif cmd == "/history":
            non_sys = [m for m in messages if m["role"] != "system"]
            if not non_sys:
                print("No history.")
            else:
                for m in non_sys:
                    prefix  = f"{GREEN}you{RESET}" if m["role"] == "user" else f"{CYAN}agent{RESET}"
                    snippet = m["content"][:120].replace("\n", " ")
                    tail    = "…" if len(m["content"]) > 120 else ""
                    print(f"  {prefix}: {DIM}{snippet}{tail}{RESET}")
                print()
            if loaded_files:
                print(f"  {BOLD}pinned files:{RESET}")
                for k, v in loaded_files.items():
                    print(f"    {GREEN}{Path(k).name}{RESET}  {DIM}({len(v):,} chars){RESET}")
                print()
            print(f"  session total: {ctx_bar(session_tokens[0])}\n")

        # ── /compact ──────────────────────────────────────
        elif cmd == "/compact":
            non_sys = [m for m in messages if m["role"] != "system"]
            if len(non_sys) < 4:
                print("Not enough history to compact.")
                continue
            print(f"{DIM}Summarising…{RESET}")
            transcript = "\n".join(
                f"{m['role'].upper()}: {strip_ansi(m['content'])}" for m in non_sys
            )
            summary = chat(model, [
                {"role": "system", "content":
                 "Summarise the conversation below concisely. "
                 "Preserve key facts, code snippets, file names, and decisions made."},
                {"role": "user", "content": transcript},
            ], show_think=False, session_tokens=None, options=CHAT_OPTIONS)
            if summary:
                sys_msgs = [m for m in messages if m["role"] == "system"]
                messages = sys_msgs + [{"role": "assistant",
                                        "content": f"[Conversation summary]\n{strip_ansi(summary)}"}]
                session_tokens[0] = (
                    sum(len(m["content"]) for m in messages)
                    + sum(len(v) for v in loaded_files.values())
                ) // 4
                print(f"{GREEN}History compacted.{RESET}  "
                      f"{DIM}~{session_tokens[0]:,} tokens remaining{RESET}")
            else:
                print(f"{RED}Compact failed — history unchanged.{RESET}")

        # ── /clear ────────────────────────────────────────
        elif cmd == "/clear":
            messages          = [{"role": "system", "content": build_system_prompt([])}]
            session_name      = None
            session_tokens[0] = 0
            last_auto_skills  = []
            loaded_files.clear()
            print("Cleared.")

        # ── /save ─────────────────────────────────────────
        elif cmd == "/save":
            parts = user_input.split(maxsplit=1)
            name  = (parts[1].strip() if len(parts) > 1
                     else session_name or datetime.datetime.now().strftime("session_%Y%m%d_%H%M%S"))
            db_save(conn, name, model, messages)
            session_name = name
            print(f"Saved: {GREEN}{name}{RESET}")

        # ── /sessions ─────────────────────────────────────
        elif cmd == "/sessions":
            rows = db_list(conn)
            if not rows:
                print("No saved sessions.")
            else:
                print(f"\n{BOLD}{'ID':<4}  {'Name':<24}  {'Model':<22}  Updated{RESET}")
                print("─" * 72)
                for sid, sname, mdl, updated, blob in rows:
                    count  = len([m for m in json.loads(blob) if m["role"] != "system"])
                    marker = f" {GREEN}◀{RESET}" if sname == session_name else ""
                    print(f"{sid:<4}  {sname:<24}  {YELLOW}{mdl:<22}{RESET}  "
                          f"{DIM}{updated}{RESET}  ({count} msgs){marker}")
                print()

        # ── /load ─────────────────────────────────────────
        elif cmd == "/load":
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2:
                print("Usage: /load [id|name]")
                continue
            result = db_load(conn, parts[1].strip())
            if result:
                session_name, model, messages = result
                count = len([m for m in messages if m["role"] != "system"])
                print(f"Loaded {GREEN}'{session_name}'{RESET}  "
                      f"model={CYAN}{model}{RESET}  {count} messages")
            else:
                print(f"{RED}Not found: {parts[1].strip()}{RESET}")

        # ── /delete ───────────────────────────────────────
        elif cmd == "/delete":
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2:
                print("Usage: /delete [id|name]")
                continue
            target  = parts[1].strip()
            preview = db_load(conn, target)
            if not preview:
                print(f"{RED}Not found: {target}{RESET}")
                continue
            p_name, p_model, p_msgs = preview
            count = len([m for m in p_msgs if m["role"] != "system"])
            print(f"  {BOLD}{p_name}{RESET}  model={CYAN}{p_model}{RESET}  {count} messages")
            if input(f"{RED}Delete this session? [y/N]{RESET} ").strip().lower() != "y":
                print("Cancelled.")
                continue
            deleted = db_delete(conn, target)
            if deleted:
                if session_name == deleted:
                    session_name = None
                print(f"{GREEN}Deleted:{RESET} {deleted}")
            else:
                print(f"{RED}Delete failed.{RESET}")

        # ── /log ──────────────────────────────────────────
        elif cmd == "/log":
            parts = user_input.split(maxsplit=2)
            table = "changelog"
            limit = 20
            if len(parts) > 1:
                arg = parts[1].strip()
                if arg == "agent":
                    table = "agent_log"
                    limit = int(parts[2].strip()) if len(parts) > 2 else 20
                else:
                    try:
                        limit = int(arg)
                    except ValueError:
                        print(f"{YELLOW}Usage: /log [N] | /log agent [N]{RESET}")
                        continue

            if table == "changelog":
                rows = conn.execute(
                    "SELECT ts, category, host, summary FROM changelog "
                    "ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
                if not rows:
                    print("No changelog entries yet.")
                else:
                    print(f"\n{BOLD}{'Time':<20}  {'Cat':<10}  {'Host':<10}  Summary{RESET}")
                    print("─" * 80)
                    for ts, cat, host, summary in reversed(rows):
                        cat_color = (GREEN  if cat == "action"   else
                                     CYAN   if cat == "finding"  else
                                     YELLOW if cat == "decision" else RED)
                        print(f"{DIM}{ts:<20}{RESET}  "
                              f"{cat_color}{(cat or '?'):<10}{RESET}  "
                              f"{YELLOW}{(host or '-'):<10}{RESET}  "
                              f"{summary[:60]}{'…' if len(summary) > 60 else ''}")
                    print()
            else:
                rows = conn.execute(
                    "SELECT ts, tool, host, command, ok FROM agent_log "
                    "ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
                if not rows:
                    print("No agent_log entries yet.")
                else:
                    print(f"\n{BOLD}{'Time':<20}  {'Tool':<12}  {'Host':<10}  {'OK':<4}  Command{RESET}")
                    print("─" * 80)
                    for ts, tool, host, command, ok in reversed(rows):
                        ok_str = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
                        print(f"{DIM}{ts:<20}{RESET}  "
                              f"{CYAN}{(tool or '?'):<12}{RESET}  "
                              f"{YELLOW}{(host or '-'):<10}{RESET}  "
                              f"{ok_str:<4}  {(command or '')[:50]}")
                    print()

        # ── /search ───────────────────────────────────────
        elif cmd == "/search":
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2:
                print("Usage: /search <query>")
                continue
            query = parts[1].strip()
            print(f"{DIM}Searching: {query}…{RESET}")
            send  = inject_search(query, messages + [{"role": "user", "content": query}])
            reply = chat(model, send, show_think, loaded_files,
                         session_tokens=session_tokens, options=CHAT_OPTIONS)
            if reply:
                messages.append({"role": "user",     "content": query})
                messages.append({"role": "assistant", "content": reply})

        # ── /fetch ────────────────────────────────────────
        elif cmd == "/fetch":
            parts = user_input.split(maxsplit=2)
            if len(parts) < 2:
                print("Usage: /fetch <url> [prompt]")
                continue
            url    = parts[1].strip()
            prompt = parts[2].strip() if len(parts) > 2 else "Summarise this page concisely."
            print(f"{DIM}Fetching {url[:70]}…{RESET}")
            page  = _fetch_page(url)
            send  = [{"role": "system",
                      "content": f"The user fetched:\nURL: {url}\n\n{page}"}] \
                    + messages + [{"role": "user", "content": prompt}]
            reply = chat(model, send, show_think, loaded_files,
                         session_tokens=session_tokens, options=CHAT_OPTIONS)
            if reply:
                messages.append({"role": "user",     "content": f"/fetch {url} — {prompt}"})
                messages.append({"role": "assistant", "content": reply})

        # ── /read ─────────────────────────────────────────
        elif cmd == "/read":
            parts = user_input.split(maxsplit=2)
            if len(parts) < 2:
                print("Usage: /read <file> [prompt]  ('.' = silent pin)")
                continue
            label, content = read_file(parts[1].strip())
            if label is None:
                print(f"{RED}{content}{RESET}")
                continue
            loaded_files[label] = content
            prompt = parts[2].strip() if len(parts) > 2 else "."
            if prompt == ".":
                print(f"{GREEN}Pinned:{RESET} {Path(label).name}  {DIM}({len(content):,} chars){RESET}")
                continue
            messages.append({"role": "user", "content": prompt})
            reply = chat(model, messages, show_think, loaded_files,
                         session_tokens=session_tokens, options=CHAT_OPTIONS)
            if reply:
                messages.append({"role": "assistant", "content": reply})
            else:
                messages.pop()

        # ── /unread ───────────────────────────────────────
        elif cmd == "/unread":
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2:
                print("Usage: /unread <file>")
                continue
            target = str(Path(parts[1].strip()).expanduser().resolve())
            if target in loaded_files:
                del loaded_files[target]
                print(f"{YELLOW}Unpinned:{RESET} {Path(target).name}")
            else:
                matches = [k for k in loaded_files if Path(k).name == parts[1].strip()]
                if matches:
                    del loaded_files[matches[0]]
                    print(f"{YELLOW}Unpinned:{RESET} {Path(matches[0]).name}")
                else:
                    print(f"{RED}Not pinned: {parts[1].strip()}{RESET}")

        # ── /files ────────────────────────────────────────
        elif cmd == "/files":
            if not loaded_files:
                print("No files pinned.")
            else:
                total = 0
                for k, v in loaded_files.items():
                    total += len(v)
                    print(f"  {GREEN}{Path(k).name:<30}{RESET}  {DIM}{len(v):,} chars  {k}{RESET}")
                print(f"  {DIM}total: {total:,} chars (~{total//4:,} tokens){RESET}")

        # ── /run ──────────────────────────────────────────
        elif cmd == "/run":
            rest = user_input[len("/run"):].strip()
            if not rest:
                print("Usage: /run <command> [-- prompt]")
                continue
            if " -- " in rest:
                shell_cmd, prompt = rest.split(" -- ", 1)
                shell_cmd = shell_cmd.strip()
                prompt    = prompt.strip()
            else:
                shell_cmd = rest
                prompt    = "Explain this output."

            new_cwd, cd_err = resolve_cd(shell_cmd, cwd)
            if cd_err:
                print(f"{RED}{cd_err}{RESET}")
                continue
            if new_cwd is not None:
                cwd = new_cwd
                print(f"{GREEN}cwd:{RESET} {cwd}")
                continue

            try:
                if needs_confirm(shell_cmd):
                    if input(f"{YELLOW}confirm run: {shell_cmd}{RESET}  [y/N] ").strip().lower() != "y":
                        print("Cancelled.")
                        continue
                print(f"{DIM}[{cwd}]$ {shell_cmd}{RESET}")
                cmd_run, output = run_command(shell_cmd, cwd)
                print(f"{DIM}{output[:800]}{'…' if len(output) > 800 else ''}{RESET}")
            except ValueError as e:
                print(f"{RED}{e}{RESET}")
                continue
            except subprocess.TimeoutExpired:
                print(f"{RED}Command timed out.{RESET}")
                continue

            send  = [{"role": "system",
                      "content": f"User ran `{cmd_run}` in `{cwd}`\n\nOutput:\n```\n{output}\n```"}] \
                    + messages + [{"role": "user", "content": prompt}]
            reply = chat(model, send, show_think, loaded_files,
                         session_tokens=session_tokens, options=CHAT_OPTIONS)
            if reply:
                messages.append({"role": "user",     "content": f"/run `{cmd_run}` — {prompt}"})
                messages.append({"role": "assistant", "content": reply})

        # ── /diff ─────────────────────────────────────────
        elif cmd == "/diff":
            parts = user_input.split(maxsplit=2)
            if len(parts) < 2:
                print("Usage: /diff <file> [instruction]")
                continue
            label, original = read_file(parts[1].strip())
            if label is None:
                print(f"{RED}{original}{RESET}")
                continue

            lines = original.splitlines()
            if lines:
                plus_ratio = sum(1 for l in lines if l.startswith("+")) / len(lines)
                if plus_ratio > 0.8:
                    print(f"{YELLOW}File looks like a patch/log "
                          f"({plus_ratio:.0%} lines start with '+'). Proceed? [y/N]{RESET} ",
                          end="")
                    if input().strip().lower() != "y":
                        continue

            instruction = (parts[2].strip() if len(parts) > 2 else
                           "Review this file and suggest improvements. "
                           "Return the full corrected file in a code block.")
            send  = [{"role": "system",
                      "content": f"File to edit: {label}\n\n```\n{original}\n```\n\n"
                                 "Return ONLY the complete revised file in a single fenced code block."}] \
                    + messages + [{"role": "user", "content": instruction}]
            reply = chat(model, send, show_think, loaded_files,
                         session_tokens=session_tokens, options=CHAT_OPTIONS)
            if not reply:
                continue

            proposed = extract_code_block(reply)
            if not proposed:
                print(f"{YELLOW}No code block in response.{RESET}")
                print(f"{DIM}{reply.strip()[:300].replace(chr(10), ' ')}…{RESET}")
                messages.append({"role": "user",     "content": f"/diff {label}"})
                messages.append({"role": "assistant", "content": reply})
                continue

            patch = diff_lines(original, proposed)
            if not patch:
                print(f"{GREEN}No changes proposed.{RESET}")
                continue

            print(f"\n{BOLD}── Proposed diff ───────────────────────{RESET}")
            for line in patch.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    print(f"{GREEN}{line}{RESET}")
                elif line.startswith("-") and not line.startswith("---"):
                    print(f"{RED}{line}{RESET}")
                else:
                    print(f"{DIM}{line}{RESET}")
            print(f"{BOLD}────────────────────────────────────────{RESET}\n")

            ans = input(f"Apply to {label}?  [y/N/e(dit)] ").strip().lower()
            if ans == "y":
                Path(label).write_text(proposed, encoding="utf-8")
                print(f"{GREEN}Saved.{RESET}")
                messages.append({"role": "user",     "content": f"/diff {label} — {instruction}"})
                messages.append({"role": "assistant", "content": reply})
            elif ans == "e":
                with tempfile.NamedTemporaryFile(mode="w", suffix=Path(label).suffix,
                                                 delete=False, encoding="utf-8") as tmp:
                    tmp.write(proposed)
                    tmp_path = tmp.name
                subprocess.run([EDITOR, tmp_path])
                edited = Path(tmp_path).read_text(encoding="utf-8")
                Path(tmp_path).unlink(missing_ok=True)
                Path(label).write_text(edited, encoding="utf-8")
                print(f"{GREEN}Saved (edited).{RESET}")
            else:
                print("Discarded.")

        # ── /skill ────────────────────────────────────────
        elif cmd == "/skill":
            args = user_input.split()[1:]

            # /skill  — list everything
            if not args:
                skills = list_skills()
                if not skills:
                    print(f"{YELLOW}No skills in {SKILLS_DIR.resolve()}{RESET}")
                    continue

                print(f"\n{BOLD}Homelab skills  (auto-routed, injectable):{RESET}")
                for key, (kws, fname) in SKILL_ROUTES.items():
                    slabel, _ = load_skill(fname)
                    status    = f"{GREEN}✓{RESET}" if slabel else f"{RED}✗ missing{RESET}"
                    kw_prev   = ", ".join(kws[:4]) + ("…" if len(kws) > 4 else "")
                    marker    = ""
                    if manual_skills is not None and key in manual_skills:
                        marker = f"  {CYAN}◀ locked*{RESET}"
                    elif manual_skills is None and key in last_auto_skills:
                        marker = f"  {BLUE}◀ last auto{RESET}"
                    print(f"  {YELLOW}{key:<10}{RESET} {status}  "
                          f"{DIM}skill/{fname}.md   triggers: {kw_prev}{RESET}{marker}")

                other = [p for p in skills
                         if p.stem != "core"
                         and p.stem not in {v[1] for v in SKILL_ROUTES.values()}]
                if other:
                    print(f"\n{BOLD}Persona skills  (full system replacement):{RESET}")
                    for p in other:
                        try:
                            preview = p.read_text(encoding="utf-8").strip()[:60].replace("\n", " ")
                        except Exception:
                            preview = "[unreadable]"
                        active = (f"  {MAGENTA}◀ active{RESET}"
                                  if persona_system is not None else "")
                        print(f"  {YELLOW}{p.stem:<20}{RESET}  {DIM}{preview}…{RESET}{active}")

                # Routing state summary
                print()
                if persona_system is not None:
                    print(f"  {DIM}state : {MAGENTA}persona active{RESET}"
                          f"  {DIM}— /skill off to return to homelab{RESET}")
                elif manual_skills is not None:
                    locked = ', '.join(manual_skills) if manual_skills else "none (core only)"
                    print(f"  {DIM}state : {CYAN}manual lock → {locked}{RESET}"
                          f"  {DIM}— /skill off to auto-route{RESET}")
                else:
                    last = ', '.join(last_auto_skills) if last_auto_skills else "none yet"
                    print(f"  {DIM}state : {GREEN}auto-routing{RESET}"
                          f"  {DIM}— last detected: {last}{RESET}")

                print(f"\n{DIM}Usage:\n"
                      f"  /skill docker              lock to one homelab skill\n"
                      f"  /skill docker opnsense     lock to multiple (cross-domain)\n"
                      f"  /skill coder               load persona skill\n"
                      f"  /skill off                 return to auto-routing{RESET}\n")
                continue

            # /skill off
            if args[0].lower() == "off":
                manual_skills    = None
                persona_system   = None
                last_auto_skills = []
                messages = apply_system(messages, build_system_prompt([]))
                print(f"{GREEN}Auto-routing enabled.{RESET}  "
                      f"{DIM}Skills will be detected per message.{RESET}")
                continue

            # Split args into routed homelab skills vs persona skill names
            routed  = [a for a in args if a in _ROUTED_SKILL_NAMES]
            persona = [a for a in args if a not in _ROUTED_SKILL_NAMES]

            if routed and not persona:
                # All args are homelab skills → manual lock
                missing = [key for key in routed
                           if load_skill(SKILL_ROUTES[key][1])[0] is None]
                if missing:
                    print(f"{RED}Missing skill files: "
                          f"{', '.join(SKILL_ROUTES[k][1] + '.md' for k in missing)}{RESET}")
                    continue
                manual_skills  = routed
                persona_system = None
                est = skill_token_estimate(manual_skills)
                print(f"{CYAN}Manual lock:{RESET} {', '.join(manual_skills)}  "
                      f"{DIM}(~{est} tokens per call){RESET}")
                if len(manual_skills) > 1:
                    print(f"{DIM}Cross-domain context active — "
                          f"both skill files will inject on every message.{RESET}")

            elif persona and not routed:
                # Persona skill → full system replacement
                if len(persona) > 1:
                    print(f"{YELLOW}Persona mode takes one skill at a time. "
                          f"Using '{persona[0]}'.{RESET}")
                name = persona[0]
                slabel, content = load_skill(name)
                if slabel is None:
                    print(f"{RED}{content}{RESET}")
                    continue
                persona_system = content
                manual_skills  = None
                messages       = apply_system(messages, content)
                print(f"{MAGENTA}Persona:{RESET} {slabel}  "
                      f"{DIM}{content[:80].replace(chr(10), ' ')}…{RESET}")

            else:
                # Mixed homelab + persona args
                print(f"{YELLOW}Cannot mix homelab skills ({', '.join(routed)}) "
                      f"with persona skills ({', '.join(persona)}).{RESET}")
                print(f"{DIM}  homelab: /skill docker opnsense{RESET}")
                print(f"{DIM}  persona: /skill coder{RESET}")

        # ── /system ───────────────────────────────────────
        elif cmd == "/system":
            parts = user_input.split(maxsplit=1)
            if len(parts) == 1:
                cur = next((m["content"] for m in messages if m["role"] == "system"), None)
                if cur:
                    print(f"\n{BOLD}System prompt:{RESET}\n{DIM}{cur}{RESET}\n")
                else:
                    print(f"{DIM}No system prompt set.{RESET}")
            else:
                arg = parts[1].strip()
                if arg.lower() == "default":
                    manual_skills    = None
                    persona_system   = None
                    last_auto_skills = []
                    messages = apply_system(messages, build_system_prompt([]))
                    print(f"{GREEN}Reset to homelab agent (auto-routing).{RESET}")
                else:
                    persona_system = arg
                    manual_skills  = None
                    messages       = apply_system(messages, arg)
                    print(f"{GREEN}System prompt updated.{RESET}")
                    print(f"{DIM}{arg[:120]}{'…' if len(arg) > 120 else ''}{RESET}\n")

        # ── unknown command ───────────────────────────────
        elif cmd.startswith("/"):
            print(f"Unknown command: {cmd}  (type /help)")

        # ── normal input → agent ──────────────────────────
        else:
            # Determine which skills to inject for this message.
            # Skills are injected into the agent call only — not stored in history.
            if persona_system is not None:
                system_prompt = persona_system
                active_skills = []

            elif manual_skills is not None:
                system_prompt = build_system_prompt(manual_skills)
                active_skills = manual_skills

            else:
                # Auto-route: detect from message text
                active_skills    = detect_skills(user_input)
                last_auto_skills = active_skills
                system_prompt    = build_system_prompt(active_skills)
                if active_skills:
                    est = skill_token_estimate(active_skills)
                    print(f"{DIM}→ {', '.join(active_skills)}  (~{est} tokens){RESET}")

            agent_messages = (
                [{"role": "system", "content": system_prompt}]
                + [m for m in messages if m["role"] != "system"]
                + [{"role": "user", "content": user_input}]
            )
            reply = agent_loop(
                agent_messages, model, show_think, loaded_files,
                cwd, conn, session_name, session_tokens=session_tokens
            )
            if reply:
                messages.append({"role": "user",     "content": user_input})
                messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
