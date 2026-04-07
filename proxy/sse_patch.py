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
    os.environ.get("KRULL_TOOL_RESULT_MAX_CHARS", "20000")
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
        print(f"[PROXY] Filtered tools: {len(tools)} → {len(filtered)} "
              f"({', '.join(ALLOWED_TOOLS & {t.get('function', t).get('name', t.get('name', '')) for t in filtered})})",
              file=sys.stderr, flush=True)
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
            print(f"[PROXY] Stripped invalid params from {tool_name}: {removed}",
                  file=sys.stderr, flush=True)
            args = stripped
            changed = True

    if changed:
        print(f"[PROXY] Fixed tool params for {tool_name}: {list(args.keys())}",
              file=sys.stderr, flush=True)
        return json.dumps(args)
    return arguments if isinstance(arguments, str) else json.dumps(arguments)


# ── Inlet Filters ─────────────────────────────────────────────────────────

ENABLE_TRUTH_GUARD = os.environ.get("ENABLE_TRUTH_GUARD", "true").lower() == "true"
ENABLE_MAP_SEARCH = os.environ.get("ENABLE_MAP_SEARCH", "true").lower() == "true"
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
    print("[FILTER] +shell_rules", file=sys.stderr, flush=True)
    return messages


def inject_atomic_plan_rubric(messages: list) -> list:
    """Inject the Atomic Plan Rubric as a dedicated system message at
    the start. Only fired when the request includes the TaskCreate tool
    (the signal that the model is in or about to enter planning mode).
    Same positioning rationale as inject_shell_rules — small standalone
    callout near the front of the stack, where the small model can
    actually attend to it."""
    messages.insert(0, {"role": "system", "content": KRULL_ATOMIC_PLAN_RUBRIC})
    print("[FILTER] +atomic_plan_rubric", file=sys.stderr, flush=True)
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
    "The /{skill_name} skill content has just loaded. You have already "
    "invoked Skill(skill=\"{skill_name}\"). Your job NOW is to follow "
    "this skill's defined procedure for the user's request:\n"
    "    {args}\n"
    "\n"
    "Procedure to follow:\n"
    "  1. Look for a procedure file in the skill's references/ "
    "directory (e.g. references/<mode>-procedure.md). Read it.\n"
    "  2. Follow the procedure step by step. If the procedure says "
    "to call a helper script (e.g. lib/<helper>.sh), call it via "
    "Bash. If it says to read a reference file, read it.\n"
    "  3. Each step uses real tool calls and real results. Cite the "
    "actual content you got back, not training knowledge.\n"
    "\n"
    "Do NOT:\n"
    "  - Refuse the user's input because the skill's surface "
    "description (e.g. 'this is for course files') doesn't seem to "
    "match perfectly. Try the procedure FIRST. The procedure may "
    "handle your input fine.\n"
    "  - Produce a final answer from training knowledge while "
    "claiming you 'used the skill'. Use the procedure's actual tool "
    "calls and cite their actual results.\n"
    "  - Invent words, definitions, citations, or facts to fill "
    "gaps. An honest 'the procedure does not appear to handle this "
    "input — the skill may need an updated procedure file' is "
    "always better than a fabricated answer.\n"
    "\n"
    "The procedure exists for a reason. Follow it.\n"
    "[End Krull Skill Follow-Through]"
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

    # Pick the right template for this shape
    escaped_args = args.replace('"', '\\"')
    if shape == "initial":
        directive = SLASH_COMMAND_PROTOCOL_TEMPLATE.format(
            skill_name=skill_name, args=escaped_args
        )
        log_label = "+slash_command_protocol"
    else:  # shape == "loaded"
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

    print(
        f"[FILTER] {log_label} (/{skill_name}, shape={shape}, "
        f"+{len(directive)} chars to user msg)",
        file=sys.stderr,
        flush=True,
    )
    return messages


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
    print(
        f"[FILTER] +loop_break ({tool_name} × {count} → injecting reminder)",
        file=sys.stderr,
        flush=True,
    )
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

    stalled = count_consecutive_tool_turns(messages)
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
    print(
        f"[FILTER] +data_starvation_warning ({failures}/{total} recent tool calls failed)",
        file=sys.stderr,
        flush=True,
    )
    return _insert_after_system_messages(messages, hint)


# Stalled progress detection (task #23). The loop detector catches
# "same tool N times in a row" but misses spelunking loops where the
# model alternates between tools (Read, Grep, Read, Grep, Read, ...)
# at random offsets/patterns hoping to find content that isn't where
# it's looking. The data starvation detector misses it too because
# the tool results are technically "successful" — Reads return real
# content, Greps return real matches, just nothing relevant to the
# task. The model keeps grinding because every individual call looks
# productive even though the workflow as a whole is stuck.
#
# Detection: count consecutive assistant tool-call turns since the
# last "pure text" assistant response. If too many in a row, the
# model is exploring without converging and we intervene.


def count_consecutive_tool_turns(messages: list) -> int:
    """Count how many recent assistant messages were tool-call turns
    (had tool_calls). A 'pure text' assistant response (no tool_calls,
    significant content) resets the counter — that's the model
    actually answering the user. Walks backward from the end."""
    count = 0
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        tcs = m.get("tool_calls") or []
        content_text = _content_text(m.get("content", "") or "").strip()
        if tcs:
            count += 1
            continue
        # Pure assistant text response (no tool_calls). If it's
        # substantive (>100 chars), it's a real answer — reset.
        # Short content with no tool calls is usually a placeholder
        # or "okay, I'll do that next" filler — keep counting.
        if len(content_text) > 100:
            break
    return count


STALLED_PROGRESS_HINT_TEMPLATE = (
    "[Krull Stalled Progress Warning]\n"
    "You have made {count} consecutive tool calls without producing "
    "a final answer to the user. This pattern means you are stuck in "
    "an exploration loop — tools are running but the workflow is not "
    "converging on an answer.\n"
    "\n"
    "STOP exploring. Whatever search/lookup strategy you have been "
    "using is not working. Repeating it with different parameters "
    "(different file offsets, different grep patterns, different URLs) "
    "will not change the result.\n"
    "\n"
    "On your next turn, do ONE of the following:\n"
    "  1. SUMMARIZE what you know AND what you do not know. Tell the "
    "user honestly what you tried, what worked, and what failed.\n"
    "  2. Give the best honest answer you can with the information "
    "you have already gathered. Cite the specific tool results you "
    "are drawing from.\n"
    "  3. Ask the user for guidance on a different approach.\n"
    "\n"
    "Do NOT call another tool unless you have a fundamentally different "
    "strategy than what you have been doing. Do NOT make up details to "
    "fill gaps in what you found. An honest 'I tried X, Y, Z and could "
    "not find this' is always better than a confident wrong answer.\n"
    "[End Krull Stalled Progress Warning]"
)


def inject_stalled_progress_warning(messages: list) -> list:
    """If the model has made KRULL_STALLED_PROGRESS_THRESHOLD or more
    consecutive tool-call turns without a substantive text response,
    inject a warning telling it to stop exploring and summarize."""
    count = count_consecutive_tool_turns(messages)
    if count < KRULL_STALLED_PROGRESS_THRESHOLD:
        return messages

    # Idempotent
    for m in messages:
        if m.get("role") == "system" and "[Krull Stalled Progress Warning]" in _content_text(m.get("content", "")):
            return messages

    hint = STALLED_PROGRESS_HINT_TEMPLATE.format(count=count)
    print(
        f"[FILTER] +stalled_progress_warning ({count} consecutive tool turns without text answer)",
        file=sys.stderr,
        flush=True,
    )
    return _insert_after_system_messages(messages, hint)


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
        print(
            "[FILTER] planning_lock skipped: no planning tools in request",
            file=sys.stderr,
            flush=True,
        )
        return tools
    if len(narrowed) < len(tools):
        print(
            f"[FILTER] +planning_lock (narrowed {len(tools)}→{len(narrowed)} tools, "
            f"slash-command query, no prior TaskCreate)",
            file=sys.stderr,
            flush=True,
        )
        return narrowed
    return tools


def inject_truth_guard(messages: list) -> list:
    """Inject truth guard rules as a system message at the start.

    Note: This inserts at position 0 (in front of Claude Code's main
    prompt). We tried moving it to _insert_after_system_messages and
    it broke tool-calling behavior in the qwen 9B — see
    sharded-hopping-canyon.md. The qwen 9B anchors heavily on whatever
    is at position 0; the existing front-loaded filters were
    accidentally serving as the model's tool-use primer, and removing
    that primer caused the model to stop calling tools. Only the Krull
    project context is positioned after Claude Code's prompt; the
    pre-existing filters stay where they were."""
    messages.insert(0, {"role": "system", "content": TRUTH_GUARD_CONTENT})
    print("[FILTER] +truth_guard", file=sys.stderr, flush=True)
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
    print("[FILTER] +date", file=sys.stderr, flush=True)
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
        print(f"[PROXY] Web search error: {e}", file=sys.stderr, flush=True)

    return messages


def _xml_element_text(el) -> str:
    """Extract all text from an XML element, including text within child tags."""
    import xml.etree.ElementTree as ET
    raw = ET.tostring(el, encoding="unicode", method="text")
    return raw.strip() if raw else ""


async def inject_kiwix(messages: list) -> list:
    """Search Kiwix for relevant offline knowledge with full-text snippets."""
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

    try:
        # Use the full-text search API (XML format) to get content snippets
        search_url = (
            f"{KIWIX_URL}/search"
            f"?pattern={urllib.parse.quote(query)}&format=xml&pageLength=3"
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(search_url)
            if resp.status_code != 200:
                return messages
            xml_text = resp.text

        # Parse the XML response for titles, snippets, and sources
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return messages

        items = channel.findall("item")
        if not items:
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

    except Exception as e:
        print(f"[PROXY] Kiwix error: {e}", file=sys.stderr, flush=True)

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
        print(f"[PROXY] Map search error: {e}", file=sys.stderr, flush=True)

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

    The model can re-Read specific lines via offset/limit if it needs
    content beyond the cap — the truncation marker tells it how.

    General-purpose: works for any tool (Read, Bash, Grep, etc.), any
    skill, any project. No skill-specific logic.

    Idempotent: tool results that already contain the truncation marker
    are passed through unchanged.
    """
    new_messages = []
    truncated = 0
    saved_chars = 0
    cap = KRULL_TOOL_RESULT_MAX_CHARS
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
        kept = content[:cap]
        dropped = len(content) - cap
        marker = (
            f"\n\n{_TRUNCATION_MARKER}: {dropped} chars dropped to keep "
            f"the conversation small enough for the model's working "
            f"window. If you need a different part of this content, "
            f"re-Read the source with offset/limit parameters to fetch "
            f"specific lines.]"
        )
        new_msg = dict(m)
        new_msg["content"] = kept + marker
        new_messages.append(new_msg)
        truncated += 1
        saved_chars += dropped
    if truncated > 0:
        print(
            f"[FILTER] +tool_result_truncate ({truncated} results capped, "
            f"{saved_chars} chars saved, cap={cap})",
            file=sys.stderr,
            flush=True,
        )
    return new_messages


def compact_context(messages: list) -> list:
    """Compact older messages when approaching the context window limit.

    Keeps system messages and recent conversation intact, summarizes
    older conversation into a single system message.
    """
    max_ctx = int(os.environ.get("CONTEXT_COMPACT_LIMIT", str(NUM_CTX)))
    threshold = int(max_ctx * 0.75)
    preserve_recent = 6  # message pairs

    # Estimate tokens (~4 chars per token)
    total_tokens = sum(
        len(m.get("content", "") if isinstance(m.get("content", ""), str) else str(m.get("content", ""))) // 4 + 4
        for m in messages
    )

    if total_tokens <= threshold:
        return messages

    system_messages = [m for m in messages if m.get("role") == "system"]
    conversation = [m for m in messages if m.get("role") != "system"]

    preserve_count = min(preserve_recent * 2, len(conversation))
    if preserve_count >= len(conversation):
        return messages

    old_messages = conversation[:-preserve_count]
    recent_messages = conversation[-preserve_count:]

    summary_parts = []
    for msg in old_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if content:
            if len(content) > 300:
                content = content[:300] + "..."
            summary_parts.append(f"[{role}]: {content}")

    if not summary_parts:
        return messages

    compact_msg = {
        "role": "system",
        "content": (
            "[Context Manager: Earlier conversation compacted to fit context window.]\n\n"
            f"=== Earlier Conversation Summary ===\n" + "\n".join(summary_parts) +
            "\n=== End Summary ===\n\n"
            "Continue the conversation naturally based on this context."
        ),
    }

    compacted = system_messages + [compact_msg] + recent_messages
    new_tokens = sum(
        len(m.get("content", "") if isinstance(m.get("content", ""), str) else "") // 4 + 4
        for m in compacted
    )
    print(f"[PROXY] Context compacted: {total_tokens} → {new_tokens} est. tokens "
          f"({len(old_messages)} msgs summarized, {len(recent_messages)} kept)",
          file=sys.stderr, flush=True)
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
        print(
            f"[CONTEXT-DEBUG] cwd parse failed; system_msgs={len(sys_msgs)} "
            f"first_snippet={snippet!r}",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            f"[CONTEXT-DEBUG] cwd parse failed; no system messages "
            f"(total_msgs={len(messages)}, roles={[m.get('role') for m in messages[:5]]})",
            file=sys.stderr,
            flush=True,
        )
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
        # Convention: lib/*.sh deterministic helpers, references/*.md procedure
        helpers = []
        lib_dir = entry / "lib"
        if lib_dir.is_dir():
            try:
                helpers = sorted(p.name for p in lib_dir.glob("*.sh"))
            except OSError:
                helpers = []
        procedures = []
        ref_dir = entry / "references"
        if ref_dir.is_dir():
            try:
                procedures = sorted(p.name for p in ref_dir.glob("*-procedure.md"))
            except OSError:
                procedures = []
        skills.append({
            "name": fm.get("name") or entry.name,
            "description": fm.get("description", ""),
            "whenToUse": fm.get("whenToUse", ""),
            "dir": str(entry),
            "helpers": helpers,
            "procedures": procedures,
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


def format_helpers_section(skills: list[dict]) -> str:
    """List skills that ship deterministic helpers (lib/*.sh)."""
    lines = []
    for s in skills:
        if s.get("helpers"):
            joined = ", ".join(s["helpers"])
            lines.append(f"- {s['name']} ships lib/{{{joined}}}")
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
        helpers = format_helpers_section(self.skills)
        parts = self._header()
        if listing:
            parts += ["", "Available skills:", listing]
        if helpers:
            parts += [
                "",
                "Skill helpers known to this proxy (deterministic, runnable directly):",
                helpers,
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
        print(
            f"[CONTEXT] cwd={cwd} project_root={pc.project_root} "
            f"skills={len(pc.skills)} ({','.join(s['name'] for s in pc.skills[:8])}"
            f"{'…' if len(pc.skills) > 8 else ''})",
            file=sys.stderr,
            flush=True,
        )
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

    print(f"[FILTER] +project_context ({mode}, {len(body)} chars)", file=sys.stderr, flush=True)
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
        print("[FILTER] +tool_guidance", file=sys.stderr, flush=True)
    else:
        if ENABLE_WEB_SEARCH:
            messages = await inject_web_search(messages)
        if ENABLE_KIWIX:
            messages = await inject_kiwix(messages)
        if ENABLE_MAP_SEARCH:
            messages = await inject_map_search(messages)
    messages = inject_project_context(messages)
    messages = inject_slash_command_protocol(messages)
    messages = inject_loop_break(messages)
    messages = inject_data_starvation_warning(messages)
    messages = inject_stalled_progress_warning(messages)
    messages = truncate_large_tool_results(messages)
    messages = compact_context(messages)
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

    ollama_body = {
        "model": chat_body.get("model", ""),
        "messages": messages,
        "stream": chat_body.get("stream", False),
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
                print(f"[PROXY] Restored cached args for {call_id}: {args[:100]}",
                      file=sys.stderr, flush=True)
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
                print(f"[PROXY] Input[{i}] type={itype}: {json.dumps(item)[:500]}", file=sys.stderr, flush=True)

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

        # Adaptive temperature: if the model is in a tool-call loop,
        # elevate temperature to give it variance to break out. Returns
        # 0.0 for normal operation; chat_to_ollama_request only honors
        # the override when it's > 0.
        elevated_temp = compute_session_temperature(chat_body["messages"])
        if elevated_temp > 0:
            chat_body["temperature"] = elevated_temp
            print(
                f"[FILTER] +temp_escalation (loop detected, temp={elevated_temp})",
                file=sys.stderr,
                flush=True,
            )

        # Convert to Ollama native format (supports options.num_ctx)
        ollama_body = chat_to_ollama_request(chat_body)
        upstream = f"{OLLAMA_URL}/api/chat"

        print(f"[PROXY] Responses API → Ollama (stream={is_streaming} model={original_model} "
              f"msgs={len(chat_body['messages'])} tools={len(chat_body.get('tools', []))} "
              f"ctx={NUM_CTX} temp={ollama_body['options'].get('temperature')} "
              f"top_p={ollama_body['options'].get('top_p')})",
              file=sys.stderr, flush=True)

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
                print(f"[PROXY]   msg[{i}] role={role} content_len={content_len}{extra}", file=sys.stderr, flush=True)
                if tc:
                    for t in tc:
                        print(f"[PROXY]     tc: {json.dumps(t)[:300]}", file=sys.stderr, flush=True)

        if is_streaming:
            ollama_body["stream"] = True
            client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
            try:
                req = client.build_request("POST", upstream, json=ollama_body)
                resp = await client.send(req, stream=True)
                if resp.status_code >= 400:
                    error_body = await resp.aread()
                    await client.aclose()
                    print(f"[PROXY] Ollama error: {resp.status_code} {error_body[:300]}", file=sys.stderr, flush=True)
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
                                    print(
                                        f"[STREAM] done content_chars={content_chars_total} "
                                        f"tool_calls={tool_calls_total} "
                                        f"prompt_tokens={ollama_chunk.get('prompt_eval_count', 0)} "
                                        f"completion_tokens={ollama_chunk.get('eval_count', 0)}",
                                        file=sys.stderr,
                                        flush=True,
                                    )
                                    if content_chars_total == 0 and tool_calls_total == 0:
                                        print(
                                            f"[STREAM] EMPTY OUTPUT — last ollama chunk: "
                                            f"{json.dumps(ollama_chunk)[:500]}",
                                            file=sys.stderr,
                                            flush=True,
                                        )
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
                                    print(f"[PROXY] Tool call: {json.dumps(tc_deltas)[:500]}", file=sys.stderr, flush=True)
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
                    print(f"[PROXY] Ollama error: {resp.status_code} {resp.content[:300]}", file=sys.stderr, flush=True)
                    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
                try:
                    ollama_resp = resp.json()
                    chat_resp = ollama_response_to_chat(ollama_resp, original_model)
                    return Response(
                        content=json.dumps(chat_response_to_responses(chat_resp, original_model)).encode(),
                        status_code=200, media_type="application/json",
                    )
                except Exception as e:
                    print(f"[PROXY] Translation error: {e}", file=sys.stderr, flush=True)
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
            if "tools" in data:
                data["tools"] = filter_tools(data["tools"])

            # Adaptive temperature: see /responses path for rationale.
            elevated_temp = compute_session_temperature(data["messages"])
            if elevated_temp > 0:
                data["temperature"] = elevated_temp
                print(
                    f"[FILTER] +temp_escalation (loop detected, temp={elevated_temp})",
                    file=sys.stderr,
                    flush=True,
                )

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
            print(f"[PROXY] Chat completions error: {e}", file=sys.stderr, flush=True)
            return Response(content=json.dumps({"error": str(e)}).encode(), status_code=500)

    # ── Generic passthrough ───────────────────────────────────────────
    upstream = f"{OLLAMA_URL}/{path}"
    if request.url.query:
        upstream += f"?{request.url.query}"

    print(f"[PROXY] Passthrough {request.method} /{path} -> {upstream}", file=sys.stderr, flush=True)

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
