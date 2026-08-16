"""
Open WebUI Inlet Filter: Automatic Context Manager (synchronous)

When the conversation approaches the context window, this filter drops the
oldest messages in-request and leaves a short inline note in their place,
keeping the most recent turns verbatim. It is fully synchronous: no job
queue, no background worker, no summary store.

History: an earlier version enqueued a fingerprinted job to /app/context_jobs
for a background summarizer to abstractively summarize the dropped messages
and inject the result on a later request. That async path was never wired
end-to-end — the worker (proxy/summary_worker.py) was never launched, and
the Open WebUI filter and the worker ran in different containers with no
shared volume, so a job written by this filter could never be read by the
worker. It was retired (see ledger DEC-2026-08-15-retire-async-summary).
Claude Code / API traffic bypasses Open WebUI and is compacted natively by
the proxy's own synchronous compact_context; this filter now mirrors that
synchronous approach for the browser-UI path.
"""
from pydantic import BaseModel, Field
from typing import Optional
import logging

# Import tokenizer util (tiktoken fallback)
from .token_utils import count_tokens

logging.basicConfig(filename="/app/logs/proxy_compact.log", level=logging.INFO)


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=0, description="Filter priority (runs first)"
        )
        max_context_tokens: int = Field(
            default=131072,
            description="Maximum context window size in tokens",
        )
        compact_threshold: float = Field(
            default=0.75,
            description="Trigger compaction at this fraction of max context (0.0-1.0)",
        )
        preserve_recent: int = Field(
            default=6,
            description="Number of recent message pairs to always keep intact",
        )
        enabled: bool = Field(
            default=True, description="Enable/disable context management"
        )

    def __init__(self):
        self.valves = self.Valves()

    def _estimate_messages_tokens(self, messages: list) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += count_tokens(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and "text" in part:
                        total += count_tokens(part["text"])
            # Overhead for role, formatting (conservative constant)
            total += 4
        return total

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        total_tokens = self._estimate_messages_tokens(messages)
        threshold = int(self.valves.max_context_tokens * self.valves.compact_threshold)

        # Log for observability
        logging.info("context_manager: total_tokens=%d threshold=%d", total_tokens, threshold)

        if total_tokens <= threshold:
            return body

        # Separate system messages from conversation
        system_messages = []
        conversation = []
        for msg in messages:
            if msg.get("role") == "system":
                system_messages.append(msg)
            else:
                conversation.append(msg)

        # Keep the most recent messages intact (preserve_recent pairs)
        preserve_count = min(self.valves.preserve_recent * 2, len(conversation))
        if preserve_count >= len(conversation):
            # Nothing to compact
            return body

        old_messages = conversation[:-preserve_count]
        recent_messages = conversation[-preserve_count:]
        if not old_messages:
            return body

        # Synchronous drop: replace the older messages with a short inline
        # note so the model knows earlier turns were trimmed to fit the
        # window. No LLM call, no job queue — the note is written in-request.
        note = {
            "role": "system",
            "content": (
                f"[Context Manager: {len(old_messages)} earlier message(s) were "
                "trimmed to keep the conversation within the context window. "
                "The most recent turns are preserved below.]"
            ),
        }

        body["messages"] = system_messages + [note] + recent_messages

        logging.info(
            "context_manager: compaction triggered dropped=%d orig_tokens=%d threshold=%d",
            len(old_messages), total_tokens, threshold,
        )

        return body
