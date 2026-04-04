"""
Open WebUI Inlet Filter: Automatic Context Manager
Monitors conversation length and automatically compacts older messages
when approaching the model's context limit. Keeps the conversation alive
by summarizing history rather than truncating it.
"""

from pydantic import BaseModel, Field
from typing import Optional


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
        summary_model: str = Field(
            default="",
            description="Model to use for summarization (empty = same model)",
        )
        enabled: bool = Field(
            default=True, description="Enable/disable context management"
        )

    def __init__(self):
        self.valves = self.Valves()

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimate: ~4 chars per token for English."""
        return len(text) // 4

    def _estimate_messages_tokens(self, messages: list) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self._estimate_tokens(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and "text" in part:
                        total += self._estimate_tokens(part["text"])
            # Overhead for role, formatting
            total += 4
        return total

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        total_tokens = self._estimate_messages_tokens(messages)
        threshold = int(
            self.valves.max_context_tokens * self.valves.compact_threshold
        )

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

        # Keep the most recent messages intact
        preserve_count = min(
            self.valves.preserve_recent * 2, len(conversation)
        )
        if preserve_count >= len(conversation):
            return body

        old_messages = conversation[:-preserve_count]
        recent_messages = conversation[-preserve_count:]

        # Build a summary of old messages
        summary_parts = []
        for msg in old_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and "text" in part:
                        text_parts.append(part["text"])
                content = " ".join(text_parts)
            if content:
                # Truncate very long individual messages in the summary
                if len(content) > 300:
                    content = content[:300] + "..."
                summary_parts.append(f"[{role}]: {content}")

        if not summary_parts:
            return body

        summary_text = "\n".join(summary_parts)

        # Create a compacted summary message
        compact_message = {
            "role": "system",
            "content": (
                "[Context Manager: The conversation history has been compacted "
                "to stay within the context window. Below is a summary of the "
                "earlier conversation.]\n\n"
                f"=== Earlier Conversation Summary ===\n{summary_text}\n"
                "=== End Summary ===\n\n"
                "Continue the conversation naturally based on this context."
            ),
        }

        # Rebuild: system messages + compact summary + recent messages
        body["messages"] = system_messages + [compact_message] + recent_messages

        return body
