"""
Open WebUI Inlet Filter: Automatic Context Manager (async-safe)

Changes made:
- Uses token_utils.count_tokens for a more accurate token estimate.
- When threshold exceeded, writes a compact-job file (fingerprinted) to
  /app/context_jobs (container path). Inserts a placeholder that encodes
  the fingerprint so a background worker can write a ready summary to
  /app/context_store/{fingerprint}.summary.json and this filter will inject
  it on subsequent requests.
- The filter is idempotent: once a summary is injected the job file and
  summary file are removed.
"""
from pydantic import BaseModel, Field
from typing import Optional
import hashlib
import json
import os
import logging
import time

# Import tokenizer util (tiktoken fallback)
from .token_utils import count_tokens

# Paths inside the sse-proxy container. Ensure docker-compose mounts host
# ./data/context_jobs -> /app/context_jobs and ./data/context_store -> /app/context_store
JOB_DIR = os.environ.get("KRULL_CONTEXT_JOB_DIR", "/app/context_jobs")
STORE_DIR = os.environ.get("KRULL_CONTEXT_STORE_DIR", "/app/context_store")
os.makedirs(JOB_DIR, exist_ok=True)
os.makedirs(STORE_DIR, exist_ok=True)

# Compact placeholder prefix so we can detect and replace it later.
PLACEHOLDER_PREFIX = "[Context Manager: compact pending fingerprint="

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
        summary_model: str = Field(
            default="",
            description="Model to use for summarization (empty = same model)",
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

    def _fingerprint_messages(self, messages: list) -> str:
        # Create a stable fingerprint from the concatenated (role + content) of messages.
        h = hashlib.sha256()
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, list):
                # flatten to text pieces if necessary
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            h.update((role + "\n").encode("utf-8", "surrogatepass"))
            if isinstance(content, str):
                h.update(content.encode("utf-8", "surrogatepass"))
        return h.hexdigest()[:16]  # short but collision-resistant for this use

    def _write_job(self, fingerprint: str, old_messages: list) -> str:
        job_path = os.path.join(JOB_DIR, f"{fingerprint}.job.json")
        payload = {"fingerprint": fingerprint, "ts": int(time.time()), "messages": old_messages}
        try:
            with open(job_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            logging.info("context_manager: enqueued compaction job %s", job_path)
        except Exception as e:
            logging.exception("context_manager: failed to write job %s: %s", job_path, e)
        return job_path

    def _try_inject_ready_summary(self, body: dict) -> bool:
        """
        Scan body messages for any compact placeholder(s). If we find a fingerprint
        and the corresponding summary file exists in STORE_DIR, replace the placeholder
        with the summary system message and return True if any injection happened.
        """
        messages = body.get("messages", [])
        changed = False
        for i, msg in enumerate(list(messages)):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and content.startswith(PLACEHOLDER_PREFIX):
                # extract fingerprint
                start = content.find("fingerprint=")
                if start == -1:
                    continue
                # expect form fingerprint=<hex>]
                rest = content[start + len("fingerprint="):]
                fingerprint = rest.split("]")[0].strip()
                summary_path = os.path.join(STORE_DIR, f"{fingerprint}.summary.json")
                if os.path.exists(summary_path):
                    try:
                        with open(summary_path, "r", encoding="utf-8") as fh:
                            summary_obj = json.load(fh)
                        summary_text = summary_obj.get("summary", "")
                        compact_message = {
                            "role": "system",
                            "content": (
                                "[Context Manager: The conversation history was compacted "
                                "to stay within the context window. Below is an abstractive "
                                "summary of the earlier conversation.]\n\n"
                                f"=== Earlier Conversation Summary ===\n{summary_text}\n"
                                "=== End Summary ===\n\n"
                                "Continue the conversation naturally based on this context."
                            ),
                        }
                        # Replace the placeholder with the compacted summary
                        messages[i] = compact_message
                        changed = True
                        logging.info("context_manager: injected ready summary for fingerprint=%s", fingerprint)
                        # Clean up the summary file (idempotent)
                        try:
                            os.remove(summary_path)
                        except Exception:
                            pass
                    except Exception as e:
                        logging.exception("context_manager: failed to read/replace summary %s: %s", summary_path, e)
        if changed:
            body["messages"] = messages
        return changed

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        # First: if any ready summaries exist for earlier placeholders, inject them.
        # This makes compaction asynchronous from the user's perspective.
        try:
            injected = self._try_inject_ready_summary(body)
            if injected:
                # Recompute messages to ensure downstream filters see the summarized content
                messages = body.get("messages", [])
        except Exception:
            logging.exception("context_manager: error while attempting to inject ready summaries")

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

        # Build a short, safe summary payload for job input (we avoid heavy inline work)
        # Fingerprint old_messages for idempotent job naming
        fingerprint = self._fingerprint_messages(old_messages)

        # Write the job out for background worker
        self._write_job(fingerprint, old_messages)

        # Placeholder includes fingerprint so the worker / future inlet can match it
        placeholder = {
            "role": "system",
            "content": f"{PLACEHOLDER_PREFIX}{fingerprint}] The summary will be injected asynchronously. Continue the conversation."
        }

        # Rebuild: system messages + placeholder + recent messages
        body["messages"] = system_messages + [placeholder] + recent_messages

        logging.info("context_manager: compaction triggered fingerprint=%s orig_tokens=%d threshold=%d",
                     fingerprint, total_tokens, threshold)

        return body
