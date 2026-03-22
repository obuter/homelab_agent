"""
tools.py — Agent tool implementations for ollama_cli.py
Imported at startup (for BLOCKED_CMDS/CONFIRM_CMDS) and by agent_loop().

Credentials loaded from .env (python-dotenv) or set as env vars:
    SSH_USER, SSH_PASS
    SSH_HOST_DEBIAN, SSH_HOST_PBS, SSH_HOST_HAOS, SSH_HOST_OPNSENSE
    OPNSENSE_HOST, OPNSENSE_KEY, OPNSENSE_SECRET
    HA_HOST, HA_TOKEN

Per-host credential overrides: SSH_USER_PBS, SSH_PASS_PBS, etc.
"""

import os
import re
import ssl
import json
import base64
import sqlite3
import datetime
import subprocess
import urllib.request
from pathlib import Path

# ── Optional deps ─────────────────────────────────────────────
try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ── Credentials ───────────────────────────────────────────────
SSH_USER = os.getenv("SSH_USER", "")
SSH_PASS = os.getenv("SSH_PASS", "")

SSH_HOSTS: dict[str, str] = {
    _key[len("SSH_HOST_"):].lower(): _val
    for _key, _val in os.environ.items()
    if _key.startswith("SSH_HOST_")
}

OPNSENSE_HOST   = os.getenv("OPNSENSE_HOST",   "0.0.0.0")
OPNSENSE_KEY    = os.getenv("OPNSENSE_KEY",    "")
OPNSENSE_SECRET = os.getenv("OPNSENSE_SECRET", "")
HA_HOST         = os.getenv("HA_HOST",  "0.0.0.0")
HA_TOKEN        = os.getenv("HA_TOKEN", "")

# ── DB path — must match DB_FILE in ollama_cli.py ─────────────
DB_PATH = Path("homelab.db")

# ── Constants ─────────────────────────────────────────────────
BROWSERLESS_URL      = os.getenv("BROWSERLESS_URL", "http://localhost:3000")
BROWSE_FETCH_CHARS   = 12000
BROWSE_FETCH_TIMEOUT = 30
SSH_READ_TIMEOUT     = 90
SHELL_TIMEOUT        = 120

# ── Safety lists (shared with ollama_cli.py /run) ─────────────
BLOCKED_CMDS = (
    "rm -rf", "rm ", "rmdir", "mkfs", "dd ", "dd if=",
    "shutdown", "reboot", "poweroff", ":(){ :", "chmod 777", "> /dev/",
    "ssh ",
)
CONFIRM_CMDS = (
    "sudo", "apt ", "pip ", "systemctl", "docker rm",
    "docker rmi", "docker restart", "kill", "pkill",
)

# SSH commands that mutate state — always require user confirmation
SSH_CONFIRM_CMDS = (
    "mkdir", "touch", "rm ", "rmdir", "mv ", "cp ",
    "chmod", "chown", "tee ", "echo ", "> ", ">> ",
    "docker restart", "docker stop", "docker start",
    "docker run", "docker compose up", "docker pull",
    "systemctl",
)

# ── Safety helpers ────────────────────────────────────────────
def _is_blocked(cmd: str) -> str | None:
    lower = cmd.lower()
    for b in BLOCKED_CMDS:
        if b in lower:
            return b
    return None


def _needs_confirm(cmd: str) -> bool:
    lower = cmd.lower()
    return any(p in lower for p in CONFIRM_CMDS)


# ════════════════════════════════════════════════════════════
#  DB LOGGING
# ════════════════════════════════════════════════════════════
_log_conn: sqlite3.Connection | None = None
_log_session: str | None = None

_ERROR_PREFIXES = (
    "[ERROR]", "[BLOCKED]", "[SSH ERROR]", "[TIMEOUT]",
    "[HTTP ERROR]", "[DB ERROR]", "[BROWSE ERROR]", "[SEARCH ERROR]", "[CANCELLED]",
)


def set_log_conn(conn: sqlite3.Connection, session_name: str | None = None):
    global _log_conn, _log_session
    _log_conn    = conn
    _log_session = session_name


def _log(tool: str, command: str, result: str, host: str | None = None):
    if _log_conn is None:
        return
    ok = 0 if any(result.startswith(p) for p in _ERROR_PREFIXES) else 1
    try:
        _log_conn.execute(
            "INSERT INTO agent_log (ts, session, tool, host, command, result_head, ok) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                datetime.datetime.now().isoformat(timespec="seconds"),
                _log_session, tool, host or None,
                command[:500], result[:300], ok,
            ),
        )
        _log_conn.commit()
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
#  WEB FETCH  (single implementation, used by agent + cli)
# ════════════════════════════════════════════════════════════
def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def tool_browse_url(url: str) -> str:
    """
    Fetch a URL via the local Browserless container (full JS rendering).
    Falls back to direct HTTP fetch if Browserless is unreachable.
    """
    # 1. Browserless — POST /chromium/content → returns rendered HTML
    if BROWSERLESS_URL:
        try:
            payload = json.dumps({
                "url": url,
                "waitForTimeout": 2000,
                "rejectResourceTypes": ["image", "font", "media"],
            }).encode()
            req = urllib.request.Request(
                f"{BROWSERLESS_URL}/chromium/content",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=BROWSE_FETCH_TIMEOUT) as r:
                html = r.read(262144).decode("utf-8", errors="ignore")
            if html and len(html.strip()) > 200:
                return _strip_html(html)[:BROWSE_FETCH_CHARS]
        except Exception:
            pass  # fall through to direct fetch

    # 2. Direct HTTP fallback (no JS, works for static pages)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; HomelabBot/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            ct = r.headers.get("Content-Type", "")
            if "html" not in ct and "text" not in ct:
                return "[non-text content — cannot display binary or media files]"
            raw = r.read(131072).decode("utf-8", errors="ignore")
        return _strip_html(raw)[:BROWSE_FETCH_CHARS]
    except Exception as e:
        return f"[BROWSE ERROR] {e}"


# ════════════════════════════════════════════════════════════
#  TOOL IMPLEMENTATIONS
# ════════════════════════════════════════════════════════════
def tool_shell_exec(command: str, cwd: Path, confirm_cb) -> str:
    blocked = _is_blocked(command)
    if blocked:
        return f"[BLOCKED] '{blocked}' is not permitted."
    if _needs_confirm(command) and not confirm_cb(command):
        return "[CANCELLED] User declined."
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=SHELL_TIMEOUT, cwd=str(cwd),
        )
        out = (r.stdout + r.stderr).strip() or f"[exit {r.returncode}, no output]"
        return out[:4000]
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT] Command exceeded {SHELL_TIMEOUT}s — for nmap use -T4 --max-retries 1"
    except Exception as e:
        return f"[ERROR] {e}"


def tool_read_file(path: str) -> str:
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"[NOT FOUND] {path}"
        if not p.is_file():
            return f"[NOT A FILE] {path}"
        content = p.read_text(encoding="utf-8", errors="replace")
        truncated = "\n[truncated]" if len(content) > 8000 else ""
        return content[:8000] + truncated
    except Exception as e:
        return f"[ERROR] {e}"


def tool_write_file(path: str, content: str, confirm_cb) -> str:
    if not confirm_cb(f"write {len(content)} chars to {path}"):
        return "[CANCELLED] User declined write."
    try:
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"[OK] Written {len(content)} chars to {path}"
    except Exception as e:
        return f"[ERROR] {e}"


def tool_web_search(query: str) -> str:
    if DDGS is None:
        return "[ERROR] ddgs not installed — run: pip install ddgs"
    try:
        with DDGS() as ddg:
            results = ddg.text(query, max_results=4)
        if not results:
            return "No results found."
        return "\n\n".join(
            f"[{r['title']}]\n{r['body']}\nURL: {r['href']}" for r in results
        )
    except Exception as e:
        return f"[SEARCH ERROR] {e}"


def tool_http_get(url: str, headers: dict | None = None) -> str:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(65536).decode("utf-8", errors="ignore")
        try:
            return json.dumps(json.loads(body), indent=2)[:4000]
        except Exception:
            return body[:4000]
    except Exception as e:
        return f"[HTTP ERROR] {e}"


def tool_ssh_exec(command: str, host: str = "debian", confirm_cb=None) -> str:
    try:
        import paramiko
    except ImportError:
        return "[ERROR] paramiko not installed — run: pip install paramiko"

    _host = SSH_HOSTS.get(host.lower())
    if not _host:
        available = ", ".join(SSH_HOSTS) or "none configured"
        return f"[ERROR] Unknown host '{host}'. Available: {available}"

    _user = os.getenv(f"SSH_USER_{host.upper()}", SSH_USER)
    _pass = os.getenv(f"SSH_PASS_{host.upper()}", SSH_PASS)

    if not all([_host, _user, _pass]):
        return "[ERROR] SSH credentials not set in .env"

    blocked = _is_blocked(command)
    if blocked:
        return f"[BLOCKED] '{blocked}' is not permitted."

    lower = command.lower()
    if confirm_cb and any(p in lower for p in SSH_CONFIRM_CMDS):
        if not confirm_cb(f"ssh {host}: {command}"):
            return "[CANCELLED] User declined — command was NOT executed."

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(_host, username=_user, password=_pass, timeout=10)
        _, stdout, stderr = client.exec_command(command)
        stdout.channel.settimeout(SSH_READ_TIMEOUT)
        stderr.channel.settimeout(SSH_READ_TIMEOUT)
        try:
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
        except Exception:
            client.close()
            return f"[TIMEOUT] SSH read exceeded {SSH_READ_TIMEOUT}s — command may still be running"
        client.close()
        return ((out + err).strip() or "[no output]")[:6000]
    except Exception as e:
        return f"[SSH ERROR] {e}"


def tool_opnsense(endpoint: str) -> str:
    url   = f"https://{OPNSENSE_HOST}{endpoint}"
    creds = base64.b64encode(f"{OPNSENSE_KEY}:{OPNSENSE_SECRET}".encode()).decode()
    req   = urllib.request.Request(url, headers={
        "Authorization": f"Basic {creds}",
        "Content-Type":  "application/json",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            body = r.read(65536).decode("utf-8", errors="ignore")
        try:
            return json.dumps(json.loads(body), indent=2)[:4000]
        except Exception:
            return body[:4000]
    except Exception as e:
        return f"[OPNSENSE ERROR] {e}"


def tool_query_db(sql: str) -> str:
    stripped = sql.strip().lstrip(";").strip()
    first    = stripped.split()[0].upper() if stripped else ""
    if first not in ("SELECT", "WITH"):
        return "[BLOCKED] Only SELECT queries are permitted against the database."
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(stripped).fetchmany(50)
        conn.close()
        if not rows:
            return "[no rows returned]"
        keys  = list(rows[0].keys())
        lines = [" | ".join(keys), "─" * 60]
        lines += [" | ".join(str(r[k]) if r[k] is not None else "NULL" for k in keys) for r in rows]
        return "\n".join(lines)
    except Exception as e:
        return f"[DB ERROR] {e}"


def tool_log_action(summary: str, category: str = "action",
                    detail: str = "", host: str = "") -> str:
    if not summary.strip():
        return "[ERROR] summary cannot be empty."
    if category not in {"action", "finding", "error", "decision"}:
        category = "action"

    def _write(conn: sqlite3.Connection):
        conn.execute(
            "INSERT INTO changelog (ts, session, category, summary, detail, host) "
            "VALUES (?,?,?,?,?,?)",
            (
                datetime.datetime.now().isoformat(timespec="seconds"),
                _log_session, category,
                summary.strip()[:500],
                detail.strip()[:2000] if detail else None,
                host.strip() or None,
            ),
        )
        conn.commit()

    try:
        if _log_conn is not None:
            _write(_log_conn)
        else:
            conn = sqlite3.connect(DB_PATH)
            _write(conn)
            conn.close()
        return f"[OK] Changelog entry written: [{category}] {summary[:80]}"
    except Exception as e:
        return f"[DB ERROR] {e}"


# ════════════════════════════════════════════════════════════
#  TOOL SCHEMAS
# ════════════════════════════════════════════════════════════
def _ssh_host_description() -> str:
    if SSH_HOSTS:
        entries = ", ".join(sorted(SSH_HOSTS))
        return (
            f"Run a shell command on a remote homelab host via SSH. "
            f"Available hosts: {entries}. "
            f"OPNsense runs FreeBSD as root — no sudo needed, use 'cscli' for CrowdSec. "
            f"Debian/PBS run Linux. Default host: debian. "
            f"For nmap on LAN targets prefer debian; for external IPs prefer shell_exec on local machine."
        )
    return "Run a shell command on a remote homelab host via SSH. Default host: debian."


def _ssh_host_enum() -> str:
    if SSH_HOSTS:
        return f"Target host label: {', '.join(sorted(SSH_HOSTS))}. Defaults to debian."
    return "Target host label. Defaults to debian."


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "shell_exec",
            "description": (
                "Run a shell command on the LOCAL machine and return stdout+stderr. "
                "Use for nmap on external IPs, local file inspection, etc. "
                "Always add -T4 --max-retries 1 to any nmap command."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute locally"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from LOCAL disk and return its contents.",
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
            "description": "Write content to a LOCAL file. Overwrites if exists. Requires user confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web via DuckDuckGo. Returns snippets and URLs. Use browse_url() to read a full page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": (
                "Make an HTTP GET request to a URL or local API endpoint. "
                "Use for Home Assistant API, Proxmox API, GitHub API, Docker Hub API, JSON endpoints. "
                "For public web pages use browse_url() instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url":     {"type": "string"},
                    "headers": {"type": "object", "description": "Optional HTTP headers (auth tokens, etc.)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_url",
            "description": (
                "Fetch a public web URL and return its content as clean text. "
                "Uses a self-hosted Browserless container for full JS rendering. "
                "Falls back to direct HTTP for static pages if Browserless is unreachable. "
                "Use for documentation, articles, GitHub READMEs, JS-heavy pages. "
                "Do NOT use for local/private APIs — use http_get() for those."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL starting with http:// or https://"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_exec",
            "description": _ssh_host_description(),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run on the remote host"},
                    "host":    {"type": "string", "description": _ssh_host_enum(), "default": "debian"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "opnsense",
            "description": (
                "Query the OPNsense REST API (HTTPS). Returns JSON. "
                "Use for firmware status, interface info, firewall rules, system health, CrowdSec plugin. "
                "Example endpoints: /api/core/firmware/status, /api/interfaces/overview/export, "
                "/api/crowdsec/settings/getSettings"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "endpoint": {"type": "string", "description": "API path starting with /api/"},
                },
                "required": ["endpoint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_db",
            "description": (
                "Run a read-only SELECT query against homelab.db. "
                "Tables: sessions, agent_log, changelog. "
                "Only SELECT statements allowed. Returns up to 50 rows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A valid SQLite SELECT statement"},
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_action",
            "description": (
                "Write a changelog entry to homelab.db describing something the agent just did, "
                "found, or decided. Call after any significant action. "
                "category must be one of: action, finding, error, decision."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary":  {"type": "string", "description": "1-2 sentence plain-English description"},
                    "category": {"type": "string", "description": "action | finding | error | decision"},
                    "detail":   {"type": "string", "description": "Optional: raw output, config snippet, or diff"},
                    "host":     {"type": "string", "description": "Affected host: debian, opnsense, pbs, haos"},
                },
                "required": ["summary", "category"],
            },
        },
    },
]


# ════════════════════════════════════════════════════════════
#  DISPATCHER
# ════════════════════════════════════════════════════════════
def dispatch(name: str, args: dict, cwd: Path, confirm_cb) -> str:
    if name == "shell_exec":
        r = tool_shell_exec(args["command"], cwd, confirm_cb)
        _log("shell_exec", args["command"], r)
        return r

    if name == "read_file":
        r = tool_read_file(args["path"])
        _log("read_file", args["path"], r)
        return r

    if name == "write_file":
        r = tool_write_file(args["path"], args["content"], confirm_cb)
        _log("write_file", args["path"], r)
        return r

    if name == "web_search":
        r = tool_web_search(args["query"])
        _log("web_search", args["query"], r)
        return r

    if name == "http_get":
        r = tool_http_get(args["url"], args.get("headers"))
        _log("http_get", args["url"], r)
        return r

    if name == "browse_url":
        r = tool_browse_url(args["url"])
        _log("browse_url", args["url"], r)
        return r

    if name == "ssh_exec":
        host = args.get("host", "debian")
        r    = tool_ssh_exec(args["command"], host, confirm_cb)
        _log("ssh_exec", args["command"], r, host=host)
        return r

    if name == "opnsense":
        r = tool_opnsense(args["endpoint"])
        _log("opnsense", args["endpoint"], r, host="opnsense")
        return r

    if name == "query_db":
        r = tool_query_db(args["sql"])
        _log("query_db", args["sql"], r)
        return r

    if name == "log_action":
        return tool_log_action(
            args["summary"],
            args.get("category", "action"),
            args.get("detail", ""),
            args.get("host", ""),
        )

    return f"[UNKNOWN TOOL] {name}"
