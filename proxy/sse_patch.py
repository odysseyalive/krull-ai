"""
SSE Patch Proxy — Filter + Translation layer for Claude Code ↔ Ollama.

Provides three critical functions:
1. Inlet filters: Injects date context, SearXNG web search results, and
   Kiwix offline knowledge into messages before they reach the model.
2. Responses API translation: Converts LiteLLM's OpenAI Responses API
   format to Chat Completions format (which Ollama supports).
3. Tool call passthrough: Routes to Ollama's /v1/chat/completions which
   preserves tool_calls in responses (Open WebUI's API swallows them).

Flow: Claude Code → LiteLLM → this proxy → Ollama /v1/chat/completions
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response

app = FastAPI()

# ── Structured File Logger ────────────────────────────────────────────────
# Writes JSON Lines to /app/logs/proxy.jsonl (bind-mounted to ./logs/ on
# the host). Each line has: timestamp, session_id, level, category, message,
# and an optional data dict. Also prints to stderr for `docker logs`.

LOG_DIR = Path(os.environ.get("KRULL_LOG_DIR", "/app/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
_log_file = open(LOG_DIR / "proxy.jsonl", "a", buffering=1)  # line-buffered

# Current request's session ID — set per-request via contextvars
import contextvars
_current_session_id = contextvars.ContextVar("session_id", default="unknown")


def proxy_log(category: str, message: str, *, level: str = "info",
              data: dict | None = None):
    """Write a structured log entry to both file and stderr."""
    entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "sid": _current_session_id.get(),
        "level": level,
        "cat": category,
        "msg": message,
    }
    if data:
        entry["data"] = data
    line = json.dumps(entry, default=str)
    _log_file.write(line + "\n")
    # Also print to stderr for docker logs (abbreviated)
    print(f"[{category}] {message}", file=sys.stderr, flush=True)


def get_session_id(request: Request) -> str:
    """Extract session ID from request headers, session file, or generate one.

    Priority: header > ~/.krull-session file > generated UUID.
    The session file is written by krull-claude on startup and cleaned
    up on exit. The host's $HOME is bind-mounted read-only into the
    container at the same path.
    """
    sid = (request.headers.get("x-krull-session")
           or request.headers.get("x-session-id"))
    if sid:
        return sid
    # Try reading the session file written by krull-claude
    host_home = os.environ.get("KRULL_HOST_HOME", "")
    if host_home:
        session_file = Path(host_home) / ".krull-session"
        try:
            return session_file.read_text().strip()
        except (FileNotFoundError, PermissionError):
            pass
    return uuid.uuid4().hex[:12]


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://krull-ollama:11434")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://krull-searxng:8080")
KIWIX_URL = os.environ.get("KIWIX_URL", "http://krull-kiwix:8080")
ENABLE_WEB_SEARCH = os.environ.get("ENABLE_WEB_SEARCH", "true").lower() == "true"
ENABLE_KIWIX = os.environ.get("ENABLE_KIWIX", "true").lower() == "true"
ENABLE_DATE = os.environ.get("ENABLE_DATE", "true").lower() == "true"
SEARCH_RESULTS = int(os.environ.get("SEARCH_RESULTS", "5"))
NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "131072"))
# Per-tool-result cap for the small-model adapter (task #16). A single
# Read of a big file can dump tens of thousands of chars into the
# conversation; after 5-6 such Reads the qwen 9B is past its working
# attention window and stops emitting tool calls. Capping each result
# independently keeps context growth linear in number of tool calls
# rather than in tool result sizes.
KRULL_TOOL_RESULT_MAX_CHARS = int(
    os.environ.get("KRULL_TOOL_RESULT_MAX_CHARS", "15000")
)
# Size above which a single tool result is considered "large" and
# triggers the synthesis directive on its own (even if it's the only
# substantial result so far). Empirically, a single result this big
# is enough to overwhelm the 9B model's working attention — after
# receiving it, the model often emits an empty response because it
# can't compress the data into an answer. The directive tells it to
# synthesize NOW rather than call more tools.
KRULL_LARGE_RESULT_SYNTHESIS_THRESHOLD = int(
    os.environ.get("KRULL_LARGE_RESULT_SYNTHESIS_THRESHOLD", "5000")
)
# Stalled-progress threshold (task #23): if the model has made this
# many consecutive tool-call turns without producing a final text
# answer, the proxy injects a "stop and summarize" warning. Catches
# spelunking loops where the model alternates between tools without
# making progress (e.g. Read random offsets, Grep different patterns,
# Read more offsets, Grep again, ...).
KRULL_STALLED_PROGRESS_THRESHOLD = int(
    os.environ.get("KRULL_STALLED_PROGRESS_THRESHOLD", "12")
)

STRIP_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}

# Cache tool call arguments by call_id so we can patch empty args in follow-up requests.
# LiteLLM sometimes strips arguments when converting Responses API round-trips.
_tool_call_cache = {}  # call_id → arguments (JSON string)


# ── Tool filtering & guidance ─────────────────────────────────────────────

# Only forward these tools to Ollama. The 9B model can't handle 92 tools.
# Claude Code will still execute them — the model just needs to call them.
ALLOWED_TOOLS = {
    # Core file operations
    "Read", "Write", "Edit",
    # Shell — covers mkdir, sed, awk, git, etc.
    "Bash",
    # Search & discovery
    "Glob", "Grep",
    # Notebook editing
    "NotebookEdit",
    # Skill invocation (/study-prep, /bible-study, etc.)
    "Skill",
    # Task tracking (used by skills)
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList",
    # Sub-agents (used by complex skills)
    "Agent",
    # Web fetching
    "WebFetch", "WebSearch",
}

TOOL_GUIDANCE = """IMPORTANT: When you need to perform an action, you MUST use tool_calls. Do NOT output JSON as text.

Parameter name reminders (use these EXACT names):
- Read: file_path (not path)
- Write: file_path, content
- Edit: file_path, old_string, new_string
- Bash: command (not cmd)
- Glob: pattern
- Grep: pattern
- Skill: skill, args"""


def filter_tools(tools: list) -> list:
    """Keep only the tools the model can reliably use."""
    filtered = []
    for tool in tools:
        # Extract tool name from either Chat Completions or Responses API format
        name = ""
        if "function" in tool:
            name = tool["function"].get("name", "")
        else:
            name = tool.get("name", "")
        if name in ALLOWED_TOOLS:
            filtered.append(tool)
    if len(filtered) != len(tools):
        proxy_log("PROXY", f"Filtered tools: {len(tools)} → {len(filtered)} "
                  f"({', '.join(ALLOWED_TOOLS & {t.get('function', t).get('name', t.get('name', '')) for t in filtered})})",
                  data={"from": len(tools), "to": len(filtered)})
    return filtered

# Common parameter name mistakes → corrections
PARAM_FIXES = {
    "Read": {"path": "file_path", "filename": "file_path", "file": "file_path"},
    "Write": {"path": "file_path", "filename": "file_path", "file": "file_path"},
    "Edit": {"path": "file_path", "filename": "file_path", "file": "file_path",
             "search": "old_string", "find": "old_string", "replace": "new_string"},
    "Bash": {"cmd": "command", "script": "command", "shell": "command", "run": "command"},
    "Glob": {"glob": "pattern", "file_pattern": "pattern", "directory": "path"},
    "Grep": {"query": "pattern", "search": "pattern", "regex": "pattern", "directory": "path"},
    "Skill": {"name": "skill", "command": "skill", "skill_name": "skill"},
}

# Valid parameter names per tool — strip anything not in this set
VALID_PARAMS = {
    "Read": {"file_path", "limit", "offset", "pages"},
    "Write": {"file_path", "content"},
    "Edit": {"file_path", "old_string", "new_string", "replace_all"},
    "Bash": {"command", "description", "timeout", "run_in_background"},
    "Glob": {"pattern", "path"},
    "Grep": {"pattern", "path", "glob", "type", "output_mode", "head_limit",
             "offset", "context", "-A", "-B", "-C", "-i", "-n", "multiline"},
    "Skill": {"skill", "args"},
    "Agent": {"prompt", "description", "subagent_type", "name", "mode",
              "model", "isolation", "run_in_background"},
}


def fix_tool_call_params(tool_name: str, arguments: str) -> str:
    """Fix common parameter name mistakes and strip invalid params."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(args, dict):
            return arguments
    except (json.JSONDecodeError, TypeError):
        return arguments

    changed = False

    # Step 1: Rename known mistakes
    fixes = PARAM_FIXES.get(tool_name)
    if fixes:
        renamed = {}
        for k, v in args.items():
            if k in fixes:
                renamed[fixes[k]] = v
                changed = True
            else:
                renamed[k] = v
        args = renamed

    # Step 2: Strip parameters that don't belong to this tool
    valid = VALID_PARAMS.get(tool_name)
    if valid:
        stripped = {k: v for k, v in args.items() if k in valid}
        if len(stripped) != len(args):
            removed = set(args.keys()) - set(stripped.keys())
            proxy_log("PROXY", f"Stripped invalid params from {tool_name}: {removed}",
                      data={"tool": tool_name, "removed": list(removed)})
            args = stripped
            changed = True

    # Step 3: Shell command string repair — generic across interpreters
    # and file extensions. Universal shell rule: any absolute path with
    # whitespace MUST be quoted or the shell word-splits it. The fix
    # finds `<command-name> <unquoted-path-with-spaces>.<ext>` and
    # wraps the path in double quotes. No assumption about which
    # interpreter (bash/python/ruby/node/cat/cd/etc.) or which file
    # extension — works for any shell command shape.
    if tool_name == "Bash":
        cmd = args.get("command") if isinstance(args, dict) else None
        if isinstance(cmd, str):
            fixed_cmd = _quote_unquoted_paths_with_spaces(cmd)
            if fixed_cmd != cmd:
                args["command"] = fixed_cmd
                changed = True
                proxy_log("PROXY", f"Auto-quoted spaced path in shell command",
                          data={"tool": tool_name})

    if changed:
        proxy_log("PROXY", f"Fixed tool params for {tool_name}: {list(args.keys())}",
                  data={"tool": tool_name, "params": list(args.keys())})
        return json.dumps(args)
    return arguments if isinstance(arguments, str) else json.dumps(arguments)


# Match `<cmd> /path/with spaces/file.ext` for ANY command name and ANY
# file extension. The path must:
#   - start with `/` (absolute)
#   - not be already wrapped in single or double quotes
#   - end with `.<extension>` followed by whitespace or end-of-string
# Non-greedy match on the path body so `cmd /a.x /b.y` captures only the
# first path. We then check inside _sub() whether the captured path
# actually contains whitespace — if not, no fix needed and the regex
# match is left alone.
_UNQUOTED_PATH_WITH_SPACE_RE = re.compile(
    r"""(?x)
    (?P<pre>(?:^|\s|&&|;|\|)\s*)
    (?P<cmd>[a-zA-Z_][\w.-]*)
    \s+
    (?!['"-])
    (?P<path>/[^\n'"<>|;&]+?\.[a-zA-Z][\w-]*)
    (?=\s|$)
    """
)


def _quote_unquoted_paths_with_spaces(command: str) -> str:
    """If a shell command invokes any binary on an absolute path that
    contains whitespace and isn't already quoted, wrap that path in
    double quotes. Interpreter- and extension-agnostic. Universal shell
    rule: unquoted paths with whitespace are word-split by the shell."""
    if not command or "/" not in command:
        return command

    def _sub(m: re.Match) -> str:
        path = m.group("path")
        if " " not in path and "\t" not in path:
            return m.group(0)
        return f'{m.group("pre")}{m.group("cmd")} "{path}"'

    return _UNQUOTED_PATH_WITH_SPACE_RE.sub(_sub, command)


# ── Inlet Filters ─────────────────────────────────────────────────────────

ENABLE_TRUTH_GUARD = os.environ.get("ENABLE_TRUTH_GUARD", "true").lower() == "true"
ENABLE_MAP_SEARCH = os.environ.get("ENABLE_MAP_SEARCH", "true").lower() == "true"
ENABLE_LANG_DOCS = os.environ.get("ENABLE_LANG_DOCS", "true").lower() == "true"
# Host-mapped Kiwix port the model uses from inside Bash. krull-claude
# runs on the host, so docker hostnames like krull-kiwix don't resolve.
# This is also where Claude Code's WebFetch is BLOCKED (localhost is on
# its private-IP denylist) — that's why lang_docs teaches the model the
# Bash+curl shape rather than a WebFetch URL.
KIWIX_HOST_URL = os.environ.get("KIWIX_HOST_URL", "http://localhost:8090")
PHOTON_URL = os.environ.get("PHOTON_URL", "http://krull-photon:2322")
TILESERVER_URL = os.environ.get("TILESERVER_URL", "http://localhost:8070")


def _insert_after_system_messages(messages: list, content: str) -> list:
    """Insert a system message right after the contiguous run of existing
    system messages at the start of the list.

    Why not insert at position 0? Small models like the qwen 9B anchor on
    whatever they read first. If a filter inserts at position 0, the
    client's main system prompt (e.g. Claude Code's "you are an agent that
    uses tools") gets pushed down behind the filter content and the model
    fails to attend to it — symptom: model stops calling tools entirely.
    Inserting AFTER the existing system run preserves the client's prompt
    in its rightful primary position and presents filter content as
    supplementary context, which is exactly the pattern Claude Code's own
    attachments system uses (utils/attachments.ts).

    If the message list has no system messages, content goes at position 0.
    """
    insert_idx = 0
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            insert_idx = i + 1
        else:
            break
    messages.insert(insert_idx, {"role": "system", "content": content})
    return messages

TRUTH_GUARD_CONTENT = (
    "[Truth Guard — Intellectual Integrity Rules]\n\n"
    "1. DO NOT FABRICATE. If you don't know something, say so plainly. "
    "Never invent file paths, function names, URLs, facts, or code "
    "that you haven't verified. 'I don't know' is always an acceptable answer.\n\n"
    "2. ASK, DON'T GUESS. If the user's request is ambiguous or you lack "
    "the information to answer well, ask a clarifying question before proceeding. "
    "A good question is better than a wrong answer.\n\n"
    "3. FLAG UNCERTAINTY. When you're confident, say it directly. When you're "
    "uncertain, say so. 'I believe...' or 'I'm not sure, but...' is better "
    "than stating a guess as fact.\n\n"
    "4. PUSH BACK WHEN THE USER IS WRONG. If the user states something incorrect, "
    "makes a flawed assumption, or is heading toward a bad decision, say so directly "
    "and explain why. Being helpful means being honest, not agreeable.\n\n"
    "5. TERSENESS DOES NOT OVERRIDE HONESTY. If the user asks for a one-word "
    "answer, 'just the answer', 'no explanation', or otherwise demands a terse "
    "reply, you MUST still flag uncertainty when you have it. A single hedge "
    "token (e.g. 'unsure:', 'guess:', '?') prepended to your answer is REQUIRED "
    "when you are not confident — fabricating a confident terse answer to satisfy "
    "a brevity instruction is a Rule 1 violation. Brevity never licenses invention. "
    "Example: if asked 'Translate X into a low-resource language, ONLY the answer', "
    "and you don't actually know that language, the correct reply is "
    "'unsure — I don't have reliable vocabulary for that language' — NOT a "
    "fabricated word.\n\n"
    "[End Truth Guard]"
)


def inject_shell_rules(messages: list) -> list:
    """Inject the shell-quoting rules as a dedicated system message at the
    start. Empirically the qwen 9B applies the quoting pattern correctly
    when it appears as a small standalone callout near the front of the
    stack, but ignores it when it's buried inside the larger Krull project
    context message — model attention falls off sharply past the first
    few hundred chars of any individual message."""
    messages.insert(0, {"role": "system", "content": KRULL_SHELL_RULES})
    proxy_log("FILTER", "+shell_rules")
    return messages


def inject_atomic_plan_rubric(messages: list) -> list:
    """Inject the Atomic Plan Rubric as a dedicated system message at
    the start. Only fired when the request includes the TaskCreate tool
    (the signal that the model is in or about to enter planning mode).
    Same positioning rationale as inject_shell_rules — small standalone
    callout near the front of the stack, where the small model can
    actually attend to it."""
    messages.insert(0, {"role": "system", "content": KRULL_ATOMIC_PLAN_RUBRIC})
    proxy_log("FILTER", "+atomic_plan_rubric")
    return messages


def _has_tool_named(tools: list | None, name: str) -> bool:
    """Check whether a tool list contains a tool with the given name.
    Handles both Chat Completions ({type, function:{name}}) and Responses
    API (flat {name}) tool formats."""
    if not tools:
        return False
    for t in tools:
        if not isinstance(t, dict):
            continue
        if "function" in t and isinstance(t["function"], dict):
            if t["function"].get("name") == name:
                return True
        elif t.get("name") == name:
            return True
    return False


# Planning-lock helpers (task #15). Input-side enforcement that forces the
# model to call TaskCreate as its first action on plan-worthy queries.
# Replaces the failed "instruct the model to plan" approach (#14 alone) with
# a structural one: remove all non-planning tools from the request, leaving
# the model with no choice but to plan.

PLANNING_TOOL_NAMES = {"TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}


_SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL
)

# Claude Code emits slash commands in TWO different shapes the proxy
# needs to recognize, depending on conversation turn:
#
# Shape 1 — TURN 1 (initial slash command):
#   <command-message>study-prep</command-message>
#   <command-name>/study-prep</command-name>
#   <command-args>translate How are you?</command-args>
#   ... user input below ...
# This is the user's original input, wrapped in XML tags.
#
# Shape 2 — TURN 2+ (loaded skill content, after Skill tool invoked):
#   Base directory for this skill: /path/to/.claude/skills/study-prep
#
#   # Study Prep
#   ... full SKILL.md content ...
#
#   ARGUMENTS: translate How are you?
# This is a NEW USER MESSAGE Claude Code injects after the model invokes
# the Skill tool — it carries the loaded skill's full content. The
# original <command-name> tags are NOT present. We need a separate
# detector for this shape so the directive can be re-injected here too,
# otherwise the model sees the skill's surface docs ("for PDF files
# only") on turn 2 with no countervailing instruction and refuses.
_COMMAND_NAME_RE = re.compile(
    r"<command-name>\s*/([a-zA-Z][a-zA-Z0-9_-]*)\s*</command-name>"
)
_COMMAND_ARGS_RE = re.compile(
    r"<command-args>(.*?)</command-args>", re.DOTALL
)
_LOADED_SKILL_BASE_DIR_RE = re.compile(
    r"Base directory for this skill:[^\n]*?/skills/([a-zA-Z][a-zA-Z0-9_-]*)"
)
_LOADED_SKILL_FULL_PATH_RE = re.compile(
    r"Base directory for this skill:\s*([^\n]+?)\s*$", re.MULTILINE
)
_LOADED_SKILL_ARGUMENTS_RE = re.compile(
    r"ARGUMENTS:\s*([^\n]*)"
)


def _strip_system_reminders(content: str) -> str:
    """Remove all <system-reminder>...</system-reminder> blocks from a
    user message, leaving only the actual user-typed content.

    Claude Code wraps every user message in one or more <system-reminder>
    blocks (containing MCP server instructions, hook reminders, project
    context, etc.). On a fresh /study-prep call, the wrapper blocks add
    up to ~25 KB while the actual user input is ~30 chars. Stripping
    them is the only reliable way to find the actual user query."""
    return _SYSTEM_REMINDER_RE.sub("", content).strip()


def _parse_slash_command(content: str) -> tuple[str, str, str] | None:
    """If the user content represents a slash command in either of the
    two shapes Claude Code uses, return (skill_name, args, shape).
    Otherwise return None.

    Shape "initial": <command-name>/X</command-name> (turn 1, user input)
    Shape "loaded":  'Base directory for this skill: .../skills/X'
                     (turn 2+, loaded skill content injected as a new
                     user message after Skill is invoked)

    The shape determines which directive template to use:
      - initial → SLASH_COMMAND_PROTOCOL_TEMPLATE (forces Skill call)
      - loaded  → SLASH_COMMAND_FOLLOWTHROUGH_TEMPLATE (forces procedure)
    """
    cleaned = _strip_system_reminders(content)

    # Shape "initial": original slash command (turn 1)
    name_match = _COMMAND_NAME_RE.search(cleaned)
    if name_match:
        skill_name = name_match.group(1)
        args_match = _COMMAND_ARGS_RE.search(cleaned)
        args = args_match.group(1).strip() if args_match else ""
        return (skill_name, args, "initial")

    # Shape "loaded": loaded skill content (turn 2+)
    base_match = _LOADED_SKILL_BASE_DIR_RE.search(cleaned)
    if base_match:
        skill_name = base_match.group(1)
        args_match = _LOADED_SKILL_ARGUMENTS_RE.search(cleaned)
        args = args_match.group(1).strip() if args_match else ""
        return (skill_name, args, "loaded")

    return None


def _is_plan_worthy_query(messages: list) -> bool:
    """Heuristic: should the latest user message force planning?

    Strips Claude Code's <system-reminder> wrapper blocks first, then
    looks for a <command-name>/X</command-name> tag — Claude Code's
    representation of a slash command in the API request. These are
    explicit invocations of multi-step skills/workflows and are exactly
    the queries the small model fails on without planning. Casual
    queries (no command-name tag) are left alone so simple requests
    don't get force-planned.
    """
    last_user = None
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m
            break
    if not last_user:
        return False
    raw = _content_text(last_user.get("content", ""))
    user_input = _strip_system_reminders(raw)
    return bool(_COMMAND_NAME_RE.search(user_input))


# Slash command protocol directive (task #20).
# Appended to the END of the user message content (the highest-attention
# position the model reads before choosing its next action). Tells the
# model to invoke the Skill tool as its first action and follow the
# skill's procedure rather than bypassing it via Grep/Read shortcuts.
#
# REVERTED FROM MOTIVATIONAL VERSION (task #24, see commit history).
# We tried rewriting this with WHY-this-matters reasoning and concrete
# causal chains explaining the value of following the skill procedure.
# The result was strictly worse: where the coercive version made the
# model thrash through alternatives but eventually find the right path
# (read workflow, read grammar, find qʰata mayka? in pragmatic.md), the
# motivational version made the model invoke Skill once, decide it had
# "satisfied" the directive, and immediately produce a fabricated answer
# from training (it invented "Sapimih?" with a fake etymology).
#
# Lesson: small models like the qwen 9B don't have the meta-cognitive
# bandwidth to act on motivational reasoning. They follow concrete
# commands ("MUST do X") much better than abstract appeals to value
# ("here's why X matters"). The motivation gives them permission to
# feel done after mechanical compliance instead of a continuing
# obligation to act. We'll keep coercive directives for small models
# and not try to extend motivational language to the other filters.
SLASH_COMMAND_PROTOCOL_TEMPLATE = (
    "\n\n[Krull Slash Command Protocol]\n"
    "You received a /{skill_name} slash command. Your FIRST tool call "
    "MUST be:\n"
    "    Skill(skill=\"{skill_name}\", args=\"{args}\")\n"
    "\n"
    "Do NOT skip ahead by:\n"
    "  - Searching files directly with Grep or Glob\n"
    "  - Reading files manually before invoking the skill\n"
    "  - Producing the answer from training knowledge\n"
    "\n"
    "Even if you 'know' the answer, you MUST invoke the skill so the "
    "skill's procedure runs and each fact in your final answer has a "
    "verified source from the skill's defined process. Following the "
    "procedure is more important than reaching an answer fast. The "
    "skill's procedure is what makes the answer correct AND traceable.\n"
    "[End Krull Slash Command Protocol]"
)

# Used when the loaded skill content has just been injected (turn 2+
# of a slash command workflow). At this point the model has ALREADY
# invoked Skill — telling it to invoke Skill again would cause a loop.
# Instead, the directive points the model at the procedure file and
# tells it to follow it step by step, NOT to refuse based on a glance
# at the skill's surface description.
SLASH_COMMAND_FOLLOWTHROUGH_TEMPLATE = (
    "\n\n[Krull Skill Follow-Through]\n"
    "The /{skill_name} skill body has loaded. Task: {args}\n"
    "\n"
    "MUST DO:\n"
    "  1. Read the procedure file in the skill's references/ "
    "directory if one exists. Follow it step by step.\n"
    "  2. Each step is a real tool call producing a real result. "
    "Cite the result, not training knowledge.\n"
    "  3. If the procedure routes by input type (mode, direction, "
    "language), check the user's actual input and pick the matching "
    "branch — not the first-listed one.\n"
    "\n"
    "MUST NOT:\n"
    "  - Refuse the input because the skill's surface description "
    "looks like a mismatch. Run the procedure first.\n"
    "  - Fabricate words, definitions, citations, or facts. An honest "
    "'the procedure does not handle this input' beats a confident "
    "wrong answer.\n"
    "[End Krull Skill Follow-Through]"
)

# Meta-question detector: distinguishes "run this skill on X" from "tell me
# about this skill / can it do X?". When a user types /skill <meta-question>,
# we still want the Skill(...) invocation to happen (so the skill's content
# loads into context), but on the "loaded" turn we want the model to *answer
# the question* using the loaded content — not mechanically execute the
# procedure as if the question were the task. Generic: works for any skill.
_META_QUESTION_PREFIXES = (
    "what ", "what's ", "whats ",
    "can you ", "can it ", "can this ", "can the ",
    "could you ", "could it ",
    "how does ", "how do ", "how would ", "how can ",
    "why does ", "why do ",
    "does it ", "does this ", "does the ",
    "is it ", "is this ", "is the ",
    "tell me about", "tell me what",
    "will it ", "will this ", "will you ",
    "would it ", "would this ", "would you ",
    "are there ", "are these ",
    "which ",
)


def _looks_like_meta_question(args: str) -> bool:
    """Heuristic: do the args look like a question *about* the skill rather
    than a task *for* the skill?

    Used to pick between the strict follow-through template (execute the
    procedure) and the softer explain template (answer the question using
    the loaded skill content). Intentionally conservative — a false
    negative (treating a meta-question as a task) preserves current
    behavior; a false positive (treating a task as a meta-question) could
    cause the model to describe the skill instead of doing work, which is
    worse.
    """
    if not args:
        return False
    lowered = args.strip().lower()
    if not lowered:
        return False
    # Only the prefix list. A trailing "?" is NOT sufficient on its own —
    # task args can legitimately contain or end with a question (e.g.
    # /translate How are you doing?, /write-email reply to Sam asking
    # "did we ship yet?"). A misclassification here causes the model to
    # describe the skill instead of running it, which produces
    # hallucinated content — the worst failure mode. Prefer false
    # negatives (treat meta-question as task, run the procedure) over
    # false positives.
    for prefix in _META_QUESTION_PREFIXES:
        if lowered.startswith(prefix):
            return True
    return False


# Softer follow-through used on the "loaded" turn when the user's args look
# like a question *about* the skill. The skill's content has already been
# injected into context by Claude Code; the model's job is to answer the
# question using that content, not to run the procedure as if the question
# were a task.
SLASH_COMMAND_META_ANSWER_TEMPLATE = (
    "\n\n[Krull Skill Question]\n"
    "The /{skill_name} skill content has just loaded. The user's input "
    "looks like a QUESTION about this skill rather than a task for it:\n"
    "    {args}\n"
    "\n"
    "Answer the user's question directly, using:\n"
    "  - The skill's description and instructions that just loaded above\n"
    "  - Any reference files in the skill's directory, if the question "
    "needs details the description doesn't cover (read them with Read, "
    "don't guess)\n"
    "  - The skill's actual capabilities as described — not your "
    "assumptions about what a skill with this name might do\n"
    "\n"
    "Do NOT:\n"
    "  - Execute the skill's procedure as if the question were a task "
    "to run. The user is asking ABOUT the skill, not asking it to do "
    "work. Running the procedure here would be a mismatch.\n"
    "  - Refuse to answer because the procedure doesn't obviously apply. "
    "The question is answerable from the skill's description and "
    "reference files.\n"
    "  - Invent capabilities the skill doesn't document. If the skill's "
    "files don't answer the question, say so plainly.\n"
    "[End Krull Skill Question]"
)


# Condensed recency reminder injected as a system message near the END
# of the messages array when the full followthrough directive (above) is
# attached to a user message that's too far from the generation point.
#
# In Responses API conversations, there's typically only ONE user message
# near the top of the conversation. All subsequent turns are
# function_call / function_call_output pairs — no new user messages.
# As the conversation grows, the followthrough directive (appended to
# that early user message) drifts outside the model's effective attention
# window. The qwen 9B stops attending to content that's 40+ messages
# and 50K+ tokens before the generation point.
#
# This recency reminder is injected at the END of the messages array
# (right before the model generates) so it lands in the highest-attention
# slot. It's a condensed version — just enough for the model to know
# what it's supposed to be doing and that it should NOT stop early.
# Compact forcing directive injected at END of messages on every "loaded"
# turn for non-meta inputs. Complements the main SLASH_COMMAND_FOLLOWTHROUGH
# directive (appended to the user message, ~2042 chars) by putting the
# single most important instruction — "don't refuse, run the procedure" —
# in the highest-attention slot. Empirically: the 9B model sometimes
# refuses the user's input as "out of scope" on the loaded turn despite the
# followthrough's explicit anti-refusal clause being in context. The
# clause was getting drowned in the larger directive. This short reminder
# lands right before the model generates, where it cannot be ignored.
SLASH_COMMAND_FORCING_DIRECTIVE = (
    "[Skill Execution — Next Action]\n"
    "Executing /{skill_name} on: {args}\n"
    "\n"
    "Your next action MUST be a tool call advancing the procedure. "
    "Not a refusal. Not a menu. Not a meta-comment about scope.\n"
    "[End Skill Execution]"
)

RECENCY_REMINDER_THRESHOLD = 15  # messages between user msg and end
SLASH_COMMAND_RECENCY_REMINDER = (
    "[Active Skill — Continue Working]\n"
    "You are executing the /{skill_name} skill. Task: {args}\n"
    "Your last tool call returned results. You MUST continue: read "
    "any available reference files, follow the skill's procedure, "
    "and produce a complete answer. Do NOT stop with a brief status "
    "update or a single sentence. If a specific procedure file was "
    "not found, use the reference files that ARE available.\n"
    "\n"
    "If the procedure supports multiple directions or modes, route "
    "based on the user's actual input — don't default to whichever "
    "workflow appears first in the file. Do not refuse the input "
    "because the skill's dominant framing feels like a mismatch; "
    "check for a detection/routing section and follow the matching "
    "branch."
)


def inject_slash_command_protocol(messages: list) -> list:
    """If the latest user message represents a slash command (in either
    shape), append the appropriate directive to the END of the user
    content.

    Two shapes, two templates:
      - "initial" (turn 1, original slash command): inject the
        SLASH_COMMAND_PROTOCOL_TEMPLATE which forces Skill invocation
        as the first action.
      - "loaded" (turn 2+, loaded skill content message): inject the
        SLASH_COMMAND_FOLLOWTHROUGH_TEMPLATE which tells the model to
        follow the skill's procedure file step by step instead of
        refusing based on the skill's surface description.

    End-of-message position is chosen because it's the highest-attention
    slot — it's the last thing the model reads before deciding its next
    action. Handles both string and list content formats. Idempotent:
    skips if either directive marker is already present in the content.
    """
    if not messages:
        return messages
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return messages

    raw_content = messages[last_user_idx].get("content", "")
    flat_text = _content_text(raw_content)
    parsed = _parse_slash_command(flat_text)
    if parsed is None:
        return messages
    skill_name, args, shape = parsed

    # Idempotent: don't double-inject either directive
    if "[Krull Slash Command Protocol]" in flat_text:
        return messages
    if "[Krull Skill Follow-Through]" in flat_text:
        return messages
    if "[Krull Skill Question]" in flat_text:
        return messages

    # Pick the right template for this shape
    escaped_args = args.replace('"', '\\"')
    if shape == "initial":
        directive = SLASH_COMMAND_PROTOCOL_TEMPLATE.format(
            skill_name=skill_name, args=escaped_args
        )
        log_label = "+slash_command_protocol"
    else:  # shape == "loaded"
        if _looks_like_meta_question(args):
            directive = SLASH_COMMAND_META_ANSWER_TEMPLATE.format(
                skill_name=skill_name, args=escaped_args
            )
            log_label = "+slash_command_meta_answer"
        else:
            directive = SLASH_COMMAND_FOLLOWTHROUGH_TEMPLATE.format(
                skill_name=skill_name, args=escaped_args
            )
            log_label = "+slash_command_followthrough"

    # Modify the message in place, preserving the original content format
    new_msg = dict(messages[last_user_idx])
    if isinstance(raw_content, str):
        new_msg["content"] = raw_content + directive
    elif isinstance(raw_content, list):
        # Append to the LAST text-like part. If none exists, add a new one.
        new_parts = list(raw_content)
        appended = False
        for i in range(len(new_parts) - 1, -1, -1):
            part = new_parts[i]
            if isinstance(part, dict) and part.get("type") in ("input_text", "text", "output_text"):
                new_part = dict(part)
                new_part["text"] = new_part.get("text", "") + directive
                new_parts[i] = new_part
                appended = True
                break
        if not appended:
            new_parts.append({"type": "input_text", "text": directive})
        new_msg["content"] = new_parts
    else:
        return messages
    messages[last_user_idx] = new_msg

    proxy_log("FILTER", f"{log_label} (/{skill_name}, shape={shape}, "
              f"+{len(directive)} chars to user msg)",
              data={"skill": skill_name, "shape": shape, "chars": len(directive)})

    # Recency reminder: if the user message is far from the generation
    # point (end of messages), the model can't attend to the directive
    # we just appended. Inject a condensed reminder as a system message
    # at the END so it lands in the highest-attention slot.
    #
    # In Responses API conversations there's typically only ONE user
    # message near the top. But when a skill loads, Claude Code injects
    # a second user message with the skill content, which can be close
    # to the end. Only fire the reminder when the user message is
    # genuinely far from the generation point.
    # Skip the recency reminder for meta-question turns — the reminder
    # pushes "continue working / follow the procedure", which is wrong
    # when the user's input is a question about the skill.
    distance = len(messages) - 1 - last_user_idx
    if shape == "loaded" and distance >= RECENCY_REMINDER_THRESHOLD \
            and not _looks_like_meta_question(args):
        reminder = SLASH_COMMAND_RECENCY_REMINDER.format(
            skill_name=skill_name, args=escaped_args
        )
        messages.append({"role": "system", "content": reminder})
        proxy_log("FILTER", f"+skill_recency_reminder (/{skill_name}, "
                  f"distance={distance}, +{len(reminder)} chars at end)",
                  data={"skill": skill_name, "distance": distance,
                        "chars": len(reminder)})

    # Forcing directive: fires on EVERY loaded turn for non-meta inputs
    # regardless of distance. Short, placed at end-of-messages for
    # maximum attention. Complements the longer followthrough directive
    # by putting the anti-refusal instruction in the highest-attention
    # slot. Skipped on meta-question turns (model is answering, not
    # executing). Idempotent: only fire if not already present.
    if shape == "loaded" and not _looks_like_meta_question(args):
        already_has = any(
            m.get("role") == "system"
            and "[Skill Execution — Next Action]" in _content_text(m.get("content", ""))
            for m in messages
        )
        if not already_has:
            forcing = SLASH_COMMAND_FORCING_DIRECTIVE.format(
                skill_name=skill_name, args=escaped_args
            )
            messages.append({"role": "system", "content": forcing})
            proxy_log("FILTER", f"+slash_command_forcing (/{skill_name}, "
                      f"+{len(forcing)} chars at end)",
                      data={"skill": skill_name, "chars": len(forcing)})

        # Active-skill resource manifest: when the skill body has just
        # loaded, the project context message goes static and drops the
        # discovery listing (chars dropped to ~360). The model loses
        # sight of the skill's resource layout exactly when it most
        # needs to plan which sub-resources to consult. Re-surface a
        # manifest scoped to the active skill — same discovery principle
        # Claude Code uses pre-invocation, applied to the post-invocation
        # turn. Generic: lists whatever subdirs the skill author created,
        # no special-casing of names.
        already_has_manifest = any(
            m.get("role") == "system"
            and "[Active Skill Resources]" in _content_text(m.get("content", ""))
            for m in messages
        )
        if not already_has_manifest:
            manifest_text = _build_active_skill_manifest(
                _content_text(raw_content), skill_name,
            )
            if manifest_text:
                messages.append({"role": "system", "content": manifest_text})
                proxy_log("FILTER", f"+active_skill_manifest (/{skill_name}, "
                          f"+{len(manifest_text)} chars at end)",
                          data={"skill": skill_name, "chars": len(manifest_text)})

        # BM25-retrieved top-K passages from the skill's markdown, scored
        # against the user's query (the skill args). Mechanical IR: no
        # LLM digest, no directory heuristics, no content-type bias.
        # Purpose: when the model must discriminate among sibling files
        # to find a pattern or idiom, put the likely-matching passages
        # directly in context so the model matches text-to-text rather
        # than filename-to-input. Bounded to stay under the qwen-9B
        # working-attention ceiling (~28K token cap).
        already_has_hits = any(
            m.get("role") == "system"
            and "[Active Skill Passages]" in _content_text(m.get("content", ""))
            for m in messages
        )
        if not already_has_hits and args:
            passages_text, hits = _build_active_skill_passages(
                _content_text(raw_content), skill_name, args,
            )
            if passages_text:
                messages.append({"role": "system", "content": passages_text})
                proxy_log("FILTER", f"+active_skill_passages (/{skill_name}, "
                          f"{len(hits)} chunks, +{len(passages_text)} chars)",
                          data={"skill": skill_name,
                                "chunks": len(hits),
                                "chars": len(passages_text),
                                "top_paths": [h for h in hits]})

    return messages


_PASSAGE_SKIP_NAMES = {
    "__pycache__", "node_modules", ".venv", "venv", ".git",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}
_PASSAGE_MARKDOWN_SUFFIXES = (".md", ".markdown")
_PASSAGE_MAX_DEPTH = 4
_PASSAGE_CHUNK_MIN_BYTES = 80
_PASSAGE_CHUNK_MAX_BYTES = 1500
_PASSAGE_TOP_K = 3
_PASSAGE_TOTAL_BYTES_MAX = 4500

_PASSAGE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*")


def _tokenize_for_bm25(text: str) -> list[str]:
    """Lowercase alphanumeric-run tokenizer. Keeps hyphenated tokens
    intact (e.g. 'cache-read'). No stopword filter — BM25's IDF already
    down-weights common terms, and hard stopword removal can kill
    short-query matches (e.g. 'how are you' against greetings chunks
    when how/are/you are all filtered). ASCII-only by design: non-ASCII
    scripts are visible literally in the passage body but don't
    participate in BM25 scoring."""
    return [
        t for t in (m.group(0).lower() for m in _PASSAGE_TOKEN_RE.finditer(text))
        if len(t) > 1
    ]


def _split_markdown_into_chunks(path: Path, rel_path: str) -> list[dict]:
    """Split a markdown file into heading-anchored chunks. Each chunk
    carries the text body, file-relative line range, and the heading
    path it sits under. Chunks larger than the max byte cap are
    further split on paragraph boundaries."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    chunks: list[dict] = []
    heading_stack: list[str] = []
    buf: list[str] = []
    buf_start = 1
    buf_heading: list[str] = []

    def flush() -> None:
        if not buf:
            return
        body = "\n".join(buf).strip()
        if len(body) < _PASSAGE_CHUNK_MIN_BYTES:
            return
        segments = _split_oversize_chunk(body) if len(body) > _PASSAGE_CHUNK_MAX_BYTES else [body]
        offset = 0
        for seg in segments:
            if len(seg) < _PASSAGE_CHUNK_MIN_BYTES:
                continue
            start_line = buf_start + offset
            end_line = start_line + seg.count("\n")
            chunks.append({
                "rel_path": rel_path,
                "heading": " › ".join(buf_heading) if buf_heading else "(top)",
                "start_line": start_line,
                "end_line": end_line,
                "body": seg,
            })
            offset += seg.count("\n") + 1

    for i, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            flush()
            buf = []
            buf_start = i
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped.lstrip("#").strip()
            heading_stack = heading_stack[: max(0, level - 1)] + [title]
            buf_heading = list(heading_stack)
            continue
        if not buf:
            buf_start = i
            buf_heading = list(heading_stack)
        buf.append(line)
    flush()
    return chunks


def _split_oversize_chunk(body: str) -> list[str]:
    """Split a chunk larger than the max cap on blank lines, then fall
    back to single-line segments if no paragraph break helps."""
    paragraphs = re.split(r"\n\s*\n", body)
    out: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = (current + "\n\n" + para).strip() if current else para.strip()
        if len(candidate) <= _PASSAGE_CHUNK_MAX_BYTES:
            current = candidate
        else:
            if current:
                out.append(current)
            if len(para) <= _PASSAGE_CHUNK_MAX_BYTES:
                current = para.strip()
            else:
                for line in para.splitlines():
                    if line.strip():
                        out.append(line.strip()[:_PASSAGE_CHUNK_MAX_BYTES])
                current = ""
    if current:
        out.append(current)
    return out


def _iter_skill_markdown_for_passages(base: Path) -> list[tuple[Path, str]]:
    """Depth-bounded walk yielding (path, rel_path) for every markdown
    file under the skill tree. No skill-specific name is filtered."""
    results: list[tuple[Path, str]] = []

    def walk(dir_path: Path, depth: int) -> None:
        if depth > _PASSAGE_MAX_DEPTH:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: p.name.lower())
        except (PermissionError, OSError):
            return
        for entry in entries:
            if entry.name.startswith(".") or entry.name in _PASSAGE_SKIP_NAMES:
                continue
            if entry.is_dir():
                walk(entry, depth + 1)
            elif entry.is_file() and entry.suffix.lower() in _PASSAGE_MARKDOWN_SUFFIXES:
                try:
                    rel = str(entry.relative_to(base))
                except ValueError:
                    continue
                results.append((entry, rel))

    walk(base, 0)
    return results


def _bm25_score(
    query_tokens: list[str],
    chunks: list[dict],
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[float, dict]]:
    """Standard BM25 scoring across pre-tokenized chunks. Chunks must
    have a `tokens` field set before calling. Returns (score, chunk)
    pairs sorted high-to-low."""
    if not chunks or not query_tokens:
        return []
    n_docs = len(chunks)
    avgdl = sum(len(c["tokens"]) for c in chunks) / n_docs if n_docs else 0
    query_set = set(query_tokens)
    df: dict[str, int] = {}
    for c in chunks:
        for t in set(c["tokens"]) & query_set:
            df[t] = df.get(t, 0) + 1
    import math
    idf = {
        t: math.log((n_docs - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5) + 1)
        for t in query_set
    }
    scored: list[tuple[float, dict]] = []
    for c in chunks:
        dl = len(c["tokens"])
        if dl == 0:
            continue
        tf_counts: dict[str, int] = {}
        for t in c["tokens"]:
            if t in query_set:
                tf_counts[t] = tf_counts.get(t, 0) + 1
        score = 0.0
        for t, tf in tf_counts.items():
            numer = tf * (k1 + 1)
            denom = tf + k1 * (1 - b + b * dl / avgdl) if avgdl else tf + k1
            score += idf.get(t, 0.0) * numer / denom
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return scored


def _build_active_skill_passages(
    loaded_content: str, skill_name: str, query: str,
) -> tuple[str, list[str]]:
    """BM25-retrieve the top-K passages from the active skill's markdown
    against the user's query. Returns (injected_text, list_of_rel_paths).
    Pure IR: chunks are heading-anchored sections of the author's own
    markdown, scoring is standard BM25, no directory or filename
    heuristics enter the ranking."""
    cleaned = _strip_system_reminders(loaded_content)
    m = _LOADED_SKILL_FULL_PATH_RE.search(cleaned)
    if not m:
        return "", []
    base = Path(m.group(1).strip())
    if not base.is_dir():
        return "", []
    query_tokens = _tokenize_for_bm25(query)
    if not query_tokens:
        return "", []

    all_chunks: list[dict] = []
    for path, rel in _iter_skill_markdown_for_passages(base):
        for ch in _split_markdown_into_chunks(path, rel):
            ch["tokens"] = _tokenize_for_bm25(ch["body"])
            all_chunks.append(ch)
    if not all_chunks:
        return "", []

    ranked = _bm25_score(query_tokens, all_chunks)
    if not ranked:
        return "", []

    selected: list[dict] = []
    total_bytes = 0
    seen_paths: set[str] = set()
    for score, ch in ranked:
        if len(selected) >= _PASSAGE_TOP_K:
            break
        body_len = len(ch["body"])
        if total_bytes + body_len > _PASSAGE_TOTAL_BYTES_MAX and selected:
            break
        key = (ch["rel_path"], ch["start_line"])
        if key in seen_paths:
            continue
        seen_paths.add(key)
        ch["score"] = score
        selected.append(ch)
        total_bytes += body_len

    if not selected:
        return "", []

    blocks: list[str] = []
    paths: list[str] = []
    for ch in selected:
        header = (
            f"----- {ch['rel_path']}:{ch['start_line']}-{ch['end_line']} "
            f"[{ch['heading']}] score={ch['score']:.2f} -----"
        )
        blocks.append(f"{header}\n{ch['body']}")
        paths.append(f"{ch['rel_path']}:{ch['start_line']}")

    body = (
        f"[Active Skill Passages]\n"
        f"BM25 retrieval on query {query!r} over the /{skill_name} "
        f"skill's markdown returned these top {len(selected)} passages. "
        f"Scoring is mechanical (query-term frequency + inverse document "
        f"frequency); ranking has no knowledge of file or directory names. "
        f"Treat these as relevant candidates, not authoritative — verify "
        f"by reading the cited file:line ranges when you cite them.\n\n"
        + "\n\n".join(blocks)
        + "\n[End Active Skill Passages]"
    )
    return body, paths


def _build_active_skill_manifest(loaded_content: str, skill_name: str) -> str:
    """Return a short system-message body listing the active skill's
    top-level subdirectories with file counts. Reads the skill's base
    directory from disk via the path Claude Code embeds in the loaded
    content. Returns empty string if the path can't be located or the
    directory has no useful structure."""
    cleaned = _strip_system_reminders(loaded_content)
    m = _LOADED_SKILL_FULL_PATH_RE.search(cleaned)
    if not m:
        return ""
    base = Path(m.group(1).strip())
    if not base.is_dir():
        return ""
    try:
        sub_entries = sorted(base.iterdir())
    except (PermissionError, OSError):
        return ""
    lines = []
    for sub in sub_entries:
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        try:
            files = [p for p in sub.iterdir() if p.is_file()]
        except (PermissionError, OSError):
            continue
        if not files:
            continue
        ext_counts: dict[str, int] = {}
        for f in files:
            ext = f.suffix.lower() or "(noext)"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
        ext_summary = ", ".join(
            f"{n} {e.lstrip('.') or '(noext)'}"
            for e, n in sorted(ext_counts.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"  - {sub.name}/  ({ext_summary})")
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        f"[Active Skill Resources]\n"
        f"The /{skill_name} skill body has loaded. Its top-level "
        f"resource layout on disk is:\n"
        f"{body}\n"
        f"\n"
        f"Read, Glob, or Grep these as the skill's procedure directs. "
        f"When the procedure references a category of resource by name "
        f"without listing exact filenames, the matching subdirectory "
        f"above is where to look.\n"
        f"[End Active Skill Resources]"
    )


def _history_has_task_create(messages: list) -> bool:
    """Has the model already called TaskCreate in this conversation?
    If yes, planning has already happened and we should not lock the
    tool list — the model needs the full toolset to execute the plan."""
    for m in messages:
        if m.get("role") != "assistant":
            continue
        tcs = m.get("tool_calls") or []
        for tc in tcs:
            func = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(func, dict) and func.get("name") == "TaskCreate":
                return True
    return False


def _narrow_to_planning_tools(tools: list) -> list:
    """Return a filtered tool list containing only planning tools.
    Handles both Chat Completions and Responses API tool formats."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if "function" in t and isinstance(t["function"], dict):
            name = t["function"].get("name", "")
        else:
            name = t.get("name", "")
        if name in PLANNING_TOOL_NAMES:
            out.append(t)
    return out


# Loop detection + adaptive temperature (task #21).
#
# At temperature=0 the small model is deterministic, which is great for
# /english-to-cw but disastrous when the model picks a "shortest path"
# strategy that happens to loop. Observed failure: model brute-forced
# wol.jw.org publication IDs by incrementing the last digit, calling
# Bash 15+ times in a row at temp=0. The deterministic next action was
# always "increment by 1, try again."
#
# Fix: detect tool-call loops, inject a system reminder, AND elevate
# temperature for the next request based on how stuck the loop is.
# Higher temperature gives the model variance to break out. Once the
# model picks a different tool the loop counter resets and temp returns
# to 0. Generic: works for any tool, any skill, any project.

LOOP_DETECTION_THRESHOLD = 5  # consecutive same-tool calls = loop


def detect_tool_loop(messages: list) -> tuple[str, int] | None:
    """Scan the assistant tool calls in the message history. If the most
    recent N (>= LOOP_DETECTION_THRESHOLD) all use the same tool name,
    return (tool_name, count). Otherwise return None.

    Tool name only — we don't compare arguments. A model legitimately
    calling Read on 10 different files would still be flagged, but
    that's the right call: the rubric tells the model to do small
    discrete tasks, and 10 sequential Reads is exactly the kind of
    pattern that should make it pause and consider whether a different
    tool (Glob, Grep) would be more efficient.
    """
    tool_names: list[str] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        tcs = m.get("tool_calls") or []
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") if isinstance(tc.get("function"), dict) else None
            if not func:
                continue
            name = func.get("name", "")
            if name:
                tool_names.append(name)

    if len(tool_names) < LOOP_DETECTION_THRESHOLD:
        return None

    # Walk backward from the end and count consecutive matches of the
    # most recent tool name.
    last = tool_names[-1]
    count = 0
    for name in reversed(tool_names):
        if name == last:
            count += 1
        else:
            break
    if count >= LOOP_DETECTION_THRESHOLD:
        return (last, count)
    return None


LOOP_BREAK_HINT_TEMPLATE = (
    "[Krull Loop Break]\n"
    "You have called the {tool_name} tool {count} times in a row. "
    "This pattern is not making progress. STOP this approach and "
    "reconsider:\n"
    "  - What information do you ACTUALLY need to answer the user?\n"
    "  - Is there a DIFFERENT tool that could find it more directly?\n"
    "  - Are you brute-forcing something that should be looked up?\n"
    "  - Should you stop and ask the user for guidance?\n"
    "\n"
    "Do NOT call {tool_name} again on your next turn unless you are "
    "calling it with a fundamentally different intent. Try a different "
    "tool, a different approach, or honestly tell the user what you "
    "tried and why it isn't working.\n"
    "[End Krull Loop Break]"
)


def inject_loop_break(messages: list) -> list:
    """If a tool-call loop is detected, inject a system reminder telling
    the model to stop and reconsider. Position: after Claude Code's
    system prompt (same pattern as project context and hook error hint).
    Idempotent — won't double-inject if the hint is already present."""
    loop = detect_tool_loop(messages)
    if loop is None:
        return messages
    tool_name, count = loop
    # Idempotent: don't stack multiple loop break hints
    for m in messages:
        if m.get("role") == "system" and "[Krull Loop Break]" in _content_text(m.get("content", "")):
            return messages
    hint = LOOP_BREAK_HINT_TEMPLATE.format(tool_name=tool_name, count=count)
    proxy_log("FILTER", f"+loop_break ({tool_name} × {count} → injecting reminder)",
              data={"tool": tool_name, "count": count})
    return _insert_after_system_messages(messages, hint)


def compute_session_temperature(messages: list) -> float:
    """Pick the right sampling temperature for this request based on the
    conversation state. Default 0.0 (deterministic). Two triggers for
    escalation:

      1. Tool-call loop (same tool N times in a row) — strongest signal
         that the model is making the same mistake repeatedly.
      2. Stalled progress (N consecutive tool turns without a text
         answer) — catches the spelunking case where the model uses
         varied tools but still isn't converging.

    The strongest applicable escalation wins. Returns 0.0 for normal
    operation. Returns > 0 only when intervention is needed. The
    request handler passes the result through to Ollama via
    chat_body['temperature']."""
    loop = detect_tool_loop(messages)
    if loop is not None:
        _tool, count = loop
        if count >= 13:
            return 0.6
        if count >= 8:
            return 0.4
        return 0.2

    stats = count_tool_call_stats(messages)
    stalled = stats["consecutive"]
    if stalled >= KRULL_STALLED_PROGRESS_THRESHOLD:
        # Escalate proportionally to how stuck the workflow is
        if stalled >= KRULL_STALLED_PROGRESS_THRESHOLD + 6:  # ≥18
            return 0.6
        if stalled >= KRULL_STALLED_PROGRESS_THRESHOLD + 3:  # ≥15
            return 0.4
        return 0.2  # 12-14 stalled turns — light nudge

    return 0.0


# Data starvation detection (task #22). When the model's recent tool
# calls have all failed/errored, the small model tends to give up and
# produce a confident-sounding answer from training instead of refusing
# honestly. Truth Guard tells it not to fabricate but is too abstract to
# act on. We need a CONCRETE, IMMEDIATE warning at the moment the
# starvation state is detected: "you tried X, Y, Z and they all failed,
# you have no data, refuse honestly."
#
# Detection is heuristic — we look at the most recent N tool results
# and check each against a set of failure patterns (timeouts, hook
# errors, HTTP errors, empty results, invalid params, etc.). If the
# failure rate is high enough, we inject the warning.

# Patterns that indicate a tool result is a failure rather than useful
# data. Conservative: we'd rather miss a real failure than false-alarm
# on a legitimate result.
_FAILURE_PATTERNS = [
    re.compile(r"PreToolUse:.*hook error", re.IGNORECASE),
    re.compile(r"timeout of \d+ms exceeded", re.IGNORECASE),
    re.compile(r"\btimed out\b", re.IGNORECASE),
    re.compile(r"\bInvalid tool parameters\b", re.IGNORECASE),
    re.compile(r"\bPage Not Found\b", re.IGNORECASE),
    re.compile(r"<tool_use_error>", re.IGNORECASE),
    re.compile(r"^Error:", re.MULTILINE),
    re.compile(r"\bFile does not exist\b", re.IGNORECASE),
    re.compile(r"\bNo such file or directory\b", re.IGNORECASE),
    re.compile(r"^Exit code [1-9]", re.MULTILINE),  # non-zero exit
]


def _is_failure_result(content: str) -> bool:
    """Heuristic: does this tool result look like a failure?

    Two ways to fail: known failure patterns (regex match), or content
    is suspiciously short (< 10 chars after stripping — probably an
    empty result or a one-word error). Conservative thresholds —
    we'd rather miss a real failure than false-alarm legitimate output.
    """
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if len(stripped) < 10:
        return True
    for pat in _FAILURE_PATTERNS:
        if pat.search(stripped):
            return True
    return False


def _count_recent_tool_failures(messages: list, n: int = 5) -> tuple[int, int, list[str]]:
    """Walk the messages from the end, find the last N tool results,
    and count how many look like failures. Returns (failures, examined,
    failure_summaries)."""
    tool_results = []
    for m in reversed(messages):
        if m.get("role") == "tool":
            tool_results.append(m)
            if len(tool_results) >= n:
                break

    failures = 0
    summaries: list[str] = []
    # We walked backward; reverse so summaries are in chronological order
    for m in reversed(tool_results):
        content = _content_text(m.get("content", ""))
        if _is_failure_result(content):
            failures += 1
            snippet = content.strip()[:90].replace("\n", " ")
            summaries.append(snippet)
    return (failures, len(tool_results), summaries)


DATA_STARVATION_HINT_TEMPLATE = (
    "[Krull Data Starvation Warning]\n"
    "Your last {failures} of {total} tool calls have failed or returned "
    "no useful data:\n"
    "{failure_summary}\n"
    "\n"
    "You DO NOT have the information needed to produce a verified "
    "answer to the user's question. You MUST NOT produce a final "
    "answer from training knowledge. Doing so creates plausible-"
    "sounding but potentially wrong content that the user cannot "
    "easily verify — that is the worst possible failure mode for an "
    "assistant claiming to be grounded in real sources.\n"
    "\n"
    "Instead, on your next turn, do ONE of the following:\n"
    "  1. Tell the user honestly: 'I tried [list of attempts] and "
    "they failed because [reasons]. I do not have the data to answer "
    "this confidently. Here is what I know I CANNOT verify: [...]'\n"
    "  2. Ask the user for guidance on a different approach\n"
    "  3. Try a fundamentally DIFFERENT tool you have not yet tried\n"
    "\n"
    "Do NOT invent details, scriptures, citations, definitions, "
    "names, dates, or facts. An honest 'I could not retrieve this' "
    "is always better than a confident wrong answer.\n"
    "[End Krull Data Starvation Warning]"
)


def inject_data_starvation_warning(messages: list) -> list:
    """If the model's recent tool calls are mostly failures, inject a
    concrete warning telling it to refuse honestly instead of inventing.
    Position: after Claude Code's system prompt (same pattern as the
    other 'detect bad state and warn' filters). Idempotent."""
    failures, total, summaries = _count_recent_tool_failures(messages, n=5)
    if total < 3:
        # Not enough recent tool results to make a confident judgment
        return messages
    if failures < total * 0.6:
        # Less than 60% failure rate — not a starvation state
        return messages

    # Idempotent — don't stack warnings if one is already in the messages
    for m in messages:
        if m.get("role") == "system" and "[Krull Data Starvation Warning]" in _content_text(m.get("content", "")):
            return messages

    summary_lines = "\n".join(f"  - {s}" for s in summaries)
    hint = DATA_STARVATION_HINT_TEMPLATE.format(
        failures=failures,
        total=total,
        failure_summary=summary_lines,
    )
    proxy_log("FILTER", f"+data_starvation_warning ({failures}/{total} recent tool calls failed)",
              level="warn", data={"failures": failures, "total": total})
    return _insert_after_system_messages(messages, hint)


# ── Synthesis directive ─────────────────────────────────────────────────
#
# Complementary to data starvation (which fires when tools FAIL): this
# fires when tools SUCCEED but the model might still hedge instead of
# synthesizing from the data it has.
#
# Observed pattern on qwen 9B: the model reads 3-4 substantial files,
# has all the information it needs, but produces a hedging meta-answer
# ("the materials don't specify...", "check Canvas for details...") instead
# of extracting and citing the specific content from its tool results.
# Claude Opus, given the same data through the same pipeline, synthesizes
# a detailed specific answer.
#
# This is a behavioral gap, not a data gap. The nudge tells the model
# HOW to use the data: extract specifics, cite details, don't defer to
# external systems when the retrieved content already contains the answer.
#
# Detection: ≥3 non-failure tool results with >200 chars each AND no
# substantial text answer (>500 chars without tool_calls) produced yet.
# This means it fires starting from the turn where the model has enough
# data to work with and hasn't yet given a real answer — typically the
# "final synthesis" turn.
#
# Position: appended at the END of the messages array. This is the
# highest-attention slot — the last thing the model reads before
# generating. Same technique as the slash command recency reminder.

SYNTHESIS_DIRECTIVE = (
    "[Krull Synthesis Directive]\n"
    "Your tool calls have returned substantial content. Your NEXT "
    "response MUST be text output that answers the user's request. "
    "Do NOT emit more tool calls on this turn. Do NOT emit an empty "
    "response. Write the answer now.\n"
    "\n"
    "When you write:\n"
    "- Extract and cite SPECIFIC details from the tool results: "
    "vocabulary, names, dates, definitions, steps, code, line numbers\n"
    "- If the retrieved content contains the answer to the user's "
    "question, give that answer directly and specifically\n"
    "- Do NOT say 'the materials don't specify' or 'check [external "
    "system]' when the content you retrieved actually does contain "
    "the information — reread your tool results above\n"
    "- Structure your answer around the specific content you found, "
    "not around meta-commentary about what the materials do or "
    "don't contain\n"
    "- If a tool returned many candidates (e.g., multiple matches "
    "per input), pick the best one per input and synthesize — do "
    "not list them all, do not call the tool again with different "
    "arguments\n"
    "- If the data genuinely does not contain the answer, say "
    "specifically what IS in the data and what is missing\n"
    "[End Krull Synthesis Directive]"
)


def _count_substantial_tool_results(messages: list) -> int:
    """Count tool results with substantial (non-failure) content.

    'Substantial' means ≥200 chars of non-failure content — enough to
    contain real information rather than just an error message or a
    tiny snippet. Used by the synthesis directive to decide whether
    the model has accumulated enough data to synthesize from.
    """
    count = 0
    for m in messages:
        if m.get("role") != "tool":
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        if len(content) < 200:
            continue
        if _is_failure_result(content):
            continue
        count += 1
    return count


def _last_tool_result_size(messages: list) -> int:
    """Return the size (in chars) of the most recent tool result, or 0.

    Used by the synthesis directive to trigger on a single large result
    — one big dump is enough to overwhelm a small model's working
    attention and cause it to stall, even if prior turns had small
    results.
    """
    for m in reversed(messages):
        if m.get("role") != "tool":
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            return 0
        if _is_failure_result(content):
            return 0
        return len(content)
    return 0


def _has_substantial_text_answer(messages: list) -> bool:
    """Has the model already produced a substantial text answer?

    A 'substantial text answer' is an assistant message with >500 chars
    and no tool_calls — meaning the model actually wrote a real response
    rather than a filler line between tool calls. Used to avoid injecting
    the synthesis directive after the model has already given its answer.
    """
    for m in messages:
        if m.get("role") != "assistant":
            continue
        if m.get("tool_calls"):
            continue
        content = _content_text(m.get("content", "") or "")
        if len(content) > 500:
            return True
    return False


# ── Grounded-answer pass (structured-output fabrication guard) ──────────
#
# Generic small-model fabrication guard, modeled on Claude Code's
# SyntheticOutputTool (tools/SyntheticOutputTool/SyntheticOutputTool.ts).
# That tool dynamically constructs a JSON schema and forces the model to
# call it as the final action — schema validation guarantees the output
# shape regardless of what the model would have free-text-generated.
#
# We use the same mechanism (Ollama's `format` parameter, empirically
# confirmed to enforce JSON schema on qwen 9B without skill-specific
# coupling) at the moment the model is expected to produce its final
# answer. The schema is universal:
#
#   {answer: string, sources_used: [string, ...]}
#
# `sources_used` is the structural pressure: the model must commit to
# which tool result content backs its answer. The proxy then verifies
# each cited string is a real substring of any tool result in the
# conversation. Citations that don't match real content are flagged.
#
# Why this passes the structural test:
#   - Schema is content-type-agnostic (works for translation, code review,
#     math, writing — every skill has answers and may or may not cite
#     tool results)
#   - No skill-shape assumption (no procedure parsing, no subdir
#     enforcement, no required helper invocation)
#   - Citation verification is mechanical (substring match), no semantic
#     judgment from the proxy or any model
#   - Skill-author opt-in not required — proxy applies universally
#
# Trigger: the same condition that fires synthesis_directive (model has
# substantial tool results AND has not yet produced a substantial text
# answer). That's the structural moment "the model should be answering
# now."
#
# The pass converts the conversation history (assistant tool_calls + tool
# messages) into flattened user/assistant text messages because Ollama on
# qwen 9B returns empty content when format=schema is combined with the
# tool-call message format. Empirically verified during design.

GROUNDED_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": (
                "The response to the user's original request. Must be "
                "complete, specific, and grounded in the tool results "
                "from this conversation. If you cannot answer from the "
                "tool results, say so plainly here."
            ),
        },
        "sources_used": {
            "type": "array",
            "description": (
                "Verbatim substrings copied from earlier tool results "
                "that support the claims in `answer`. Each entry must "
                "be a literal copy of text that appeared in a tool "
                "result above. Empty array means the answer is not "
                "grounded in any tool result (i.e. it relies on prior "
                "knowledge or is uncertain)."
            ),
            "items": {"type": "string"},
        },
    },
    "required": ["answer", "sources_used"],
}


def _should_force_grounded_answer(messages: list) -> bool:
    """Fires only after the model has done multiple turns of tool work
    AND accumulated substantial results. Stricter than
    inject_synthesis_directive (which fires on the first large result)
    because grounded enforcement strips the tools, so we must be sure
    the model has had real opportunity to do its lookup work first."""
    if _has_substantial_text_answer(messages):
        return False
    tool_turns = sum(
        1 for m in messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    )
    if tool_turns < 4:
        return False
    substantial = _count_substantial_tool_results(messages)
    return substantial >= 2


def _flatten_messages_for_format(messages: list) -> list:
    """Convert a conversation that uses tool_calls + role=tool messages
    into one that uses only role=user / role=assistant / role=system
    text messages. Required because Ollama's format=schema combined
    with the tool-call message format returns empty content on qwen
    9B (verified empirically)."""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            content = _content_text(m.get("content", ""))
            tcid = m.get("tool_call_id", "")
            out.append({
                "role": "user",
                "content": f"[Tool result for {tcid or '<call>'}]:\n{content}",
            })
            continue
        if role == "assistant" and m.get("tool_calls"):
            text_content = _content_text(m.get("content", ""))
            tc_descriptions = []
            for tc in m["tool_calls"]:
                func = tc.get("function") or {}
                name = func.get("name", "")
                args = func.get("arguments", "")
                if isinstance(args, dict):
                    args = json.dumps(args)
                tc_descriptions.append(f"  - {name}({str(args)[:200]})")
            tc_text = "\n".join(tc_descriptions)
            combined = (text_content + "\n" if text_content else "") + (
                f"[Called tools]\n{tc_text}" if tc_descriptions else ""
            )
            out.append({"role": "assistant", "content": combined or "(tool call)"})
            continue
        # Plain message — pass through (system / user / assistant text-only)
        out.append({
            "role": role,
            "content": _content_text(m.get("content", "")),
        })
    return out


def _collect_tool_result_text(messages: list) -> str:
    """Concatenate all tool result content. Used to verify citations
    via substring match."""
    out = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        out.append(_content_text(m.get("content", "")))
    return "\n".join(out)


def _verify_citations(sources_used: list, all_tool_text: str) -> tuple[int, int]:
    """Return (verified_count, total_count). A citation is verified if
    it appears verbatim (case-sensitive substring) in the concatenated
    tool result text. Empty/whitespace citations don't count."""
    verified = 0
    total = 0
    for src in sources_used or []:
        if not isinstance(src, str):
            continue
        s = src.strip()
        if not s or len(s) < 3:
            continue
        total += 1
        if s in all_tool_text:
            verified += 1
    return verified, total


async def _ollama_grounded_answer_call(
    flattened_messages: list, model: str, num_ctx: int,
) -> dict | None:
    """Make a synchronous (non-streaming) Ollama call with format=schema
    and no tools. Returns parsed JSON dict on success, None on failure."""
    body = {
        "model": model,
        "messages": flattened_messages,
        "stream": False,
        "format": GROUNDED_ANSWER_SCHEMA,
        "options": {
            "temperature": 0.0,
            "num_ctx": num_ctx,
            "num_predict": 1500,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/chat", json=body)
            if resp.status_code >= 400:
                return None
            data = resp.json()
            content = data.get("message", {}).get("content", "")
            if not isinstance(content, str) or not content.strip():
                return None
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return None
    except (httpx.RequestError, json.JSONDecodeError, ValueError):
        return None


# ── Procedure map (mechanical structure exposure) ───────────────────────
#
# Generic skill-content navigation aid. When the model Reads a markdown
# file from inside the active skill tree, the proxy mechanically parses
# that file's structure (headers, links, inline file paths, code-block
# invocations) and prepends a compact "Procedure Map" to the tool
# result. The model sees the parsed structure alongside the raw content.
#
# Why this passes the structural test:
#   - The proxy makes NO decision about what's load-bearing. It parses
#     what the file itself documents and exposes that structure.
#   - Markdown parsing is universal — works for any procedure file in
#     any skill. Skills that don't have markdown files in their tree
#     get nothing.
#   - References are filesystem-verified before being surfaced (drops
#     hallucinated examples in prose, drops external links).
#   - Maps are EXPOSURE, not enforcement. The model decides what to do
#     with the navigation aid. No nudges, no pushes, no thresholds.
#   - Modeled on Claude Code's post-tool hooks pattern
#     (services/tools/toolHooks.ts:runPostToolUseHooks) — same shape:
#     enrich the result before the model consumes it.
#
# Idempotency: the prepend includes a marker `[Procedure Map`. Subsequent
# turns detect the marker and skip re-prepending.

# Markdown structural patterns (mechanical, no semantic interpretation)
_PM_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_PM_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_PM_INLINE_PATH_RE = re.compile(
    r"(?<![/\w.])([a-zA-Z_][\w-]+(?:/[a-zA-Z_][\w.-]*)+\.[a-zA-Z][\w-]*)"
    r"(?![\w.])"
)
# Require an explicit shell-language hint. Code blocks without a hint
# tend to be illustrative content (examples, sample output, interlinear
# glosses) and surface as false-positive "invocations" otherwise.
_PM_CODE_BLOCK_RE = re.compile(
    r"```(?:bash|sh|zsh|shell)\s*\n([^\n].*?)\n```", re.DOTALL
)


def _build_procedure_map(content: str, skill_resolved: Path) -> str:
    """Mechanically extract structural elements from markdown content.
    Returns a compact map block, or empty string if nothing useful
    found."""
    if not content or len(content) < 100:
        return ""

    # Section headings (top 3 levels only — deeper is detail noise)
    headers = []
    for m in _PM_HEADER_RE.finditer(content):
        if len(m.group(1)) > 3:
            continue
        h = m.group(2).strip().rstrip("#").strip()
        if h and h not in headers:
            headers.append(h)

    # Cross-referenced files: markdown links + inline path tokens.
    # Filter to those that actually exist inside the skill tree.
    candidate_refs: set[str] = set()
    for m in _PM_MD_LINK_RE.finditer(content):
        candidate_refs.add(m.group(1).strip())
    for m in _PM_INLINE_PATH_RE.finditer(content):
        candidate_refs.add(m.group(1).strip())

    verified_refs = []
    for r in sorted(candidate_refs):
        # External URL — skip
        if "://" in r:
            continue
        # Try as relative path inside skill
        try:
            cand = (skill_resolved / r).resolve()
            cand.relative_to(skill_resolved)
        except (ValueError, OSError):
            continue
        if cand.exists():
            verified_refs.append(r)

    # Shell invocations from fenced code blocks (first non-comment line)
    invocations = []
    for m in _PM_CODE_BLOCK_RE.finditer(content):
        block = m.group(1).strip()
        for line in block.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                if line not in invocations:
                    invocations.append(line[:200])
                break

    if not headers and not verified_refs and not invocations:
        return ""

    parts = ["[Procedure Map — mechanically extracted from this file]"]
    if headers:
        if len(headers) > 12:
            line = "; ".join(headers[:12]) + f"; …(+{len(headers) - 12} more)"
        else:
            line = "; ".join(headers)
        parts.append(f"Sections: {line}")
    if verified_refs:
        if len(verified_refs) > 10:
            line = ", ".join(verified_refs[:10]) + f", …(+{len(verified_refs) - 10} more)"
        else:
            line = ", ".join(verified_refs)
        parts.append(f"In-tree references (verified to exist): {line}")
    if invocations:
        if len(invocations) > 5:
            line = "; ".join(invocations[:5]) + f"; …(+{len(invocations) - 5} more)"
        else:
            line = "; ".join(invocations)
        parts.append(f"Code-block invocations: {line}")
    parts.append("[End Procedure Map]")
    return "\n".join(parts)


def _active_skill_base_for_maps(messages: list) -> Path | None:
    """Same logic as _find_active_skill_base but local to this filter
    so its lifecycle is independent."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        text = _content_text(m.get("content", ""))
        cleaned = _strip_system_reminders(text)
        match = _LOADED_SKILL_FULL_PATH_RE.search(cleaned)
        if match:
            base = Path(match.group(1).strip())
            if base.is_dir():
                return base
    return None


def inject_procedure_maps(messages: list) -> list:
    """For every tool message that holds the result of a Read against a
    markdown file inside the active skill tree, prepend a mechanically-
    extracted Procedure Map. Idempotent (skips messages already mapped).
    Generic across all skills."""
    skill_base = _active_skill_base_for_maps(messages)
    if skill_base is None:
        return messages
    skill_resolved = skill_base.resolve()

    augmented = 0
    for i, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls", []) or []:
            func = tc.get("function") if isinstance(tc, dict) else None
            if not isinstance(func, dict) or func.get("name") != "Read":
                continue
            try:
                args = func.get("arguments") or "{}"
                args = json.loads(args) if isinstance(args, str) else args
            except (json.JSONDecodeError, TypeError):
                continue
            fp = (args or {}).get("file_path", "")
            if not isinstance(fp, str) or not fp.endswith(".md"):
                continue
            try:
                resolved = Path(fp).resolve()
                resolved.relative_to(skill_resolved)
            except (ValueError, OSError):
                continue
            tcid = tc.get("id")
            if not tcid:
                continue
            for j in range(i + 1, len(messages)):
                rm = messages[j]
                if rm.get("role") != "tool":
                    continue
                if rm.get("tool_call_id") != tcid:
                    continue
                content = _content_text(rm.get("content", ""))
                if not content or "[Procedure Map" in content:
                    break
                map_text = _build_procedure_map(content, skill_resolved)
                if not map_text:
                    break
                new_rm = dict(rm)
                new_rm["content"] = f"{map_text}\n\n{content}"
                messages[j] = new_rm
                augmented += 1
                break
    if augmented:
        proxy_log(
            "FILTER",
            f"+procedure_maps ({augmented} markdown tool result(s) augmented)",
            data={"augmented": augmented},
        )
    return messages


def inject_synthesis_directive(messages: list) -> list:
    """When the model has accumulated substantial tool results but hasn't
    yet produced a real answer, inject a directive pushing it to
    synthesize from the data rather than hedging.

    This addresses a behavioral gap in smaller models: they retrieve
    the data correctly (same tool-call pattern as larger models) but
    then produce hedging meta-answers instead of specific synthesized
    responses. The directive tells the model HOW to use the data it has.

    Fires when EITHER:
    - ≥3 non-failure tool results with ≥200 chars each (accumulated evidence), OR
    - The most recent tool result is ≥KRULL_LARGE_RESULT_SYNTHESIS_THRESHOLD chars
      (one big dump — enough to saturate working attention on its own)

    In both cases, requires: no substantial text answer (>500 chars, no tool_calls)
    yet in the conversation.

    Position: appended at the END of messages for maximum attention.
    Idempotent: won't double-inject if already present.
    """
    # Idempotent
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str) and "[Krull Synthesis Directive]" in content:
            return messages

    if _has_substantial_text_answer(messages):
        return messages

    substantial = _count_substantial_tool_results(messages)
    last_size = _last_tool_result_size(messages)
    large_last = last_size >= KRULL_LARGE_RESULT_SYNTHESIS_THRESHOLD

    if substantial < 3 and not large_last:
        return messages

    messages.append({"role": "system", "content": SYNTHESIS_DIRECTIVE})
    trigger = "large_last" if large_last and substantial < 3 else "accumulated"
    proxy_log("FILTER", f"+synthesis_directive ({substantial} substantial results, "
              f"last={last_size} chars, trigger={trigger})",
              data={"substantial_results": substantial,
                    "last_result_chars": last_size,
                    "trigger": trigger})
    return messages


# Stalled progress detection (task #23, revised for filler-text evasion).
#
# The original detector counted consecutive tool-call assistant turns,
# resetting when any assistant message had >100 chars of text without
# tool_calls. This missed a common pattern: the model emits short filler
# text ("Let me look up these words...") between tool-call batches,
# which resets the counter even though the model isn't actually answering.
#
# The new approach uses a WINDOWED RATIO: look at the last N assistant
# messages and count what fraction are tool-call turns. If the ratio is
# too high, the model is grinding. A single "real answer" (>500 chars,
# no tool_calls) still resets, but filler doesn't.

# Hard cap: after this many TOTAL tool-call assistant turns in the
# conversation, strip tools entirely and force a text response.
KRULL_TOOL_CALL_HARD_CAP = int(
    os.environ.get("KRULL_TOOL_CALL_HARD_CAP", "20")
)

# Working-attention cap: strip tools when estimated prompt tokens exceed
# this threshold AND the model has accumulated substantial tool results.
# Small models like qwen 9B choke well before their nominal context size
# (131K) — empirically they start producing empty output around 35–40K
# prompt tokens. When we're in that zone with enough data already gathered,
# stripping tools forces the model to synthesize rather than keep loading
# more files. Generic: any skill, any workflow, any tool mix.
KRULL_WORKING_ATTENTION_TOKENS = int(
    os.environ.get("KRULL_WORKING_ATTENTION_TOKENS", "28000")
)


def count_tool_call_stats(messages: list) -> dict:
    """Analyze tool-call patterns across assistant messages.

    Returns:
        total_tool_turns: total assistant messages with tool_calls
        total_text_turns: total assistant messages without tool_calls
        window_tool_turns: tool-call turns in the last WINDOW messages
        window_size: how many assistant messages were in the window
        consecutive: consecutive tool-call turns from the end (legacy)
    """
    WINDOW = 20  # look at last 20 assistant messages

    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]

    total_tool = 0
    total_text = 0
    for m in assistant_msgs:
        if m.get("tool_calls"):
            total_tool += 1
        else:
            total_text += 1

    # Windowed analysis
    window = assistant_msgs[-WINDOW:] if len(assistant_msgs) > WINDOW else assistant_msgs
    window_tool = 0
    for m in window:
        if m.get("tool_calls"):
            window_tool += 1

    # Consecutive from end (but filler-resistant: only a REAL answer
    # of >500 chars with no tool_calls resets the counter)
    consecutive = 0
    for m in reversed(assistant_msgs):
        tcs = m.get("tool_calls") or []
        content_text = _content_text(m.get("content", "") or "").strip()
        if tcs:
            consecutive += 1
            continue
        # Only a substantial text-only response resets.
        # 500 chars ≈ a real paragraph answer, not filler.
        if len(content_text) > 500:
            break
        # Filler text — keep counting
        consecutive += 1

    return {
        "total_tool_turns": total_tool,
        "total_text_turns": total_text,
        "window_tool_turns": window_tool,
        "window_size": len(window),
        "consecutive": consecutive,
    }


STALLED_PROGRESS_HINT_TEMPLATE = (
    "[Krull Stalled Progress Warning]\n"
    "You have made {count} tool-call turns without producing a real "
    "answer to the user ({ratio}% of recent turns were tool calls). "
    "This pattern means you are stuck in a loop — tools are running "
    "but the workflow is not converging.\n"
    "\n"
    "STOP making tool calls. On your next turn, do ONE of these:\n"
    "  1. SUMMARIZE what you found and what you still need. Be honest.\n"
    "  2. Give your best answer with the data you already have.\n"
    "  3. Ask the user for guidance.\n"
    "\n"
    "Do NOT call another tool. Do NOT emit filler text like "
    "'Let me continue...' followed by more tool calls. The user is "
    "waiting for a real response.\n"
    "[End Krull Stalled Progress Warning]"
)

HARD_CAP_HINT = (
    "[Krull Tool Call Limit Reached]\n"
    "{reason} Tools have been DISABLED for this turn. You MUST "
    "respond with text only.\n"
    "\n"
    "Use the tool results you have ALREADY gathered to produce the "
    "user's answer now. Do not ask for more data — synthesize from "
    "what's in this conversation. If the data is sufficient to answer, "
    "give the answer directly and specifically (cite content, "
    "vocabulary, definitions, steps you found). If something is "
    "genuinely missing, say what you have and what you couldn't find.\n"
    "\n"
    "If you cannot synthesize a full answer, summarize:\n"
    "- What the user asked for\n"
    "- What you have completed\n"
    "- What remains to be done\n"
    "- Any errors or blockers encountered\n"
    "\n"
    "The user can then decide whether to continue.\n"
    "[End Krull Tool Call Limit]"
)


def inject_stalled_progress_warning(messages: list) -> list:
    """Detect stalled tool-call loops using windowed ratio analysis.

    Fires when EITHER:
    - The consecutive tool-call count (filler-resistant) exceeds threshold
    - The windowed ratio (tool turns / window size) exceeds 80%
      AND window has at least 8 assistant messages
    """
    stats = count_tool_call_stats(messages)

    # Check windowed ratio
    ratio_triggered = False
    ratio_pct = 0
    if stats["window_size"] >= 8:
        ratio_pct = int(stats["window_tool_turns"] / stats["window_size"] * 100)
        if ratio_pct >= 80:
            ratio_triggered = True

    consecutive_triggered = stats["consecutive"] >= KRULL_STALLED_PROGRESS_THRESHOLD

    if not ratio_triggered and not consecutive_triggered:
        return messages

    # Idempotent — don't inject if already present
    for m in messages:
        if m.get("role") == "system" and "[Krull Stalled Progress Warning]" in _content_text(m.get("content", "")):
            return messages

    count = max(stats["consecutive"], stats["window_tool_turns"])
    hint = STALLED_PROGRESS_HINT_TEMPLATE.format(count=count, ratio=ratio_pct)
    trigger = "ratio" if ratio_triggered else "consecutive"
    proxy_log("FILTER", f"+stalled_progress_warning ({trigger}: consecutive={stats['consecutive']} "
              f"window={stats['window_tool_turns']}/{stats['window_size']} "
              f"total={stats['total_tool_turns']})",
              level="warn", data=stats)
    return _insert_after_system_messages(messages, hint)


def apply_hard_tool_cap(messages: list, tools: list | None) -> tuple[list, list | None]:
    """If total tool-call turns OR estimated prompt tokens exceed their
    caps, strip all tools and inject a message forcing a text-only
    response.

    Two triggers — either fires independently:
    - total_tool_turns >= KRULL_TOOL_CALL_HARD_CAP (count-based)
    - est_prompt_tokens >= KRULL_WORKING_ATTENTION_TOKENS (size-based)

    The size trigger catches the common case where a small model hasn't
    made many tool calls but has accumulated enough large results (big
    Reads, flooded Bash outputs) to saturate its working attention. In
    that state it typically emits empty output instead of synthesizing —
    stripping tools forces it to produce text from what it already has.

    Generic: works for any tool, any skill, any workflow.

    Returns (messages, tools) — tools will be [] if cap is hit.
    """
    if not tools:
        return messages, tools

    stats = count_tool_call_stats(messages)
    est_tokens = sum(_msg_tokens(m) for m in messages)

    count_triggered = stats["total_tool_turns"] >= KRULL_TOOL_CALL_HARD_CAP
    size_triggered = est_tokens >= KRULL_WORKING_ATTENTION_TOKENS

    if not (count_triggered or size_triggered):
        return messages, tools

    # Only strip if there's actually substantial tool data to synthesize
    # from. If we haven't gathered anything yet, stripping tools would
    # force the model to answer from training knowledge, which is worse.
    if size_triggered and not count_triggered:
        if _count_substantial_tool_results(messages) < 1:
            return messages, tools

    # Already injected?
    for m in messages:
        if m.get("role") == "system" and "[Krull Tool Call Limit Reached]" in _content_text(m.get("content", "")):
            return messages, []

    if count_triggered:
        reason = (
            f"You have used {stats['total_tool_turns']} tool-call turns "
            f"in this conversation (cap: {KRULL_TOOL_CALL_HARD_CAP})."
        )
        trigger_name = "count"
    else:
        reason = (
            f"This conversation has grown to ~{est_tokens} tokens, past "
            f"the working-attention threshold ({KRULL_WORKING_ATTENTION_TOKENS}) "
            f"for this model. You already have substantial tool data."
        )
        trigger_name = "size"

    hint = HARD_CAP_HINT.format(reason=reason)
    proxy_log("FILTER", f"+hard_tool_cap (trigger={trigger_name}, "
              f"tool_turns={stats['total_tool_turns']}, "
              f"est_tokens={est_tokens}, stripping all tools)",
              level="warn", data={"total_tool_turns": stats["total_tool_turns"],
                                  "count_cap": KRULL_TOOL_CALL_HARD_CAP,
                                  "est_tokens": est_tokens,
                                  "size_cap": KRULL_WORKING_ATTENTION_TOKENS,
                                  "trigger": trigger_name})
    messages = _insert_after_system_messages(messages, hint)
    return messages, []


def maybe_lock_to_planning(messages: list, tools: list | None) -> list | None:
    """If the latest user message is plan-worthy and no prior TaskCreate
    exists in history, narrow the tools list down to planning tools only.
    Otherwise return the tools list unchanged. This is the planning-lock
    forcing function — the model has no choice but to plan first.
    Returns the (possibly narrowed) tools list."""
    if not tools:
        return tools
    if not _is_plan_worthy_query(messages):
        return tools
    if _history_has_task_create(messages):
        return tools
    narrowed = _narrow_to_planning_tools(tools)
    if not narrowed:
        # No planning tools available — can't lock. Leave tools alone
        # so the model at least has *something* to call.
        proxy_log("FILTER", "planning_lock skipped: no planning tools in request")
        return tools
    if len(narrowed) < len(tools):
        proxy_log("FILTER", f"+planning_lock (narrowed {len(tools)}→{len(narrowed)} tools, "
                  f"slash-command query, no prior TaskCreate)",
                  data={"from": len(tools), "to": len(narrowed)})
        return narrowed
    return tools


_TERSENESS_PATTERNS = [
    re.compile(r"\bonly\s+(?:the\s+)?(?:answer|translation|word|response|result)\b", re.I),
    re.compile(r"\bno\s+(?:explanation|commentary|preamble|context|hedging)\b", re.I),
    re.compile(r"\bjust\s+(?:the\s+)?(?:answer|translation|word|name|number)\b", re.I),
    re.compile(r"\b(?:in\s+)?(?:one|a\s+single)\s+word\b", re.I),
    re.compile(r"\bgive\s+me\s+only\b", re.I),
    re.compile(r"\b(?:terse|brief|concise|short)\s+(?:answer|reply|response)\b", re.I),
]

TRUTH_GUARD_TERSE_NUDGE = (
    "\n\n[Truth Guard reminder — applies to the request above]\n"
    "You asked for a terse answer. Honesty still applies: if you are not "
    "confident in the answer, you MUST prepend the single hedge token "
    "'unsure:' to your reply (e.g. 'unsure: I don't have reliable "
    "vocabulary for that language'). Fabricating a confident terse answer "
    "is forbidden. Brevity never licenses invention."
)


def _last_user_text(messages: list) -> tuple[int, str] | tuple[None, None]:
    """Return (index, text) of the last user message whose content is a
    plain string. Returns (None, None) if none found."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return i, c
    return None, None


def inject_truth_guard(messages: list) -> list:
    """Inject truth guard rules as a system message at the start, AND
    append a freshness reminder to the latest user message when it
    contains terseness directives.

    Note: This inserts at position 0 (in front of Claude Code's main
    prompt). We tried moving it to _insert_after_system_messages and
    it broke tool-calling behavior in the qwen 9B — see
    sharded-hopping-canyon.md. The qwen 9B anchors heavily on whatever
    is at position 0; the existing front-loaded filters were
    accidentally serving as the model's tool-use primer, and removing
    that primer caused the model to stop calling tools. Only the Krull
    project context is positioned after Claude Code's prompt; the
    pre-existing filters stay where they were.

    The freshness append is needed because qwen 9B's attention falls
    off well before the truth guard system message when Claude Code's
    own ~25k-char system prompt is in front of it. Empirically the
    model fabricates confident answers under "give me ONLY X, no
    explanation" phrasing despite the system-level guard. Appending the
    nudge to the user message itself puts the reminder in the
    highest-attention position right before generation."""
    messages.insert(0, {"role": "system", "content": TRUTH_GUARD_CONTENT})
    proxy_log("FILTER", "+truth_guard")

    idx, text = _last_user_text(messages)
    if idx is not None and any(p.search(text) for p in _TERSENESS_PATTERNS):
        messages[idx] = dict(messages[idx])
        messages[idx]["content"] = text + TRUTH_GUARD_TERSE_NUDGE
        proxy_log("FILTER", "+truth_guard_terse_nudge")

    return messages


def inject_date(messages: list) -> list:
    """Inject current date/time as a system message at the start.
    See inject_truth_guard for why this stays at position 0."""
    now = datetime.now()
    date_str = now.strftime("%A, %B %d, %Y")
    time_str = now.strftime("%I:%M %p")
    messages.insert(0, {
        "role": "system",
        "content": (
            f"Today's date is {date_str}. The current time is {time_str}. "
            f"This is a verified fact from the server clock, not a guess. "
            f"You MUST treat this as the actual current date when answering questions. "
            f"Do NOT say your data is outdated or that you cannot verify the date. "
            f"The date is {date_str}."
        ),
    })
    proxy_log("FILTER", "+date")
    return messages


async def inject_web_search(messages: list) -> list:
    """Search SearXNG for the user's query and prepend results."""
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "user":
        return messages
    query = last.get("content", "")
    if not query or len(query.strip()) < 3:
        return messages

    # Skip web search for very short/simple messages or tool results
    if isinstance(query, list):
        return messages

    try:
        search_query = query
        recency_words = ["latest", "recent", "current", "today", "new", "now", "update"]
        if any(word in query.lower() for word in recency_words):
            search_query = f"{query} {datetime.now().strftime('%B %Y')}"

        search_url = (
            f"{SEARXNG_URL}/search"
            f"?q={urllib.parse.quote(search_query)}"
            f"&format=json&categories=general"
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(search_url)
            if resp.status_code != 200:
                return messages
            data = resp.json()

        results = data.get("results", [])[:SEARCH_RESULTS]
        if not results:
            return messages

        date_str = datetime.now().strftime("%B %d, %Y")
        lines = [f"[Web Search Results — retrieved {date_str}]"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("content", "")
            lines.append(f"{i}. {title}\n   URL: {url}\n   {snippet}")
        lines.append("[End Web Search Results]")
        lines.append("")
        lines.append(
            f"IMPORTANT: The search results above are LIVE results retrieved "
            f"just now on {date_str}. This information is current and "
            f"supersedes your training data. You MUST use these results to "
            f"answer the question. Cite your sources inline."
        )
        lines.append("")

        messages[-1] = dict(last)
        messages[-1]["content"] = "\n".join(lines) + f"\nUser question: {query}"

    except Exception as e:
        proxy_log("PROXY", f"Web search error: {e}", level="error")

    return messages


def _xml_element_text(el) -> str:
    """Extract all text from an XML element, including text within child tags."""
    import xml.etree.ElementTree as ET
    raw = ET.tostring(el, encoding="unicode", method="text")
    return raw.strip() if raw else ""


# Cache of eng-only book names from the Kiwix catalog. Populated lazily on
# first use of inject_kiwix and on first use of inject_lang_docs.
# kiwix-serve rejects multi-book /search calls whose books span more than
# one language ("confusion of tongues", HTTP 400) — even when the second
# language only appears as a comma-separated entry in the book's <language>
# tag (e.g. TED talks). We must enumerate eng-strict books at startup and
# pass each one explicitly via books.name=. Mirrors the same logic in
# kiwix-front/.../search.js.
_KIWIX_ENG_BOOKS: list[str] | None = None
_KIWIX_BOOKS_LOCK = asyncio.Lock()


async def _get_eng_book_names() -> list[str]:
    """Return cached list of strict-eng Kiwix book names suitable for the
    /search?books.name= parameter. Fetches the OPDS catalog on first call.

    IMPORTANT: kiwix-serve's books.name= search parameter expects the FULL
    ZIM filename minus .zim (e.g. 'devdocs_en_python_2026-02'), not the
    OPDS <name> element (e.g. 'devdocs_en_python'). Passing the unsuffixed
    name yields HTTP 400 'No such book'. We extract the suffixed name from
    the entry's /content/<name> href, which is the only place in the OPDS
    feed where the full filename appears."""
    global _KIWIX_ENG_BOOKS
    if _KIWIX_ENG_BOOKS is not None:
        return _KIWIX_ENG_BOOKS
    async with _KIWIX_BOOKS_LOCK:
        if _KIWIX_ENG_BOOKS is not None:
            return _KIWIX_ENG_BOOKS
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{KIWIX_URL}/catalog/v2/entries?count=500"
                )
                if resp.status_code != 200:
                    _KIWIX_ENG_BOOKS = []
                    return _KIWIX_ENG_BOOKS
                xml_text = resp.text
            import xml.etree.ElementTree as ET
            ns = {"a": "http://www.w3.org/2005/Atom"}
            root = ET.fromstring(xml_text)
            books: list[str] = []
            for entry in root.findall("a:entry", ns):
                lang_el = entry.find("a:language", ns)
                if lang_el is None:
                    continue
                lang = (lang_el.text or "").strip()
                if "," in lang or lang != "eng":
                    continue
                # Pull the suffixed name out of the /content/<name> link.
                full_name: str | None = None
                for link in entry.findall("a:link", ns):
                    href = link.attrib.get("href", "")
                    if href.startswith("/content/"):
                        full_name = href[len("/content/"):].rstrip("/")
                        break
                if not full_name:
                    continue
                books.append(full_name)
            _KIWIX_ENG_BOOKS = books
            proxy_log("PROXY", f"Kiwix catalog: {len(books)} eng-strict books cached",
                      data={"books": len(books)})
        except Exception as e:
            proxy_log("PROXY", f"Kiwix catalog error: {e}", level="error")
            _KIWIX_ENG_BOOKS = []
    return _KIWIX_ENG_BOOKS


async def inject_kiwix(messages: list) -> list:
    """Search Kiwix for relevant offline knowledge with full-text snippets.

    Scopes the search to strict-eng books enumerated from the catalog and
    passes each one as a books.name= query parameter, because kiwix-serve
    rejects an unscoped /search?pattern=... when the loaded library has
    books in more than one language."""
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "user":
        return messages
    query = last.get("content", "")
    if not query or len(query.strip()) < 3:
        return messages
    if isinstance(query, list):
        return messages

    book_names = await _get_eng_book_names()
    if not book_names:
        return messages

    try:
        # Use the full-text search API (XML format) to get content snippets.
        # Scope by per-book books.name= params to satisfy kiwix-serve's
        # one-language-per-search invariant.
        params = [("pattern", query), ("format", "xml"), ("pageLength", "3")]
        params.extend(("books.name", b) for b in book_names)
        search_url = f"{KIWIX_URL}/search?" + urllib.parse.urlencode(params)

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(search_url)
            if resp.status_code != 200:
                proxy_log("FILTER", f"kiwix HTTP {resp.status_code} (url len={len(search_url)})",
                          level="warn", data={"status": resp.status_code, "url_len": len(search_url)})
                return messages
            xml_text = resp.text

        # Parse the XML response for titles, snippets, and sources
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            proxy_log("FILTER", "kiwix (no channel)")
            return messages

        items = channel.findall("item")
        if not items:
            proxy_log("FILTER", f"kiwix (0 items, query={query[:60]!r})")
            return messages

        lines = ["[Offline Knowledge Base (Kiwix) — full-text search results]"]
        for i, item in enumerate(items, 1):
            title_el = item.find("title")
            desc_el = item.find("description")
            link_el = item.find("link")
            book_el = item.find("book/title")
            title = title_el.text if title_el is not None else "Unknown"
            snippet = _xml_element_text(desc_el) if desc_el is not None else ""
            link = link_el.text if link_el is not None else ""
            book = book_el.text if book_el is not None else ""

            lines.append(f"--- Result {i}: {title} ---")
            if book:
                lines.append(f"Source: {book}")
            if snippet:
                # Truncate very long snippets
                if len(snippet) > 800:
                    snippet = snippet[:800] + "..."
                lines.append(snippet)
            if link:
                lines.append(f"Read more: http://localhost:8090{link}")
            lines.append("")

        lines.append("[End Offline Knowledge Base]")
        lines.append("")

        if len(lines) > 4:
            messages[-1] = dict(last)
            messages[-1]["content"] = "\n".join(lines) + f"\n{messages[-1]['content']}"
            proxy_log("FILTER", f"+kiwix ({len(items)} results, {len(book_names)} books scoped)",
                      data={"results": len(items), "books": len(book_names)})
        else:
            proxy_log("FILTER", "kiwix (no results)")

    except Exception as e:
        proxy_log("PROXY", f"Kiwix error: {e}", level="error")

    return messages


# ── Language-aware Kiwix devdocs injector ────────────────────────────────
#
# When krull-claude is in a coding session, the relevant Kiwix devdocs ZIM
# is sitting at http://localhost:8090 doing nothing. The model has no idea
# it exists, and Claude Code's WebFetch tool refuses localhost URLs (host
# blocklist), so even spelling out a URL doesn't help. The viable channel
# is Bash + curl, which Claude Code allows freely. inject_lang_docs detects
# the language in play from the latest user message, looks up the matching
# devdocs ZIMs in the live Kiwix catalog, and injects a system note that
# teaches the model the exact curl shape to use.
#
# Map keys are canonical language tags. Values are sequences of ZIM book
# name PREFIXES (without the trailing date suffix) — they're resolved to
# full suffixed names against the cached catalog at request time so the
# date suffix doesn't go stale as books are updated.
LANG_ZIM_MAP_RAW: dict[str, tuple[str, ...]] = {
    "python": (
        "devdocs_en_python",
        "devdocs_en_numpy",
        "devdocs_en_pandas",
        "devdocs_en_scikit-learn",
        "devdocs_en_fastapi",
    ),
    "javascript": (
        "devdocs_en_javascript",
        "devdocs_en_node",
        "devdocs_en_typescript",
    ),
    "typescript": (
        "devdocs_en_typescript",
        "devdocs_en_javascript",
        "devdocs_en_node",
    ),
    "react": (
        "devdocs_en_react",
        "devdocs_en_nextjs",
        "devdocs_en_javascript",
        "devdocs_en_typescript",
    ),
    "rust": ("devdocs_en_rust",),
    "go": ("devdocs_en_go",),
    "php": ("devdocs_en_php", "devdocs_en_phpunit"),
    "bash": ("devdocs_en_bash",),
    "html": ("devdocs_en_html", "devdocs_en_css", "devdocs_en_svg"),
    "css": ("devdocs_en_css", "devdocs_en_html", "devdocs_en_tailwindcss"),
    "sql": (
        "devdocs_en_postgresql",
        "devdocs_en_mariadb",
        "devdocs_en_sqlite",
    ),
    "docker": ("devdocs_en_docker", "devdocs_en_kubernetes", "devdocs_en_nginx"),
    "kubernetes": ("devdocs_en_kubernetes", "devdocs_en_docker"),
    "git": ("devdocs_en_git",),
    "redis": ("devdocs_en_redis",),
}

# File extension → language. Used by detect_language_context to weight
# extensions more heavily than free-text keyword matches.
LANG_EXT_MAP: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".ipynb": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".php": "php",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "css",
    ".sql": "sql",
}

# Marker filenames that pin a language even when no source file is named.
LANG_MARKER_FILES: dict[str, str] = {
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "setup.py": "python",
    "package.json": "javascript",
    "tsconfig.json": "typescript",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "composer.json": "php",
    "Dockerfile": "docker",
    "kubernetes.yaml": "kubernetes",
    "k8s.yaml": "kubernetes",
}

# Free-text keyword → language. Anchored to word boundaries so "go" only
# matches the word, not substrings of "django" / "ago" / etc.
LANG_KEYWORD_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bpython\b", re.I), "python"),
    (re.compile(r"\bnumpy\b|\bpandas\b|\basyncio\b|\bfastapi\b", re.I), "python"),
    (re.compile(r"\bjavascript\b|\bnode\.?js\b|\bnpm\b", re.I), "javascript"),
    (re.compile(r"\btypescript\b|\btsc\b", re.I), "typescript"),
    (re.compile(r"\breact\b|\bnext\.?js\b", re.I), "react"),
    (re.compile(r"\brust\b|\bcargo\b|\btokio\b|\bserde\b", re.I), "rust"),
    (re.compile(r"\bgolang\b", re.I), "go"),
    (re.compile(r"\bphp\b|\blaravel\b|\bphpunit\b", re.I), "php"),
    (re.compile(r"\bbash\b|\bshell script\b|\bzsh\b", re.I), "bash"),
    (re.compile(r"\bpostgres(?:ql)?\b|\bmariadb\b|\bsqlite\b|\bmysql\b", re.I), "sql"),
    (re.compile(r"\bdocker(?:file)?\b", re.I), "docker"),
    (re.compile(r"\bkubernetes\b|\bk8s\b|\bkubectl\b", re.I), "kubernetes"),
    (re.compile(r"\bredis\b", re.I), "redis"),
    # CSS/HTML are deliberately weak — only fire on explicit mentions, not
    # incidental occurrences in unrelated prose.
]

_EXT_RE = re.compile(r"(?:^|[\s/'\"`(])([\w./-]+(\.\w{1,5}))\b")


def detect_language_context(messages: list) -> list[str]:
    """Return an ordered list of detected language tags for the latest user
    message. Order = priority (extension hits first, then markers, then
    keywords). Caps at 2 languages to keep the injected block focused."""
    if not messages:
        return []
    last = messages[-1]
    if last.get("role") != "user":
        return []
    text = last.get("content", "")
    if not isinstance(text, str) or not text:
        return []
    seen: list[str] = []

    def add(lang: str) -> None:
        if lang in LANG_ZIM_MAP_RAW and lang not in seen:
            seen.append(lang)

    # 1) Extension matches.
    for m in _EXT_RE.finditer(text):
        ext = m.group(2).lower()
        if ext in LANG_EXT_MAP:
            add(LANG_EXT_MAP[ext])

    # 2) Marker filenames.
    for marker, lang in LANG_MARKER_FILES.items():
        # Word-boundary check to avoid matching as substring.
        if re.search(rf"\b{re.escape(marker)}\b", text):
            add(lang)

    # 3) Keyword tokens.
    for pat, lang in LANG_KEYWORD_PATTERNS:
        if pat.search(text):
            add(lang)

    return seen[:2]


def _resolve_lang_books(prefixes: tuple[str, ...], catalog: list[str]) -> list[str]:
    """Resolve a tuple of unsuffixed ZIM prefixes against the catalog list
    of full suffixed names. Order is preserved from the prefix tuple. If
    multiple catalog entries match a prefix (rare — older + newer copies
    of the same ZIM), the first one wins."""
    out: list[str] = []
    for prefix in prefixes:
        for book in catalog:
            if book == prefix or book.startswith(prefix + "_"):
                out.append(book)
                break
    return out


def _build_lang_docs_message(lang: str, books: list[str]) -> str:
    book_list = ", ".join(books)
    primary = books[0]
    return (
        f"[Krull Offline Docs — {lang} detected]\n"
        f"Offline reference docs for {lang} are loaded into the local Kiwix server. "
        f"Use Bash + curl to query them. Do NOT use WebFetch — it blocks localhost URLs.\n\n"
        f"Search (titles + snippets, returns HTML):\n"
        f"  curl -s '{KIWIX_HOST_URL}/search?books.name={primary}&pattern=<urlencoded query>&pageLength=5'\n\n"
        f"Fetch a specific page returned by the search above:\n"
        f"  curl -s '{KIWIX_HOST_URL}/content/{primary}/<path-from-search-result>'\n\n"
        f"Available books for {lang}: {book_list}\n"
        f"(Add another book by changing books.name=, or pass multiple books.name= "
        f"params to scope across them.)\n\n"
        f"CONSULT THESE OFFLINE DOCS BEFORE relying on training-data recall for "
        f"{lang} stdlib/framework specifics. They are authoritative and current.\n"
        f"[End Krull Offline Docs]"
    )


async def inject_lang_docs(messages: list, has_tools: bool) -> list:
    """When the latest user message names a programming language, inject
    a system note teaching the model the Bash+curl shape for querying the
    matching Kiwix devdocs ZIMs.

    Only fires when has_tools=True — for tools-less requests, inject_kiwix
    already prepends real search results, which is more useful. The two
    filters are complementary, not redundant."""
    if not has_tools or not ENABLE_LANG_DOCS:
        return messages
    langs = detect_language_context(messages)
    if not langs:
        return messages
    catalog = await _get_eng_book_names()
    if not catalog:
        return messages

    parts: list[str] = []
    fired: list[str] = []
    for lang in langs:
        prefixes = LANG_ZIM_MAP_RAW.get(lang, ())
        books = _resolve_lang_books(prefixes, catalog)
        if not books:
            continue
        parts.append(_build_lang_docs_message(lang, books))
        fired.append(lang)
    if not parts:
        return messages

    # Insert at position 1 (right after TOOL_GUIDANCE which is already at
    # position 0). This keeps the qwen 9B's tool-use anchor undisturbed
    # but puts the offline-docs guidance at the next-most-attended slot.
    content = "\n\n".join(parts)
    messages.insert(1, {"role": "system", "content": content})
    proxy_log("FILTER", f"+lang_docs ({', '.join(fired)})",
              data={"langs": fired})
    return messages


_LOCATION_PATTERNS = [
    re.compile(r"\b(?:where|find|locate|nearest|nearby|close to|around)\b", re.I),
    re.compile(r"\b(?:directions?|route|navigate|how (?:do I |to )get to)\b", re.I),
    re.compile(r"\b(?:map|maps|address|location|coordinates?|gps)\b", re.I),
    re.compile(r"\b(?:restaurant|cafe|coffee|shop|store|hotel|hospital|school|park|museum|library|airport|station)\b", re.I),
    re.compile(r"\b(?:street|road|avenue|boulevard|highway|drive|lane|plaza)\b", re.I),
    re.compile(r"\b(?:city|town|county|state|country|region|district|neighborhood)\b", re.I),
    re.compile(r"\b(?:latitude|longitude|lat|lon|lng)\b", re.I),
    re.compile(r"\b(?:zip\s*code|postal\s*code)\b", re.I),
]


def _is_location_query(text: str) -> bool:
    return any(p.search(text) for p in _LOCATION_PATTERNS)


async def inject_map_search(messages: list) -> list:
    """Search Photon geocoding for location-related queries."""
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "user":
        return messages
    query = last.get("content", "")
    if not query or len(query.strip()) < 3:
        return messages
    if isinstance(query, list):
        return messages
    if not _is_location_query(query):
        return messages

    try:
        search_url = (
            f"{PHOTON_URL}/api"
            f"?q={urllib.parse.quote(query)}"
            f"&limit={SEARCH_RESULTS}"
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(search_url)
            if resp.status_code != 200:
                return messages
            data = resp.json()

        features = data.get("features", [])
        if not features:
            return messages

        lines = ["[Offline Map Search Results — OpenStreetMap via Photon]"]
        for i, feature in enumerate(features, 1):
            props = feature.get("properties", {})
            geom = feature.get("geometry", {})
            coords = geom.get("coordinates", [])

            name = props.get("name", "")
            street = props.get("street", "")
            housenumber = props.get("housenumber", "")
            city = props.get("city", "")
            state = props.get("state", "")
            country = props.get("country", "")
            osm_type = props.get("osm_value", props.get("type", ""))

            parts = []
            if housenumber and street:
                parts.append(f"{housenumber} {street}")
            elif street:
                parts.append(street)
            if city:
                parts.append(city)
            if state:
                parts.append(state)
            if country:
                parts.append(country)
            address = ", ".join(parts)

            line = f"{i}. {name}" if name else f"{i}. {address}"
            if name and address:
                line += f"\n   Address: {address}"
            if osm_type:
                line += f"\n   Type: {osm_type}"
            if len(coords) >= 2:
                lon, lat = coords[0], coords[1]
                line += f"\n   Coordinates: {lat:.6f}, {lon:.6f}"
                line += f"\n   Map: {TILESERVER_URL}/#17/{lat}/{lon}"

            lines.append(line)

        lines.append("[End Map Search Results]")
        lines.append("")
        lines.append(
            "IMPORTANT: When using location information from the map search "
            "results above, cite OpenStreetMap as the source. Include "
            "coordinates and addresses in your response."
        )
        lines.append("")

        messages[-1] = dict(last)
        messages[-1]["content"] = "\n".join(lines) + f"\nUser question: {query}"

    except Exception as e:
        proxy_log("PROXY", f"Map search error: {e}", level="error")

    return messages


# Marker text used to detect already-truncated tool results so we don't
# re-truncate them on subsequent passes. Must match the marker emitted
# by truncate_large_tool_results below.
_TRUNCATION_MARKER = "[... truncated by Krull proxy"


def truncate_large_tool_results(messages: list) -> list:
    """Cap each individual tool result at KRULL_TOOL_RESULT_MAX_CHARS.

    Small models like the qwen 9B have a much smaller effective working
    attention window than their nominal context size. A single Read of
    a big file (e.g., a multi-megabyte cache, a long transcript, the
    entire dictionary) dumps tens of thousands of chars into the
    conversation, and after 5-6 such Reads the model is past its
    working window and stops emitting useful tool calls. Capping each
    result independently keeps context growth linear in *number* of
    tool calls rather than in tool result sizes.

    Uses a head+tail strategy: keeps the first 75% and last 25% of the
    cap budget, with a clear marker showing what was dropped from the
    middle. This preserves document structure — headers, vocabulary
    lists, and metadata at the top AND conclusions, summaries, and
    final sections at the bottom. The middle (typically the longest,
    most repetitive section) is where content is dropped.

    The model can re-Read specific lines via offset/limit if it needs
    content from the dropped middle section.

    General-purpose: works for any tool (Read, Bash, Grep, etc.), any
    skill, any project. No skill-specific logic.

    Idempotent: tool results that already contain the truncation marker
    are passed through unchanged.
    """
    new_messages = []
    truncated = 0
    saved_chars = 0
    cap = KRULL_TOOL_RESULT_MAX_CHARS
    # Reserve ~250 chars for the marker itself so total output ≈ cap
    marker_overhead = 300
    effective_cap = cap - marker_overhead
    head_size = int(effective_cap * 0.75)
    tail_size = effective_cap - head_size
    for m in messages:
        if m.get("role") != "tool":
            new_messages.append(m)
            continue
        content = m.get("content", "")
        # Tool results are usually plain strings; bail on other shapes
        # rather than guessing how to truncate them.
        if not isinstance(content, str):
            new_messages.append(m)
            continue
        if len(content) <= cap:
            new_messages.append(m)
            continue
        if _TRUNCATION_MARKER in content:
            # Already truncated by an earlier pass through this filter
            new_messages.append(m)
            continue
        head = content[:head_size]
        tail = content[-tail_size:]
        dropped = len(content) - head_size - tail_size
        marker = (
            f"\n\n{_TRUNCATION_MARKER}: {dropped} chars from the middle "
            f"of this content were dropped to keep the conversation "
            f"within the model's working window. The beginning and end "
            f"are preserved above and below. If you need content from "
            f"the dropped middle section, re-Read the source with "
            f"offset/limit parameters to fetch specific lines.]\n\n"
        )
        new_msg = dict(m)
        new_msg["content"] = head + marker + tail
        new_messages.append(new_msg)
        truncated += 1
        saved_chars += dropped
    if truncated > 0:
        proxy_log("FILTER", f"+tool_result_truncate ({truncated} results capped, "
                  f"{saved_chars} chars saved, cap={cap}, strategy=head+tail)",
                  data={"truncated": truncated, "saved_chars": saved_chars,
                        "cap": cap, "head": head_size, "tail": tail_size})
    return new_messages


def _est_tokens(text) -> int:
    """Rough token estimate: ~4 chars per token."""
    if isinstance(text, list):
        return sum(_est_tokens(p.get("text", "") if isinstance(p, dict) else str(p)) for p in text)
    return len(str(text or "")) // 4 + 1


def _msg_tokens(msg: dict) -> int:
    return _est_tokens(msg.get("content", "")) + 4


def compact_context(messages: list) -> list:
    """Compact older messages while preserving working state.

    The goal is NOT to summarize the conversation — it's to let the model
    pick up where it left off. We extract:
      1. Task state (TaskCreate/TaskUpdate results → what's done, what's next)
      2. The user's original request (first user message)
      3. Key decisions and findings from assistant messages
      4. Recent conversation (last N messages, kept verbatim)
      5. Tool call history (what was tried, what worked/failed)

    This prevents the infinite-loop problem where the model loses context
    about what it was doing, rediscovers the same issue, and repeats.
    """
    max_ctx = int(os.environ.get("CONTEXT_COMPACT_LIMIT", str(NUM_CTX)))
    threshold = int(max_ctx * 0.75)

    total_tokens = sum(_msg_tokens(m) for m in messages)

    if total_tokens <= threshold:
        proxy_log("PROXY", f"Context check: ~{total_tokens} tokens "
                  f"(threshold {threshold}, headroom {threshold - total_tokens})",
                  data={"est_tokens": total_tokens, "threshold": threshold,
                        "headroom": threshold - total_tokens, "msgs": len(messages)})
        return messages

    system_messages = [m for m in messages if m.get("role") == "system"]
    conversation = [m for m in messages if m.get("role") != "system"]

    if len(conversation) <= 16:
        # Too few messages to compact meaningfully
        return messages

    # ── Budget: how many tokens can the summary use? ──────────────────
    system_tokens = sum(_msg_tokens(m) for m in system_messages)
    # Reserve 40% of remaining budget for recent messages, 60% for summary
    remaining_budget = threshold - system_tokens
    recent_budget = int(remaining_budget * 0.4)
    summary_budget = int(remaining_budget * 0.55)  # leave 5% margin

    # ── Preserve recent conversation (as many messages as fit) ────────
    recent_messages = []
    recent_tokens = 0
    for msg in reversed(conversation):
        mt = _msg_tokens(msg)
        if recent_tokens + mt > recent_budget:
            break
        recent_messages.insert(0, msg)
        recent_tokens += mt

    # Ensure at least the last 8 messages are kept
    if len(recent_messages) < 8:
        recent_messages = conversation[-8:]
        recent_tokens = sum(_msg_tokens(m) for m in recent_messages)

    old_messages = conversation[:len(conversation) - len(recent_messages)]
    if not old_messages:
        return messages

    # ── Extract structured state from old messages ────────────────────

    # 1. The user's original request (first user message)
    original_request = ""
    for msg in old_messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            content = str(content or "")
            # Skip tool results masquerading as user messages
            if content and not content.startswith("{") and len(content) > 20:
                original_request = content[:1000]
                break

    # 2. Task state — extract from tool calls and results
    tasks_found = []
    for msg in old_messages:
        tc_list = msg.get("tool_calls", [])
        for tc in tc_list:
            func = tc.get("function", {})
            name = func.get("name", "")
            if name in ("TaskCreate", "TaskUpdate"):
                try:
                    args = func.get("arguments", "{}")
                    if isinstance(args, str):
                        args = json.loads(args)
                    tasks_found.append({"action": name, **args})
                except (json.JSONDecodeError, TypeError):
                    pass

    # 3. Key decisions — assistant messages that contain findings/conclusions
    #    (not just tool calls)
    decisions = []
    for msg in old_messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        content = str(content or "").strip()
        # Skip empty or very short assistant messages (tool-call-only turns)
        if len(content) < 50:
            continue
        # Keep a truncated version of substantive assistant messages
        decisions.append(content[:500])

    # 4. Tool call history — what was tried (name + brief args)
    tool_history = []
    for msg in old_messages:
        for tc in msg.get("tool_calls", []):
            func = tc.get("function", {})
            name = func.get("name", "")
            if name in ("TaskCreate", "TaskUpdate", "TaskGet", "TaskList"):
                continue  # already captured in task state
            try:
                args = func.get("arguments", "{}")
                if isinstance(args, str):
                    args = json.loads(args)
                # Brief summary of what was called
                if name == "Read":
                    tool_history.append(f"Read({args.get('file_path', '?')})")
                elif name == "Edit":
                    tool_history.append(f"Edit({args.get('file_path', '?')})")
                elif name == "Write":
                    tool_history.append(f"Write({args.get('file_path', '?')})")
                elif name == "Bash":
                    cmd = args.get("command", "")[:100]
                    tool_history.append(f"Bash({cmd})")
                elif name == "Grep":
                    tool_history.append(f"Grep({args.get('pattern', '?')})")
                elif name == "Glob":
                    tool_history.append(f"Glob({args.get('pattern', '?')})")
                elif name == "Skill":
                    tool_history.append(f"Skill({args.get('skill', '?')})")
                else:
                    tool_history.append(f"{name}(...)")
            except (json.JSONDecodeError, TypeError):
                tool_history.append(f"{name}(...)")

    # 5. Tool results that indicate errors (preserve these fully)
    errors_encountered = []
    for msg in old_messages:
        if msg.get("role") == "tool":
            content = str(msg.get("content", ""))
            if any(w in content.lower() for w in ("error", "failed", "exception",
                                                    "denied", "not found", "traceback")):
                errors_encountered.append(content[:300])

    # ── Build the summary message ─────────────────────────────────────
    parts = ["[Context Manager: Earlier conversation compacted to fit context window.]\n"]

    parts.append("=== WHAT YOU WERE DOING ===")
    if original_request:
        parts.append(f"Original user request:\n{original_request}\n")

    if tasks_found:
        parts.append("Task state from earlier in conversation:")
        for t in tasks_found[-20:]:  # last 20 task operations
            parts.append(f"  - {t.get('action')}: {json.dumps({k: v for k, v in t.items() if k != 'action'})}")
        parts.append("")

    if decisions:
        parts.append("=== KEY FINDINGS & DECISIONS ===")
        # Keep the most recent decisions (they're most relevant)
        for d in decisions[-5:]:
            parts.append(f"{d}\n")

    if tool_history:
        parts.append("=== TOOLS ALREADY USED ===")
        # Deduplicate consecutive identical calls
        deduped = []
        for t in tool_history:
            if not deduped or deduped[-1] != t:
                deduped.append(t)
        parts.append(", ".join(deduped[-30:]))  # last 30 unique calls
        parts.append("")

    if errors_encountered:
        parts.append("=== ERRORS ENCOUNTERED ===")
        for e in errors_encountered[-5:]:
            parts.append(f"  - {e}")
        parts.append("")

    parts.append("=== INSTRUCTIONS ===")
    parts.append("Continue from where you left off. Do NOT restart or re-investigate "
                 "issues already resolved above. If tasks are listed, check their "
                 "status and continue with the next incomplete task.")

    summary_content = "\n".join(parts)

    # Trim summary if it exceeds budget
    if _est_tokens(summary_content) > summary_budget:
        # Progressively trim: tool history first, then decisions, then errors
        while _est_tokens(summary_content) > summary_budget and tool_history:
            tool_history = tool_history[len(tool_history) // 2:]
            parts_rebuild = [p for p in parts if not p.startswith("=== TOOLS")]
            if tool_history:
                idx = next((i for i, p in enumerate(parts_rebuild) if "ERRORS" in p or "INSTRUCTIONS" in p), len(parts_rebuild))
                parts_rebuild.insert(idx, "=== TOOLS ALREADY USED ===")
                parts_rebuild.insert(idx + 1, ", ".join(tool_history[-15:]))
                parts_rebuild.insert(idx + 2, "")
            summary_content = "\n".join(parts_rebuild)

        # Last resort: hard truncate
        max_chars = summary_budget * 4
        if len(summary_content) > max_chars:
            summary_content = summary_content[:max_chars] + "\n[...truncated]"

    compact_msg = {"role": "system", "content": summary_content}

    compacted = system_messages + [compact_msg] + recent_messages
    new_tokens = sum(_msg_tokens(m) for m in compacted)

    proxy_log("PROXY", f"Context compacted: {total_tokens} → {new_tokens} est. tokens "
              f"({len(old_messages)} msgs summarized, {len(recent_messages)} kept, "
              f"tasks={len(tasks_found)} decisions={len(decisions)} "
              f"tools={len(tool_history)} errors={len(errors_encountered)})",
              level="warn",
              data={"from_tokens": total_tokens, "to_tokens": new_tokens,
                    "summarized": len(old_messages), "kept": len(recent_messages),
                    "tasks": len(tasks_found), "decisions": len(decisions),
                    "tool_history": len(tool_history),
                    "errors": len(errors_encountered)})
    return compacted


# ── Project context injection ─────────────────────────────────────────────
#
# Mirrors Claude Code's per-turn skill listing pattern (utils/attachments.ts:875,
# tools/SkillTool/prompt.ts:20-171). On every request, the proxy:
#
#   1. Extracts the user's cwd from the <env> block Claude Code injects into
#      the system prompt (constants/prompts.ts:642). Single source of truth.
#   2. Walks up from cwd looking for .git to find the project root, mirroring
#      utils/git.ts:27-109 (findGitRoot).
#   3. Discovers .claude/skills directories with the same precedence as
#      skills/loadSkillsDir.ts:638-803 (~/.claude/skills, then project,
#      walking up to $HOME), deduped by inode.
#   4. Reads SKILL.md frontmatter (read-only) for name+description, notes the
#      presence of lib/*.sh helpers and references/*.md procedure files.
#   5. Injects a budgeted system message ahead of the existing filters. After
#      the first turn for a given session, sends only deltas (or nothing).
#
# We never modify any project file. The "Known environmental notes" line in
# the injected system message is the principled way we route around broken
# downstream hooks: instead of patching the project, we tell the model what
# its environment looks like so it can choose tools that work.

KRULL_HOST_HOME = os.environ.get("KRULL_HOST_HOME", os.environ.get("HOME", "/root"))

# Short, always-on environmental rules. Re-injected on every turn (not just
# turn 1) because they govern the model's *next* tool call regardless of how
# far into the conversation we are. Skill listings are delta-only because
# they're large; rules are tiny and worth keeping fresh.
#
# Empirical: a 9B local model can apply the quoting rule on the first try
# when shown the working pattern next to a broken one, but burns 5+ turns
# rediscovering it from shell errors when not told. See log analysis at
# .claude/plans/sharded-hopping-canyon.md.
KRULL_SHELL_RULES = (
    "[Krull Shell Rules — applies to every tool that takes a path]\n"
    "1. PATHS WITH SPACES MUST BE DOUBLE-QUOTED. Unquoted paths are "
    "word-split by the shell and you get 'No such file or directory' "
    "on the first fragment.\n"
    "2. The CORRECT pattern when calling a script with arguments via Bash "
    "is: bash \"/path with spaces/script.sh\" arg1 arg2 arg3\n"
    "   - The path is in its own quoted string.\n"
    "   - Each argument is OUTSIDE the path quotes (separately quoted is "
    "fine).\n"
    "3. WRONG patterns to avoid:\n"
    "   - bash /path with spaces/script.sh arg1 arg2     "
    "(unquoted — splits on space)\n"
    "   - bash \"/path with spaces/script.sh arg1 arg2\"   "
    "(args inside quotes — treated as part of filename)\n"
    "4. The same rule applies to Read/Write/Edit file_path, Glob path, "
    "Grep path, etc. — but those tools accept the path as a JSON string "
    "argument, so you don't need to quote it yourself; the JSON encoding "
    "preserves spaces. The quoting rule above is specifically for Bash "
    "command strings.\n"
    "[End Krull Shell Rules]"
)


# Atomic Plan Rubric — the first leg of the small-model adapter (task #14).
#
# Injected when the request contains the TaskCreate tool, which is the
# signal that the model is about to (or already is) building a plan.
# Teaches the model to size individual tasks to a small model's working
# attention window. Strictly format-based — no skill names, no file paths,
# no domain-specific examples. Generalizes to any project, any skill.
#
# The rubric is the first of three components that work together:
#   1. This rubric teaches the rules.
#   2. The TaskCreate validator (#15) enforces them at creation time.
#   3. The plan-aware compactor (#19) keeps the conversation small enough
#      across many atomic steps that the model can actually execute the
#      plan it produced.
KRULL_ATOMIC_PLAN_RUBRIC = (
    "[Krull Atomic Plan Rubric — read before creating any TaskCreate items]\n"
    "\n"
    "You may be running on a smaller local model whose working attention "
    "window is much smaller than its nominal context size. To stay within "
    "it, every plan task you create must satisfy ALL of the following:\n"
    "\n"
    "  1. EXACTLY ONE TOOL CALL per task. (Or zero, if the task is pure "
    "synthesis from facts already stored by earlier tasks.)\n"
    "  2. TASK OUTPUT ≤ 500 chars, OR summarizable to ≤ 500 chars in one "
    "short sentence the next task can use as its input.\n"
    "  3. NO TASK MAY REQUIRE HOLDING A PRIOR TASK'S FULL OUTPUT in "
    "attention. You may carry forward only the *fact extracted from* the "
    "prior output, not the output itself.\n"
    "  4. EACH TASK IS INDEPENDENTLY VERIFIABLE — you can answer 'is this "
    "task done?' with yes/no by looking at one tool result.\n"
    "\n"
    "It is normal and correct for a single user request to expand into "
    "10–30 atomic tasks. A short plan with 4 large tasks is the wrong "
    "shape. A long plan with 25 small tasks is the right shape.\n"
    "\n"
    "WRONG-SHAPE patterns to avoid:\n"
    "  ✗ 'Read all <files> and synthesize a result'\n"
    "  ✗ 'Process the entire input in one step'\n"
    "  ✗ 'Look everything up and combine'\n"
    "  ✗ Any task whose subject contains: 'all', 'everything', "
    "'synthesize', 'combine and produce', 'load and apply'\n"
    "\n"
    "RIGHT-SHAPE patterns to model:\n"
    "  ✓ 'Run <one helper script> with one argument; store the result'\n"
    "  ✓ 'Read <one reference file>; extract the one relevant pattern'\n"
    "  ✓ 'Look up <one term> in <one source>; store the single fact'\n"
    "  ✓ '(no tool call) Combine <list of stored facts> into the answer'\n"
    "  ✓ '(no tool call) Verify each item in the answer appears in "
    "stored facts (no fabrication check)'\n"
    "\n"
    "CASCADE PRESERVATION (read carefully — silent skips are the worst "
    "failure mode for an atomic plan):\n"
    "\n"
    "  5. EVERY TASK THAT PRODUCES A FACT must explicitly name the fact "
    "in its description, in the form 'store: <fact_name>'. Pick names "
    "that the next task can reference unambiguously.\n"
    "       ✓ 'Run helper.sh with arg X; store: term_translation'\n"
    "       ✓ 'Read foo.md, extract pattern Y; store: question_pattern'\n"
    "\n"
    "  6. EVERY TASK THAT CONSUMES PRIOR FACTS must explicitly name them "
    "in the form 'uses: <fact_a>, <fact_b>', AND must use TaskCreate's "
    "addBlockedBy parameter to mark the producing tasks as dependencies. "
    "This is how the proxy knows which tasks must complete before this "
    "one can start.\n"
    "       ✓ '(no tool) Combine the stored values into the answer; "
    "uses: term_translation, question_pattern; addBlockedBy: [task ids "
    "that produce those facts]'\n"
    "\n"
    "  7. IF A FACT YOU NEED IS MISSING, EMPTY, OR FAILED — DO NOT "
    "IMPROVISE.\n"
    "     - Mark your current task with status: blocked\n"
    "     - Surface the missing dependency by its fact name\n"
    "     - Stop and wait for the dependency to be reproduced\n"
    "     Improvising past a missing fact is the worst failure mode for "
    "an atomic plan: a single silent skip cascades through every "
    "dependent task and corrupts the final answer without any visible "
    "signal. 'I don't have the result I need' is always a better answer "
    "than 'let me make something up.'\n"
    "\n"
    "If a task feels too big for the rules above, split it before "
    "creating it. Long checklists are correct for this model.\n"
    "[End Krull Atomic Plan Rubric]"
)


# 1% of context window in chars, mirroring SKILL_BUDGET_CONTEXT_PERCENT in
# tools/SkillTool/prompt.ts:20-41. NUM_CTX is in tokens; multiply by 4 for chars.
SKILL_BUDGET_CONTEXT_PERCENT = 0.01
CHARS_PER_TOKEN = 4
DEFAULT_CHAR_BUDGET = 8000
MAX_LISTING_DESC_CHARS = 250  # Per-skill description cap, matches prompt.ts:29

# Matches Claude Code's injected env info. Two formats exist in claude-scripts:
#   - computeEnvInfo (constants/prompts.ts:640):
#       <env>
#       Working directory: <cwd>
#       ...
#       </env>
#   - computeSimpleEnvInfo (constants/prompts.ts:705) — newer, used by recent
#     CLI versions like 2.1.92:
#       # Environment
#       You have been invoked in the following environment:
#        - Primary working directory: <cwd>
#        - Is a git repository: ...
# Both end the path at a newline. We accept either leading word and ignore
# any optional bullet/indent prefix.
_CWD_RE = re.compile(
    r"(?:Primary working directory|Working directory)\s*:\s*([^\n]+)"
)


def extract_cwd_from_messages(messages: list) -> str | None:
    """Find Claude Code's reported cwd in the system messages.

    Claude Code injects an <env> block into every system prompt that contains
    'Working directory: <abs path>'. Parsing it gives us the user's cwd
    without requiring any client-side header support.
    """
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if not isinstance(content, str):
            continue
        m = _CWD_RE.search(content)
        if m:
            return m.group(1).strip()
    # Debug: if we got here with a system message present but no match, dump
    # a snippet so we can see what format Claude Code actually sent.
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    if sys_msgs:
        first = sys_msgs[0]
        c = first.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        snippet = (c or "")[:600].replace("\n", "\\n")
        proxy_log("CONTEXT", f"cwd parse failed; system_msgs={len(sys_msgs)} "
                  f"first_snippet={snippet!r}", level="warn")
    else:
        proxy_log("CONTEXT", f"cwd parse failed; no system messages "
                  f"(total_msgs={len(messages)}, roles={[m.get('role') for m in messages[:5]]})",
                  level="warn")
    return None


def find_project_root(cwd: str) -> str:
    """Walk up from cwd looking for a .git file or directory.

    Mirrors findGitRoot in utils/git.ts:27-109. Falls back to cwd if no .git
    is found anywhere up the tree (matches Claude Code's behavior).
    """
    try:
        path = Path(cwd).resolve()
    except Exception:
        return cwd
    cur = path
    while True:
        if (cur / ".git").exists():
            return str(cur)
        parent = cur.parent
        if parent == cur:
            return str(path)  # filesystem root reached, fall back to cwd
        cur = parent


def _read_skill_frontmatter(skill_md: Path) -> dict:
    """Parse YAML frontmatter from a SKILL.md file. Read-only."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm = text[3:end]
    out = {}
    for line in fm.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _discover_skills_in_dir(skills_dir: Path) -> list[dict]:
    """Walk one .claude/skills directory and read each SKILL.md."""
    if not skills_dir.is_dir():
        return []
    skills = []
    try:
        entries = sorted(skills_dir.iterdir())
    except (PermissionError, OSError):
        return []
    for entry in entries:
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            stat = skill_md.stat()
            inode_key = (stat.st_dev, stat.st_ino)
        except OSError:
            continue
        fm = _read_skill_frontmatter(skill_md)
        # Generic top-level resource manifest: list every subdirectory of the
        # skill with its file count grouped by extension. No special-casing
        # of subdir names — surfaces whatever the skill author put there
        # (lib/, references/, grammar/, library/, examples/, …) so the
        # model can decide which to consult based on the procedure's own
        # words rather than the proxy's assumptions about layout.
        resources = []
        try:
            sub_entries = sorted(entry.iterdir())
        except (PermissionError, OSError):
            sub_entries = []
        for sub in sub_entries:
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            try:
                files = [p for p in sub.iterdir() if p.is_file()]
            except (PermissionError, OSError):
                continue
            if not files:
                continue
            ext_counts: dict[str, int] = {}
            for f in files:
                ext = f.suffix.lower() or "(noext)"
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
            ext_summary = ", ".join(
                f"{n} {e.lstrip('.')}" if e.startswith(".") else f"{n} {e}"
                for e, n in sorted(ext_counts.items(), key=lambda kv: -kv[1])
            )
            resources.append({"name": sub.name, "summary": ext_summary})
        skills.append({
            "name": fm.get("name") or entry.name,
            "description": fm.get("description", ""),
            "whenToUse": fm.get("whenToUse", ""),
            "dir": str(entry),
            "resources": resources,
            "_inode": inode_key,
        })
    return skills


def _project_skill_dirs(project_root: str) -> list[Path]:
    """Mirror getProjectDirsUpToHome from skills/loadSkillsDir.ts.

    Walk upward from project_root collecting .claude/skills dirs until $HOME.
    """
    dirs = []
    home = Path(KRULL_HOST_HOME).resolve()
    try:
        cur = Path(project_root).resolve()
    except Exception:
        return dirs
    while True:
        candidate = cur / ".claude" / "skills"
        if candidate.is_dir():
            dirs.append(candidate)
        if cur == home or cur == cur.parent:
            break
        cur = cur.parent
    return dirs


def discover_skills(project_root: str) -> list[dict]:
    """Discover all skills visible from project_root.

    Precedence (first wins, dedup by inode), mirroring loadSkillsDir.ts:638-803:
      1. ~/.claude/skills (user-global)
      2. <project_root>/.claude/skills and ancestors up to $HOME
    """
    seen = set()
    out = []
    user_skills = Path(KRULL_HOST_HOME) / ".claude" / "skills"
    sources = [user_skills] + _project_skill_dirs(project_root)
    for src in sources:
        for s in _discover_skills_in_dir(src):
            if s["_inode"] in seen:
                continue
            seen.add(s["_inode"])
            out.append(s)
    return out


def _get_char_budget() -> int:
    """1% of context window in chars, with 8000 fallback (prompt.ts:20-41)."""
    try:
        ctx_tokens = int(os.environ.get("OLLAMA_NUM_CTX", str(NUM_CTX)))
        return max(
            DEFAULT_CHAR_BUDGET,
            int(ctx_tokens * CHARS_PER_TOKEN * SKILL_BUDGET_CONTEXT_PERCENT),
        )
    except Exception:
        return DEFAULT_CHAR_BUDGET


def format_skill_listing(skills: list[dict], budget_chars: int) -> str:
    """Format skills as '- name: description', truncating to fit the budget.

    Mirrors formatCommandsWithinBudget in tools/SkillTool/prompt.ts:70-171:
    try full descriptions first, fall back to names-only if over budget.
    """
    def _line(s, desc_chars):
        desc = s.get("whenToUse") or s.get("description") or ""
        if desc and desc_chars > 0:
            if len(desc) > desc_chars:
                desc = desc[: desc_chars - 1].rstrip() + "…"
            return f"- {s['name']}: {desc}"
        return f"- {s['name']}"

    full = "\n".join(_line(s, MAX_LISTING_DESC_CHARS) for s in skills)
    if len(full) <= budget_chars:
        return full
    # Names-only fallback
    return "\n".join(f"- {s['name']}" for s in skills)


def format_resources_section(skills: list[dict]) -> str:
    """For each skill that has any top-level subdirectories with files,
    list them so the model knows what's inside the skill before reading
    its body. Generic — no special-casing of directory names. Mirrors
    the discovery principle in tools/SkillTool/prompt.ts (lightweight
    pre-invocation visibility) but extended to the skill's own layout."""
    lines = []
    for s in skills:
        resources = s.get("resources") or []
        if not resources:
            continue
        joined = ", ".join(f"{r['name']}/ ({r['summary']})" for r in resources)
        lines.append(f"- {s['name']}: {joined}")
    return "\n".join(lines)


class ProjectContext:
    """Resolved per-cwd project state, cached in PROJECT_CACHE."""

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.project_root = find_project_root(cwd)
        self.skills = discover_skills(self.project_root)
        self.skill_names = {s["name"] for s in self.skills}

    def _header(self) -> list[str]:
        skills_dir_lines = []
        seen_dirs = set()
        for s in self.skills:
            parent = str(Path(s["dir"]).parent)
            if parent not in seen_dirs:
                seen_dirs.add(parent)
                skills_dir_lines.append(parent)
        parts = [
            "[Krull Project Context]",
            f"Project root: {self.project_root}",
            f"Working directory: {self.cwd}",
        ]
        if skills_dir_lines:
            parts.append("Skills directories: " + ", ".join(skills_dir_lines))
        return parts

    def _footer(self) -> list[str]:
        # Shell rules used to live here, but folding them into the bottom
        # of a 3 KB project-context message demoted them out of the
        # model's prime attention zone. They're now injected as a
        # standalone system message at the front via inject_shell_rules,
        # alongside TOOL_GUIDANCE/DATE/TRUTH_GUARD where they get the same
        # anchoring benefit as the other instructional filters.
        #
        # The "PreToolUse hook error" environmental note also used to live
        # here. Removed because the literal hook error string is rendered
        # client-side by Claude Code as a display annotation and is not
        # transmitted in the API content. The model can't act on something
        # it can't see; the advice was a no-op.
        return [
            "[End Krull Project Context]",
        ]

    def full_message(self, budget_chars: int) -> str:
        """Full message including the skill listing. Used on turn 1."""
        listing = format_skill_listing(self.skills, budget_chars)
        resources = format_resources_section(self.skills)
        parts = self._header()
        if listing:
            parts += ["", "Available skills:", listing]
        if resources:
            parts += [
                "",
                "Skill resources (top-level subdirectories visible to the proxy — read or list these as the skill's procedure directs):",
                resources,
            ]
        parts += self._footer()
        return "\n".join(parts)

    def static_message(self) -> str:
        """Compact message without the skill listing. Used on turns 2+."""
        parts = self._header() + self._footer()
        return "\n".join(parts)


# Cache by absolute cwd; cheap to rebuild and small per entry.
PROJECT_CACHE: dict[str, ProjectContext] = {}

# Per-session delta tracking. Session keyed by hash of (cwd, first user
# message), so the same conversation across multiple turns hits the same
# entry but distinct sessions get distinct entries. Mirrors sentSkillNames
# tracking in attachments.ts:2699-2730.
SESSION_STATE: dict[str, dict] = {}


def _session_key(cwd: str, messages: list) -> str:
    first_user = ""
    for m in messages:
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict)
                )
            first_user = (c or "")[:200]
            break
    h = hashlib.sha256(f"{cwd}\x00{first_user}".encode("utf-8")).hexdigest()
    return h[:16]


def get_project_context(cwd: str) -> ProjectContext:
    pc = PROJECT_CACHE.get(cwd)
    if pc is None:
        pc = ProjectContext(cwd)
        PROJECT_CACHE[cwd] = pc
        proxy_log("CONTEXT", f"cwd={cwd} project_root={pc.project_root} "
                  f"skills={len(pc.skills)} ({','.join(s['name'] for s in pc.skills[:8])}"
                  f"{'…' if len(pc.skills) > 8 else ''})",
                  data={"cwd": cwd, "project_root": pc.project_root,
                        "skills": [s["name"] for s in pc.skills]})
    return pc


def inject_project_context(messages: list) -> list:
    """Inject a single project-context system message on every turn.

    Position: right AFTER the contiguous run of system messages at the
    start of the list. This is critical — small models like the qwen 9B
    anchor on whatever they read first, so Claude Code's main 'you are
    an agent that uses tools' prompt must come before our addendum.
    Inserting at position 0 (which is what insert(0, ...) does) buries
    Claude Code's prompt under our context and the model stops calling
    tools entirely.

    Content:
      - Turn 1: full message with header, skill listing, helpers, shell
        rules, env notes.
      - Turns 2+: static message (header + shell rules + env notes,
        no listing). Skill listing is delta-only via sentSkillNames,
        mirroring attachments.ts:2699-2730. If new skills appear, the
        full message is sent again.
    """
    cwd = extract_cwd_from_messages(messages)
    if not cwd:
        return messages

    pc = get_project_context(cwd)
    sess_key = _session_key(cwd, messages)
    state = SESSION_STATE.setdefault(sess_key, {"sent_skills": set(), "sent_full": False})

    new_skills = pc.skill_names - state["sent_skills"]
    if not state["sent_full"] or new_skills:
        body = pc.full_message(_get_char_budget())
        mode = "full" if not state["sent_full"] else f"delta(+{len(new_skills)})"
        state["sent_full"] = True
        state["sent_skills"] = set(pc.skill_names)
    else:
        body = pc.static_message()
        mode = "static"

    # Avoid duplicates if our message is already present (e.g., a re-run
    # within the same request). Match on the unique header tag.
    for m in messages:
        if m.get("role") == "system" and "[Krull Project Context]" in _content_text(m.get("content", "")):
            return messages

    proxy_log("FILTER", f"+project_context ({mode}, {len(body)} chars)",
              data={"mode": mode, "chars": len(body)})
    return _insert_after_system_messages(messages, body)


# NOTE: A "hook_error_hint" filter used to live here. It was supposed to
# detect "PreToolUse:Bash hook error" in tool results and inject a
# recovery hint telling the model to prefer Read/Grep/Glob over Bash on
# projects with broken hooks. Investigation revealed it is structurally
# impossible: the literal string "PreToolUse:Bash hook error" never
# appears in any tool result content the proxy receives. It is rendered
# client-side by Claude Code as a display annotation, NOT transmitted in
# the API content. The proxy only sees the *actual* tool result (often
# the script output, sometimes empty, sometimes wrapped in
# <tool_use_error> for Skill/Read errors but never for hook blocks).
# We grep'd every unique short tool output from the proxy's history and
# found zero matches for "PreToolUse" anywhere in any tool result. Dead
# code removed. The data starvation warning's _FAILURE_PATTERNS list also
# carries the same dead PreToolUse regex; it is left in place there for
# now as a no-op (the same investigation result applies — it can never
# fire) but could be cleaned up later. If we ever find evidence the proxy
# CAN see hook errors in some shape, detection can be re-added — but
# with a real signal, not the literal string we were searching for.


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    return str(content) if content is not None else ""


async def apply_filters(
    messages: list,
    has_tools: bool = False,
    tools: list | None = None,
) -> list:
    """Run all enabled inlet filters on the messages.

    Each pre-existing filter inserts at position 0, so the LAST one to run
    ends up at position 0 in the final list. The order below is chosen
    deliberately so the final layout is:

      [0] TOOL_GUIDANCE              ← qwen 9B's tool-use primer (must be 0)
      [1] KRULL_ATOMIC_PLAN_RUBRIC   ← only when TaskCreate is in tools
      [2] KRULL_SHELL_RULES          ← bash quoting pattern, prominent slot
      [3] DATE
      [4] TRUTH_GUARD
      [5] Claude Code's main system prompt
      [6] KRULL_PROJECT_CONTEXT      ← inserted AFTER Claude Code's prompt
      [7..] user/assistant messages

    We tried moving TOOL_GUIDANCE/DATE/TRUTH_GUARD to insert-after-system as
    well. It broke tool-calling behavior in the qwen 9B — turns out
    front-loaded TOOL_GUIDANCE was implicitly serving as the model's
    tool-use primer. Burying it past Claude Code's main prompt caused the
    model to stop calling tools. So only the project context (which is too
    large to front-load) gets the insert-after treatment; the small
    front-loaded filters stay where they were.

    KRULL_SHELL_RULES used to live inside the project context message
    footer. Folding a small instructional callout into the bottom of a
    3 KB context block demoted it past the model's attention zone — the
    rules were technically present but the model wasn't applying them.
    Pulling them back out into a dedicated front-loaded slot is what
    makes them effective.

    KRULL_ATOMIC_PLAN_RUBRIC fires only when TaskCreate is among the
    request's tool definitions — that's the signal the model is in (or
    about to enter) planning mode. The same anchoring logic that applies
    to TOOL_GUIDANCE and SHELL_RULES applies here: it's a small
    instructional callout that needs to be at the front of the stack to
    be effective on a small model. The 'tools' parameter is now threaded
    through from the request handlers so this filter (and future ones)
    can inspect what's available.
    """
    if ENABLE_TRUTH_GUARD:
        messages = inject_truth_guard(messages)
    if ENABLE_DATE:
        messages = inject_date(messages)
    messages = inject_shell_rules(messages)
    # NOTE: atomic_plan_rubric injection (was triggered when TaskCreate is
    # in the tool list) is DISABLED while we evaluate whether it's making
    # the model behave worse. Symptom: the rubric tells the model
    # "EXACTLY ONE TOOL CALL per task" + "look up one term at a time",
    # which the model interprets as "decompose phrases into word-by-word
    # lookups and read as few reference files as possible". Test case:
    # /english-to-cw "How are you?" went from successfully calling
    # cw-reverse-lookup.sh with the whole phrase (returning the canonical
    # "qʰata mayka?" entry from the supplement) to calling it with
    # ["how", "are", "you", "greeting"] and getting the buggy
    # "are → suquamish-iliʔi" word-by-word result. The constant and
    # function are still defined — re-enable by uncommenting the line
    # below if the test shows the rubric was load-bearing somewhere.
    # if _has_tool_named(tools, "TaskCreate"):
    #     messages = inject_atomic_plan_rubric(messages)
    if has_tools:
        # Inject tool usage guidance so the model uses correct parameter names.
        # KEEP THIS AT POSITION 0 — see apply_filters docstring.
        messages.insert(0, {"role": "system", "content": TOOL_GUIDANCE})
        proxy_log("FILTER", "+tool_guidance")
        # Language-aware Kiwix devdocs hint. Inserts at position 1 so it
        # sits right behind TOOL_GUIDANCE without dislodging the tool-use
        # anchor. Only fires when a language signal is detected in the
        # latest user message.
        messages = await inject_lang_docs(messages, has_tools=True)
    else:
        # Order matters: each injector prepends its block to the user
        # message, so the first one to run becomes the LAST block before
        # the user's question, AND its prepend bloats the message such
        # that subsequent injectors would search on the wrong text. Run
        # kiwix and map_search first (they need a clean query), then
        # web_search last so its results sit closest to the question.
        if ENABLE_KIWIX:
            messages = await inject_kiwix(messages)
        if ENABLE_MAP_SEARCH:
            messages = await inject_map_search(messages)
        if ENABLE_WEB_SEARCH:
            messages = await inject_web_search(messages)
    messages = inject_project_context(messages)
    messages = inject_slash_command_protocol(messages)
    messages = inject_procedure_maps(messages)
    messages = inject_loop_break(messages)
    messages = inject_data_starvation_warning(messages)
    messages = inject_stalled_progress_warning(messages)
    messages = truncate_large_tool_results(messages)
    messages = compact_context(messages)
    # Synthesis directive goes LAST — after compact_context — so it lands
    # at the very end of the messages array, the highest-attention slot.
    # Must be after truncation (which may reduce tool result sizes) and
    # after compaction (which may rearrange messages). The directive's
    # detection logic counts substantial tool results in the final message
    # state, so running it here sees the true picture.
    messages = inject_synthesis_directive(messages)
    return messages


# ── Ollama native API helpers ─────────────────────────────────────────────

def chat_to_ollama_request(chat_body: dict) -> dict:
    """Convert Chat Completions request to Ollama /api/chat format with num_ctx."""
    # Ollama native API expects tool_call arguments as dicts, not JSON strings.
    # Deep-copy messages and fix any string arguments.
    messages = []
    for msg in chat_body.get("messages", []):
        msg = dict(msg)
        if "tool_calls" in msg:
            fixed_tcs = []
            for tc in msg["tool_calls"]:
                tc = dict(tc)
                if "function" in tc:
                    func = dict(tc["function"])
                    args = func.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            func["arguments"] = json.loads(args)
                        except json.JSONDecodeError:
                            func["arguments"] = {}
                    tc["function"] = func
                fixed_tcs.append(tc)
            msg["tool_calls"] = fixed_tcs
        messages.append(msg)

    # Keep the model loaded in GPU memory for 30 minutes after each
    # request. Default is 5m, which causes cold-start evictions mid-session
    # when tool-call loops take longer than expected between API calls.
    keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

    ollama_body = {
        "model": chat_body.get("model", ""),
        "messages": messages,
        "stream": chat_body.get("stream", False),
        "keep_alive": keep_alive,
        # Small-model determinism override: force temperature=0 and top_p=1
        # so the same prompt produces the same output every run. Default
        # Ollama temperature is ~0.7, which makes the qwen 9B's "use tools"
        # vs "answer from training" choice essentially a coin flip on
        # marginal queries. The previous "qʰata mayka?" success was a lucky
        # roll of those dice; the next run with the same input produced
        # 1027 chars of hallucination. Tool-driven workflows are far more
        # valuable as reproducible than as creatively varied.
        "options": {
            "num_ctx": NUM_CTX,
            "temperature": 0.0,
            "top_p": 1.0,
        },
    }
    if "tools" in chat_body:
        ollama_body["tools"] = chat_body["tools"]
    if "max_tokens" in chat_body:
        ollama_body["options"]["num_predict"] = chat_body["max_tokens"]
    # Honor explicit temperature/top_p from the client only if non-zero —
    # we still allow callers to opt back into sampling if they want.
    if chat_body.get("temperature"):
        ollama_body["options"]["temperature"] = chat_body["temperature"]
    if chat_body.get("top_p"):
        ollama_body["options"]["top_p"] = chat_body["top_p"]
    return ollama_body


def ollama_response_to_chat(ollama_resp: dict, model: str = "") -> dict:
    """Convert Ollama /api/chat response to Chat Completions format."""
    msg = ollama_resp.get("message", {})
    choice = {
        "index": 0,
        "message": {
            "role": msg.get("role", "assistant"),
            "content": msg.get("content", ""),
        },
        "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop",
    }

    if msg.get("tool_calls"):
        tool_calls = []
        for i, tc in enumerate(msg["tool_calls"]):
            func = tc.get("function", {})
            args = func.get("arguments", {})
            if isinstance(args, dict):
                args = json.dumps(args)
            tool_name = func.get("name", "")
            args = fix_tool_call_params(tool_name, args)
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "index": i,
                "type": "function",
                "function": {"name": tool_name, "arguments": args},
            })
        choice["message"]["tool_calls"] = tool_calls

    # Usage from eval_count/prompt_eval_count
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or ollama_resp.get("model", ""),
        "choices": [choice],
        "usage": {
            "prompt_tokens": ollama_resp.get("prompt_eval_count", 0),
            "completion_tokens": ollama_resp.get("eval_count", 0),
            "total_tokens": ollama_resp.get("prompt_eval_count", 0) + ollama_resp.get("eval_count", 0),
        },
    }


# ── Responses API → Chat Completions translation ─────────────────────────

def responses_input_to_messages(input_items):
    """Convert Responses API 'input' to Chat Completions 'messages'."""
    messages = []
    for item in input_items:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        item_type = item.get("type", "")
        if item_type == "message":
            role = item.get("role", "user")
            content_parts = item.get("content", [])
            if isinstance(content_parts, str):
                messages.append({"role": role, "content": content_parts})
            else:
                text_parts = []
                for part in content_parts:
                    pt = part.get("type", "")
                    if pt in ("input_text", "text", "output_text"):
                        text_parts.append(part.get("text", ""))
                content = "\n".join(text_parts) if len(text_parts) > 1 else (text_parts[0] if text_parts else "")
                messages.append({"role": role, "content": content})
        elif item_type == "function_call":
            # Assistant's prior tool call — add as assistant message with tool_calls
            call_id = item.get("call_id", item.get("id", ""))
            args = item.get("arguments", "{}")
            if isinstance(args, dict):
                args = json.dumps(args)
            # Restore cached arguments if LiteLLM stripped them
            if args in ("{}", "", "null") and call_id in _tool_call_cache:
                args = _tool_call_cache[call_id]
                proxy_log("PROXY", f"Restored cached args for {call_id}: {args[:100]}")
            # Check if we can merge with the previous assistant message
            if messages and messages[-1].get("role") == "assistant" and "tool_calls" in messages[-1]:
                messages[-1]["tool_calls"].append({
                    "id": item.get("call_id", item.get("id", "")),
                    "type": "function",
                    "function": {"name": item.get("name", ""), "arguments": args},
                })
            else:
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": item.get("call_id", item.get("id", "")),
                        "type": "function",
                        "function": {"name": item.get("name", ""), "arguments": args},
                    }],
                })
        elif item_type == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": item.get("output", ""),
            })
    return messages


def responses_request_to_chat(data):
    """Convert a Responses API request body to Chat Completions format."""
    messages = responses_input_to_messages(data.get("input", []))

    if data.get("instructions"):
        messages.insert(0, {"role": "system", "content": data["instructions"]})

    result = {"model": data.get("model", ""), "messages": messages}

    if "max_output_tokens" in data:
        result["max_tokens"] = data["max_output_tokens"]
    if "stream" in data:
        result["stream"] = data["stream"]
    if "temperature" in data:
        result["temperature"] = data["temperature"]
    if "top_p" in data:
        result["top_p"] = data["top_p"]
    if "tools" in data:
        tools = []
        for tool in data["tools"]:
            if "function" in tool:
                tools.append(tool)
            else:
                # Responses API flat format → Chat Completions nested format
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    },
                })
        result["tools"] = filter_tools(tools)

    return result


def chat_response_to_responses(chat_data, model=""):
    """Convert a Chat Completions response to Responses API format."""
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    choices = chat_data.get("choices", [])
    output = []

    for choice in choices:
        msg = choice.get("message", {})
        content_parts = []

        if msg.get("content"):
            content_parts.append({
                "type": "output_text",
                "text": msg["content"],
                "annotations": [],
            })

        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_name = tc["function"]["name"]
                tc_args = fix_tool_call_params(tc_name, tc["function"]["arguments"])
                output.append({
                    "type": "function_call",
                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                    "call_id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                    "name": tc_name,
                    "arguments": tc_args,
                    "status": "completed",
                })

        if content_parts:
            output.append({
                "type": "message",
                "id": msg_id,
                "role": "assistant",
                "content": content_parts,
                "status": "completed",
            })

    usage = chat_data.get("usage", {})
    return {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model or chat_data.get("model", ""),
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": True,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "user": None,
        "metadata": {},
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
    }


# ── Streaming Responses API adapter ──────────────────────────────────────

class StreamAdapter:
    """Converts Chat Completions SSE chunks to Responses API SSE events."""

    def __init__(self, resp_id, msg_id, model):
        self.resp_id = resp_id
        self.msg_id = msg_id
        self.model = model
        self.full_text = ""
        self.has_text_output = False
        self.output_index = 0
        self.content_index = 0
        self.pending = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.tool_calls = {}

    def _emit(self, event_type, data):
        self.pending.append((event_type, data))

    def start(self):
        base = {
            "id": self.resp_id, "object": "response", "status": "in_progress",
            "model": self.model, "output": [],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
        self._emit("response.created", {"type": "response.created", "response": base})
        self._emit("response.in_progress", {"type": "response.in_progress", "response": base})

    def feed(self, chunk):
        usage = chunk.get("usage")
        if usage:
            self.input_tokens = usage.get("prompt_tokens", self.input_tokens)
            self.output_tokens = usage.get("completion_tokens", self.output_tokens)

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            finish = choice.get("finish_reason")

            content = delta.get("content")
            if content is not None:
                if not self.has_text_output:
                    self.has_text_output = True
                    self._emit("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": self.output_index,
                        "item": {"type": "message", "id": self.msg_id, "role": "assistant",
                                 "content": [], "status": "in_progress"},
                    })
                    self._emit("response.content_part.added", {
                        "type": "response.content_part.added",
                        "output_index": self.output_index, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    })
                self.full_text += content
                self._emit("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "output_index": self.output_index, "content_index": 0,
                    "delta": content,
                })

            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in self.tool_calls:
                    self.tool_calls[idx] = {"id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"), "name": "", "arguments": ""}
                if tc.get("id"):
                    self.tool_calls[idx]["id"] = tc["id"]
                if tc.get("function", {}).get("name"):
                    self.tool_calls[idx]["name"] = tc["function"]["name"]
                if tc.get("function", {}).get("arguments"):
                    self.tool_calls[idx]["arguments"] += tc["function"]["arguments"]

            if finish:
                self._finish()

    def _finish(self):
        if self.has_text_output:
            self._emit("response.output_text.done", {
                "type": "response.output_text.done",
                "output_index": self.output_index, "content_index": 0,
                "text": self.full_text,
            })
            self._emit("response.content_part.done", {
                "type": "response.content_part.done",
                "output_index": self.output_index, "content_index": 0,
                "part": {"type": "output_text", "text": self.full_text, "annotations": []},
            })
            self._emit("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": self.output_index,
                "item": {"type": "message", "id": self.msg_id, "role": "assistant",
                         "content": [{"type": "output_text", "text": self.full_text, "annotations": []}],
                         "status": "completed"},
            })
            self.output_index += 1

        for _idx, tc in sorted(self.tool_calls.items()):
            tc["arguments"] = fix_tool_call_params(tc["name"], tc["arguments"])
            # Emit function_call with empty arguments initially (like OpenAI does)
            initial_item = {
                "type": "function_call", "id": tc["id"], "call_id": tc["id"],
                "name": tc["name"], "arguments": "", "status": "in_progress",
            }
            self._emit("response.output_item.added", {
                "type": "response.output_item.added", "output_index": self.output_index, "item": initial_item,
            })
            # Cache arguments so we can restore them if LiteLLM strips them later
            _tool_call_cache[tc["id"]] = tc["arguments"]
            # Emit the arguments as a delta + done (LiteLLM reads args from these events)
            self._emit("response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta",
                "output_index": self.output_index,
                "delta": tc["arguments"],
            })
            self._emit("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "output_index": self.output_index,
                "arguments": tc["arguments"],
            })
            # Emit done with full arguments
            done_item = {
                "type": "function_call", "id": tc["id"], "call_id": tc["id"],
                "name": tc["name"], "arguments": tc["arguments"], "status": "completed",
            }
            self._emit("response.output_item.done", {
                "type": "response.output_item.done", "output_index": self.output_index, "item": done_item,
            })
            self.output_index += 1

        final_output = []
        if self.has_text_output:
            final_output.append({
                "type": "message", "id": self.msg_id, "role": "assistant",
                "content": [{"type": "output_text", "text": self.full_text, "annotations": []}],
                "status": "completed",
            })
        for _idx, tc in sorted(self.tool_calls.items()):
            final_output.append({
                "type": "function_call", "id": tc["id"], "call_id": tc["id"],
                "name": tc["name"], "arguments": tc["arguments"], "status": "completed",
            })

        self._emit("response.completed", {
            "type": "response.completed",
            "response": {
                "id": self.resp_id, "object": "response", "status": "completed",
                "model": self.model, "output": final_output,
                "usage": {
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "total_tokens": self.input_tokens + self.output_tokens,
                },
            },
        })

    def drain(self):
        events = self.pending
        self.pending = []
        return events


# ── Anthropic SSE patching (for direct /v1/messages passthrough) ──────────

def patch_usage_fields(event: dict) -> dict:
    if event.get("type") == "message_start":
        msg = event.get("message", {})
        usage = msg.get("usage", {})
        usage.setdefault("cache_creation_input_tokens", 0)
        usage.setdefault("cache_read_input_tokens", 0)
        msg["usage"] = usage
        if msg.get("content") is None:
            msg["content"] = []
        event["message"] = msg
    if event.get("type") == "message_delta":
        usage = event.get("usage", {})
        usage.setdefault("cache_creation_input_tokens", 0)
        usage.setdefault("cache_read_input_tokens", 0)
        event["usage"] = usage
    return event


# ── Main proxy handler ────────────────────────────────────────────────────

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
async def proxy(request: Request, path: str):
    is_responses = (path == "responses" and request.method == "POST")

    # Set session ID for this request's log entries
    session_id = get_session_id(request)
    _current_session_id.set(session_id)

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    body = await request.body()

    # ── Responses API: translate + apply filters + route to Ollama ─────
    if is_responses:
        try:
            resp_data = json.loads(body)
        except Exception as e:
            return Response(content=json.dumps({"error": str(e)}).encode(), status_code=400)

        # Debug: log the raw Responses API input items
        for i, item in enumerate(resp_data.get("input", [])):
            itype = item.get("type", "?") if isinstance(item, dict) else "string"
            if itype in ("function_call", "function_call_output"):
                proxy_log("PROXY", f"Input[{i}] type={itype}: {json.dumps(item)[:500]}")

        is_streaming = resp_data.get("stream", False)
        original_model = resp_data.get("model", "")
        chat_body = responses_request_to_chat(resp_data)

        # NOTE: Planning-lock (#15) is disabled. When narrowed to only the
        # 4 Task tools, the qwen 9B emits 40 chars of prose and zero tool
        # calls — it doesn't have the planning pattern in its training.
        # The benchmark succeeds with the full toolset because the model
        # can drive the workflow directly via Skill/Read/Bash. Removing
        # capability without giving the model an alternative just kills
        # the workflow. Helpers (maybe_lock_to_planning, etc.) remain
        # defined in case we want to revisit with a different shape.
        # chat_body["tools"] = maybe_lock_to_planning(
        #     chat_body.get("messages", []),
        #     chat_body.get("tools"),
        # )
        # Apply inlet filters to the messages
        has_tools = bool(chat_body.get("tools"))
        chat_body["messages"] = await apply_filters(
            chat_body["messages"],
            has_tools=has_tools,
            tools=chat_body.get("tools"),
        )

        # Hard cap: if the model has exceeded the total tool-call limit,
        # strip all tools and force a text-only response. This is the
        # nuclear option when loop_break and stalled_progress both fail.
        chat_body["messages"], chat_body["tools"] = apply_hard_tool_cap(
            chat_body["messages"], chat_body.get("tools"),
        )

        # Adaptive temperature: if the model is in a tool-call loop,
        # elevate temperature to give it variance to break out. Returns
        # 0.0 for normal operation; chat_to_ollama_request only honors
        # the override when it's > 0.
        elevated_temp = compute_session_temperature(chat_body["messages"])
        if elevated_temp > 0:
            chat_body["temperature"] = elevated_temp
            proxy_log("FILTER", f"+temp_escalation (loop detected, temp={elevated_temp})",
                      data={"temp": elevated_temp})

        # Convert to Ollama native format (supports options.num_ctx)
        ollama_body = chat_to_ollama_request(chat_body)
        upstream = f"{OLLAMA_URL}/api/chat"

        proxy_log("PROXY", f"Responses API → Ollama (stream={is_streaming} model={original_model} "
                  f"msgs={len(chat_body['messages'])} tools={len(chat_body.get('tools', []))})",
                  data={"stream": is_streaming, "model": original_model,
                        "msgs": len(chat_body["messages"]),
                        "tools": len(chat_body.get("tools", [])),
                        "ctx": NUM_CTX,
                        "temp": ollama_body["options"].get("temperature"),
                        "top_p": ollama_body["options"].get("top_p")})

        # TEMPORARY FORENSIC: dump every message's role + length + start/end
        # snippet so we can see exactly what the model is being given when
        # it produces the wrong final answer. Especially: does the slash
        # command directive persist into turn 2? Where does the loaded
        # skill content live in the request structure?
        print("[DEBUG-CONV] === full conversation dump ===", file=sys.stderr, flush=True)
        for _i, _m in enumerate(chat_body["messages"]):
            _role = _m.get("role", "?")
            _c = _m.get("content", "")
            if isinstance(_c, list):
                _c = " ".join(_p.get("text", "") if isinstance(_p, dict) else str(_p) for _p in _c)
            _c = str(_c) if _c is not None else ""
            _len = len(_c)
            _has_slash_dir = "[Krull Slash Command Protocol]" in _c
            _has_cmd_tags = "<command-name>" in _c
            _start = _c[:160].replace("\n", "\\n")
            _end = _c[-160:].replace("\n", "\\n") if _len > 160 else ""
            _flags = []
            if _has_slash_dir: _flags.append("HAS_DIR")
            if _has_cmd_tags: _flags.append("HAS_CMD_TAGS")
            _flag_str = f" [{','.join(_flags)}]" if _flags else ""
            print(f"[DEBUG-CONV]   msg[{_i}] role={_role} len={_len}{_flag_str}", file=sys.stderr, flush=True)
            print(f"[DEBUG-CONV]     start: {_start!r}", file=sys.stderr, flush=True)
            if _end:
                print(f"[DEBUG-CONV]     end:   {_end!r}", file=sys.stderr, flush=True)
        print("[DEBUG-CONV] === end dump ===", file=sys.stderr, flush=True)

        # Debug: log message roles and any tool_calls in messages
        for i, m in enumerate(chat_body["messages"]):
            role = m.get("role", "?")
            tc = m.get("tool_calls")
            content_len = len(str(m.get("content", "")))
            extra = f" tool_calls={len(tc)}" if tc else ""
            if role in ("assistant", "tool") or tc:
                proxy_log("PROXY", f"  msg[{i}] role={role} content_len={content_len}{extra}")
                if tc:
                    for t in tc:
                        proxy_log("PROXY", f"    tc: {json.dumps(t)[:300]}")

        # Grounded-answer interception. Fires when the model is at the
        # structural moment for a final answer (synthesis trigger met)
        # AND the request is in streaming mode (Claude Code's path).
        # Replaces the upstream streaming call with a non-streaming
        # format=schema call, then synthesizes a streaming response from
        # the unwrapped `answer` field. Citation verification appended
        # as an annotation. See _should_force_grounded_answer comment
        # block for the structural rationale.
        if is_streaming and _should_force_grounded_answer(chat_body["messages"]):
            flattened = _flatten_messages_for_format(chat_body["messages"])
            grounded = await _ollama_grounded_answer_call(
                flattened, ollama_body.get("model", original_model), NUM_CTX,
            )
            if grounded and isinstance(grounded.get("answer"), str):
                answer = grounded["answer"]
                sources_used = grounded.get("sources_used") or []
                all_tool_text = _collect_tool_result_text(chat_body["messages"])
                verified, total = _verify_citations(sources_used, all_tool_text)
                if total == 0:
                    citation_note = "\n\n[Note: answer not grounded in any cited tool result]"
                elif verified == total:
                    citation_note = ""  # all good, no need to annotate
                else:
                    citation_note = (
                        f"\n\n[Note: {verified}/{total} cited sources verified "
                        f"against tool results; {total - verified} could not be "
                        f"verified]"
                    )
                final_text = answer + citation_note
                proxy_log(
                    "FILTER",
                    f"+grounded_answer (cited {total} sources, "
                    f"{verified} verified, {len(answer)} chars)",
                    data={"sources_total": total, "sources_verified": verified,
                          "answer_chars": len(answer)},
                )

                adapter = StreamAdapter(
                    f"resp_{uuid.uuid4().hex[:24]}",
                    f"msg_{uuid.uuid4().hex[:24]}",
                    original_model,
                )
                adapter.start()
                # Feed the answer through the adapter as a single Chat
                # Completions chunk. Adapter will emit the proper
                # Responses API SSE events on drain.
                adapter.feed({
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": final_text},
                        "finish_reason": None,
                    }],
                })
                adapter.feed({
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": len(json.dumps(flattened)) // 4,
                        "completion_tokens": len(final_text) // 4,
                    },
                })
                proxy_log(
                    "STREAM",
                    f"done content_chars={len(final_text)} tool_calls=0 "
                    f"(grounded)",
                    data={"content_chars": len(final_text), "tool_calls": 0,
                          "grounded": True},
                )

                async def stream_grounded():
                    for et, ed in adapter.drain():
                        yield f"event: {et}\ndata: {json.dumps(ed)}\n\n"

                return StreamingResponse(
                    stream_grounded(), media_type="text/event-stream",
                )
            else:
                proxy_log(
                    "FILTER",
                    "+grounded_answer fallthrough (call failed or no JSON)",
                    level="warn",
                )
                # Fall through to normal streaming path

        if is_streaming:
            ollama_body["stream"] = True
            client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
            try:
                req = client.build_request("POST", upstream, json=ollama_body)
                resp = await client.send(req, stream=True)
                if resp.status_code >= 400:
                    error_body = await resp.aread()
                    await client.aclose()
                    proxy_log("PROXY", f"Ollama error: {resp.status_code} {error_body[:300]}", level="error")
                    return Response(content=error_body, status_code=resp.status_code, media_type="application/json")
            except Exception as e:
                await client.aclose()
                return Response(content=json.dumps({"error": str(e)}).encode(), status_code=502)

            adapter = StreamAdapter(
                f"resp_{uuid.uuid4().hex[:24]}",
                f"msg_{uuid.uuid4().hex[:24]}",
                original_model,
            )
            adapter.start()

            async def stream_responses():
                tc_counter = 0  # Running tool call index across chunks
                content_chars_total = 0
                tool_calls_total = 0
                try:
                    for et, ed in adapter.drain():
                        yield f"event: {et}\ndata: {json.dumps(ed)}\n\n"

                    buffer = ""
                    async for chunk in resp.aiter_bytes():
                        buffer += chunk.decode("utf-8", errors="replace")
                        # Ollama native API uses NDJSON (one JSON per line)
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                ollama_chunk = json.loads(line)
                                msg = ollama_chunk.get("message", {})
                                done = ollama_chunk.get("done", False)
                                # DEBUG: tally content + tool calls per stream
                                if msg.get("content"):
                                    content_chars_total += len(msg["content"])
                                if msg.get("tool_calls"):
                                    tool_calls_total += len(msg["tool_calls"])
                                if done:
                                    proxy_log("STREAM", f"done content_chars={content_chars_total} "
                                              f"tool_calls={tool_calls_total}",
                                              data={"content_chars": content_chars_total,
                                                    "tool_calls": tool_calls_total,
                                                    "prompt_tokens": ollama_chunk.get("prompt_eval_count", 0),
                                                    "completion_tokens": ollama_chunk.get("eval_count", 0)})
                                    if content_chars_total == 0 and tool_calls_total == 0:
                                        proxy_log("STREAM", f"EMPTY OUTPUT — last ollama chunk: "
                                                  f"{json.dumps(ollama_chunk)[:500]}",
                                                  level="warn")
                                chat_chunk = {
                                    "choices": [{
                                        "index": 0,
                                        "delta": {},
                                        "finish_reason": None,
                                    }],
                                }
                                if msg.get("content"):
                                    chat_chunk["choices"][0]["delta"]["content"] = msg["content"]
                                if msg.get("role"):
                                    chat_chunk["choices"][0]["delta"]["role"] = msg["role"]
                                if msg.get("tool_calls"):
                                    tc_deltas = []
                                    for tc in msg["tool_calls"]:
                                        func = tc.get("function", {})
                                        args = func.get("arguments", {})
                                        if isinstance(args, dict):
                                            args = json.dumps(args)
                                        tool_name = func.get("name", "")
                                        args = fix_tool_call_params(tool_name, args)
                                        call_id = f"call_{uuid.uuid4().hex[:8]}"
                                        _tool_call_cache[call_id] = args
                                        tc_deltas.append({
                                            "index": tc_counter,
                                            "id": call_id,
                                            "type": "function",
                                            "function": {"name": tool_name, "arguments": args},
                                        })
                                        tc_counter += 1
                                    chat_chunk["choices"][0]["delta"]["tool_calls"] = tc_deltas
                                    proxy_log("PROXY", f"Tool call: {json.dumps(tc_deltas)[:500]}")
                                if done:
                                    chat_chunk["choices"][0]["finish_reason"] = "tool_calls" if msg.get("tool_calls") else "stop"
                                    chat_chunk["usage"] = {
                                        "prompt_tokens": ollama_chunk.get("prompt_eval_count", 0),
                                        "completion_tokens": ollama_chunk.get("eval_count", 0),
                                    }

                                adapter.feed(chat_chunk)
                                for et, ed in adapter.drain():
                                    yield f"event: {et}\ndata: {json.dumps(ed)}\n\n"
                            except json.JSONDecodeError:
                                pass
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(stream_responses(), media_type="text/event-stream")

        else:
            ollama_body["stream"] = False
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(upstream, json=ollama_body)
                if resp.status_code >= 400:
                    proxy_log("PROXY", f"Ollama error: {resp.status_code} {resp.content[:300]}", level="error")
                    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
                try:
                    ollama_resp = resp.json()
                    chat_resp = ollama_response_to_chat(ollama_resp, original_model)
                    return Response(
                        content=json.dumps(chat_response_to_responses(chat_resp, original_model)).encode(),
                        status_code=200, media_type="application/json",
                    )
                except Exception as e:
                    proxy_log("PROXY", f"Translation error: {e}", level="error")
                    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")

    # ── Chat Completions passthrough (apply filters, use native Ollama API) ──
    if path in ("chat/completions", "v1/chat/completions", "api/chat/completions"):
        try:
            data = json.loads(body)
            # NOTE: Planning-lock (#15) is disabled. See /responses path
            # for rationale.
            # data["tools"] = maybe_lock_to_planning(
            #     data.get("messages", []),
            #     data.get("tools"),
            # )
            has_tools = bool(data.get("tools"))
            data["messages"] = await apply_filters(
                data.get("messages", []),
                has_tools=has_tools,
                tools=data.get("tools"),
            )
            # Hard cap: strip tools if model has exceeded total limit
            data["messages"], capped_tools = apply_hard_tool_cap(
                data["messages"], data.get("tools"),
            )
            if capped_tools is not None:
                data["tools"] = capped_tools
            if "tools" in data:
                data["tools"] = filter_tools(data["tools"])

            # Adaptive temperature: see /responses path for rationale.
            elevated_temp = compute_session_temperature(data["messages"])
            if elevated_temp > 0:
                data["temperature"] = elevated_temp
                proxy_log("FILTER", f"+temp_escalation (loop detected, temp={elevated_temp})")

            ollama_body = chat_to_ollama_request(data)
            upstream = f"{OLLAMA_URL}/api/chat"
            is_streaming = data.get("stream", False)

            if is_streaming:
                ollama_body["stream"] = True
                client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
                req = client.build_request("POST", upstream, json=ollama_body)
                resp = await client.send(req, stream=True)
                if resp.status_code >= 400:
                    error_body = await resp.aread()
                    await client.aclose()
                    return Response(content=error_body, status_code=resp.status_code, media_type="application/json")

                # Convert Ollama NDJSON stream to SSE for Chat Completions format
                async def stream_chat():
                    try:
                        buffer = ""
                        async for chunk in resp.aiter_bytes():
                            buffer += chunk.decode("utf-8", errors="replace")
                            while "\n" in buffer:
                                line, buffer = buffer.split("\n", 1)
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    ollama_chunk = json.loads(line)
                                    msg = ollama_chunk.get("message", {})
                                    done = ollama_chunk.get("done", False)
                                    chat_chunk = {"choices": [{"index": 0, "delta": {}, "finish_reason": None}]}
                                    if msg.get("content"):
                                        chat_chunk["choices"][0]["delta"]["content"] = msg["content"]
                                    if msg.get("tool_calls"):
                                        tcs = []
                                        for i, tc in enumerate(msg["tool_calls"]):
                                            func = tc.get("function", {})
                                            args = func.get("arguments", {})
                                            if isinstance(args, dict):
                                                args = json.dumps(args)
                                            tcs.append({"index": i, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                                        "function": {"name": func.get("name", ""), "arguments": args}})
                                        chat_chunk["choices"][0]["delta"]["tool_calls"] = tcs
                                    if done:
                                        chat_chunk["choices"][0]["finish_reason"] = "stop"
                                        chat_chunk["usage"] = {
                                            "prompt_tokens": ollama_chunk.get("prompt_eval_count", 0),
                                            "completion_tokens": ollama_chunk.get("eval_count", 0),
                                        }
                                    yield f"data: {json.dumps(chat_chunk)}\n\n"
                                except json.JSONDecodeError:
                                    pass
                        yield "data: [DONE]\n\n"
                    finally:
                        await resp.aclose()
                        await client.aclose()

                return StreamingResponse(stream_chat(), media_type="text/event-stream")
            else:
                ollama_body["stream"] = False
                async with httpx.AsyncClient(timeout=300.0) as client:
                    resp = await client.post(upstream, json=ollama_body)
                    if resp.status_code >= 400:
                        return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
                    ollama_resp = resp.json()
                    chat_resp = ollama_response_to_chat(ollama_resp)
                    return Response(content=json.dumps(chat_resp).encode(), status_code=200, media_type="application/json")

        except Exception as e:
            proxy_log("PROXY", f"Chat completions error: {e}", level="error")
            return Response(content=json.dumps({"error": str(e)}).encode(), status_code=500)

    # ── Generic passthrough ───────────────────────────────────────────
    upstream = f"{OLLAMA_URL}/{path}"
    if request.url.query:
        upstream += f"?{request.url.query}"

    proxy_log("PROXY", f"Passthrough {request.method} /{path} -> {upstream}")

    if request.method == "HEAD":
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.head(upstream, headers=headers)
            return Response(content=b"", status_code=resp.status_code)

    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.request(method=request.method, url=upstream, headers=headers, content=body)
        return Response(content=resp.content, status_code=resp.status_code,
                        headers={k: v for k, v in resp.headers.items() if k.lower() not in STRIP_HEADERS},
                        media_type=resp.headers.get("content-type"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8081"))
    uvicorn.run(app, host="0.0.0.0", port=port)
