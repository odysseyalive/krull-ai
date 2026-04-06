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
    return messages


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
    "1. PATHS WITH SPACES MUST BE DOUBLE-QUOTED. Many user paths (e.g. "
    "Google Drive, Insync) contain spaces. Unquoted paths are word-split "
    "by the shell and you get 'No such file or directory' on the first "
    "fragment.\n"
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
        return [
            "",
            "Environmental notes:",
            "- If Bash starts returning 'PreToolUse:Bash hook error' (rather",
            "  than shell errors), the project has a hook with an unquoted",
            "  $CLAUDE_PROJECT_DIR. Prefer Read/Grep/Glob, which don't fire",
            "  the Bash hook chain.",
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
        state["sent_full"] = True
        state["sent_skills"] = set(pc.skill_names)
    else:
        body = pc.static_message()

    # Avoid duplicates if our message is already present (e.g., a re-run
    # within the same request). Match on the unique header tag.
    for m in messages:
        if m.get("role") == "system" and "[Krull Project Context]" in _content_text(m.get("content", "")):
            return messages

    return _insert_after_system_messages(messages, body)


# Detect tool_results that contain known hook failure shapes, so we can
# inject a one-line recovery hint on the next turn. Generic, not project-
# or skill-specific. Mirrors the tool error feedback loop pattern in
# services/tools/toolExecution.ts.
HOOK_ERROR_PATTERNS = [
    "PreToolUse:Bash hook error",
    "PreToolUse:Edit hook error",
    "PreToolUse:Write hook error",
]
HOOK_ERROR_HINT = (
    "[Krull] A previous tool call was rejected by a project PreToolUse hook "
    "(likely an unquoted $CLAUDE_PROJECT_DIR in a hook command). The Bash/"
    "Edit/Write tool may be unreliable in this environment. Prefer Read, "
    "Grep, or Glob for inspection. For dictionary or index lookups, invoke "
    "the relevant Skill — the proxy can run helper scripts directly without "
    "firing project hooks."
)


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    return str(content) if content is not None else ""


def messages_have_hook_error(messages: list) -> bool:
    """Scan tool messages for the known hook error literals."""
    for m in messages:
        if m.get("role") != "tool":
            continue
        text = _content_text(m.get("content", ""))
        if any(pat in text for pat in HOOK_ERROR_PATTERNS):
            return True
    return False


def inject_hook_error_hint(messages: list) -> list:
    """If a hook-error tool result is present, inject the recovery hint
    after the client's main system prompt."""
    if not messages_have_hook_error(messages):
        return messages
    # Avoid duplicate hints
    for m in messages:
        if m.get("role") == "system" and HOOK_ERROR_HINT in _content_text(m.get("content", "")):
            return messages
    print("[CONTEXT] hook-error hint injected", file=sys.stderr, flush=True)
    return _insert_after_system_messages(messages, HOOK_ERROR_HINT)


async def apply_filters(messages: list, has_tools: bool = False) -> list:
    """Run all enabled inlet filters on the messages.

    Each pre-existing filter inserts at position 0, so the LAST one to run
    ends up at position 0 in the final list. The order below is chosen
    deliberately so the final layout is:

      [0] TOOL_GUIDANCE              ← qwen 9B's tool-use primer (must be 0)
      [1] KRULL_SHELL_RULES          ← bash quoting pattern, prominent slot
      [2] DATE
      [3] TRUTH_GUARD
      [4] Claude Code's main system prompt
      [5] KRULL_PROJECT_CONTEXT      ← inserted AFTER Claude Code's prompt
      [6] HOOK_ERROR_HINT             (conditional)
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
    """
    if ENABLE_TRUTH_GUARD:
        messages = inject_truth_guard(messages)
    if ENABLE_DATE:
        messages = inject_date(messages)
    messages = inject_shell_rules(messages)
    if has_tools:
        # Inject tool usage guidance so the model uses correct parameter names.
        # KEEP THIS AT POSITION 0 — see apply_filters docstring.
        messages.insert(0, {"role": "system", "content": TOOL_GUIDANCE})
    else:
        if ENABLE_WEB_SEARCH:
            messages = await inject_web_search(messages)
        if ENABLE_KIWIX:
            messages = await inject_kiwix(messages)
        if ENABLE_MAP_SEARCH:
            messages = await inject_map_search(messages)
    messages = inject_project_context(messages)
    messages = inject_hook_error_hint(messages)
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
        "options": {"num_ctx": NUM_CTX},
    }
    if "tools" in chat_body:
        ollama_body["tools"] = chat_body["tools"]
    if "max_tokens" in chat_body:
        ollama_body["options"]["num_predict"] = chat_body["max_tokens"]
    if "temperature" in chat_body:
        ollama_body["options"]["temperature"] = chat_body["temperature"]
    if "top_p" in chat_body:
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

        # Apply inlet filters to the messages
        has_tools = bool(chat_body.get("tools"))
        chat_body["messages"] = await apply_filters(chat_body["messages"], has_tools=has_tools)

        # Convert to Ollama native format (supports options.num_ctx)
        ollama_body = chat_to_ollama_request(chat_body)
        upstream = f"{OLLAMA_URL}/api/chat"

        print(f"[PROXY] Responses API → Ollama (stream={is_streaming} model={original_model} "
              f"msgs={len(chat_body['messages'])} tools={len(chat_body.get('tools', []))} ctx={NUM_CTX})",
              file=sys.stderr, flush=True)

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
            has_tools = bool(data.get("tools"))
            data["messages"] = await apply_filters(data.get("messages", []), has_tools=has_tools)
            if "tools" in data:
                data["tools"] = filter_tools(data["tools"])

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
