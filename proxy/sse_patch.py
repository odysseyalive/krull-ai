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

import json
import os
import sys
import uuid
import time
import urllib.parse
from datetime import datetime

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


def fix_tool_call_params(tool_name: str, arguments: str) -> str:
    """Fix common parameter name mistakes in tool call arguments."""
    fixes = PARAM_FIXES.get(tool_name)
    if not fixes:
        return arguments
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(args, dict):
            return arguments
        fixed = {}
        changed = False
        for k, v in args.items():
            if k in fixes:
                fixed[fixes[k]] = v
                changed = True
            else:
                fixed[k] = v
        if changed:
            print(f"[PROXY] Fixed tool params for {tool_name}: {list(args.keys())} → {list(fixed.keys())}",
                  file=sys.stderr, flush=True)
            return json.dumps(fixed)
        return arguments if isinstance(arguments, str) else json.dumps(arguments)
    except (json.JSONDecodeError, TypeError):
        return arguments


# ── Inlet Filters ─────────────────────────────────────────────────────────

ENABLE_TRUTH_GUARD = os.environ.get("ENABLE_TRUTH_GUARD", "true").lower() == "true"

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


def inject_truth_guard(messages: list) -> list:
    """Inject truth guard rules as a system message at the start."""
    messages.insert(0, {"role": "system", "content": TRUTH_GUARD_CONTENT})
    return messages


def inject_date(messages: list) -> list:
    """Inject current date/time as a system message at the start."""
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


async def inject_kiwix(messages: list) -> list:
    """Search Kiwix for relevant offline knowledge articles."""
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
        suggest_url = (
            f"{KIWIX_URL}/suggest"
            f"?term={urllib.parse.quote(query)}&limit=3"
        )

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(suggest_url)
            if resp.status_code != 200:
                return messages
            suggestions = resp.json()

        if not suggestions:
            return messages

        lines = ["[Offline Knowledge Base (Kiwix)]"]
        for i, item in enumerate(suggestions, 1):
            title = item.get("label", item.get("value", ""))
            path = item.get("path", item.get("url", ""))
            if title:
                lines.append(f"{i}. {title}")
                if path:
                    lines.append(f"   Source: http://localhost:8090{path}")
        lines.append("[End Offline Knowledge Base]")
        lines.append("")

        if len(lines) > 3:
            messages[-1] = dict(last)
            messages[-1]["content"] = "\n".join(lines) + f"\n{messages[-1]['content']}"

    except Exception as e:
        print(f"[PROXY] Kiwix error: {e}", file=sys.stderr, flush=True)

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


async def apply_filters(messages: list, has_tools: bool = False) -> list:
    """Run all enabled inlet filters on the messages.

    When has_tools is True (Claude Code sending tool definitions), skip web
    search and kiwix — their injected context interferes with tool calling.
    Date is always injected as it's lightweight and doesn't confuse the model.
    Context compaction always runs as the last step.
    """
    if ENABLE_TRUTH_GUARD:
        messages = inject_truth_guard(messages)
    if ENABLE_DATE:
        messages = inject_date(messages)
    if has_tools:
        # Inject tool usage guidance so the model uses correct parameter names
        messages.insert(0, {"role": "system", "content": TOOL_GUIDANCE})
    else:
        if ENABLE_WEB_SEARCH:
            messages = await inject_web_search(messages)
        if ENABLE_KIWIX:
            messages = await inject_kiwix(messages)
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
