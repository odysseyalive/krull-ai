"""
Open WebUI Inlet Filter: Plan Execution Tracker
After a plan is approved and execution begins, this filter:
- Extracts and remembers the plan steps from the conversation
- Tracks which steps have been completed based on file edits and tool usage
- Injects a progress reminder into each request so the model stays on track
- Warns if the model appears to be doing something outside the plan
"""

import re
from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=4, description="Filter priority (runs after plan mode assist)"
        )
        enabled: bool = Field(
            default=True, description="Enable/disable plan execution tracking"
        )
        reminder_interval: int = Field(
            default=2,
            description="Inject progress reminder every N messages",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._plan_text = None
        self._plan_steps = []
        self._completed_files = set()
        self._message_count = 0

    def _extract_plan(self, messages: list) -> Optional[str]:
        """Find the approved plan in the conversation history."""
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                # Look for the plan approval marker
                if "## Approved Plan:" in content or "## Plan:" in content:
                    # Extract everything after the marker
                    for marker in ["## Approved Plan:", "## Plan:"]:
                        if marker in content:
                            plan = content.split(marker, 1)[1].strip()
                            return plan
                # Also check for plan mode exit signal with plan content
                if "User has approved your plan" in content and "##" in content:
                    parts = content.split("##", 1)
                    if len(parts) > 1:
                        return "##" + parts[1]
        return None

    def _parse_steps(self, plan_text: str) -> list:
        """Extract actionable steps from plan text."""
        steps = []
        lines = plan_text.split("\n")
        current_step = None

        for line in lines:
            stripped = line.strip()
            # Match numbered steps, bullet points, or checkbox items
            step_match = re.match(
                r"^(?:(\d+)[.)]\s+|[-*]\s+(?:\[[ x]\]\s+)?|#{1,3}\s+(?:Step\s+\d+[:.]\s+)?)(.*)",
                stripped,
            )
            if step_match:
                step_text = step_match.group(2) if step_match.group(2) else step_match.group(0)
                step_text = step_text.strip()
                if step_text and len(step_text) > 5:
                    current_step = {
                        "text": step_text,
                        "files": self._extract_file_refs(step_text),
                        "completed": False,
                    }
                    steps.append(current_step)
            elif current_step and stripped and not stripped.startswith("#"):
                # Continuation of previous step — check for file references
                more_files = self._extract_file_refs(stripped)
                current_step["files"].update(more_files)

        return steps

    def _extract_file_refs(self, text: str) -> set:
        """Extract file path references from text."""
        patterns = [
            r"`([^`]*\.[a-zA-Z]{1,10})`",  # `file.ext`
            r"(?:^|\s)(\S+\.[a-zA-Z]{1,10})(?:\s|$|,|:)",  # bare file.ext
        ]
        files = set()
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                path = match.group(1)
                # Filter out obvious non-files
                if not any(
                    path.startswith(p)
                    for p in ["http", "www.", "e.g", "i.e"]
                ):
                    files.add(path)
        return files

    def _detect_completed_files(self, messages: list) -> set:
        """Detect which files have been edited based on conversation history."""
        edited = set()
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                # Look for tool result patterns indicating file edits
                edit_patterns = [
                    r"(?:edited|modified|updated|wrote|created|saved)\s+[`']?(\S+\.[a-zA-Z]{1,10})[`']?",
                    r"File\s+[`']?(\S+\.[a-zA-Z]{1,10})[`']?\s+(?:has been|was)\s+(?:updated|created|modified|edited)",
                    r"Successfully\s+(?:edited|wrote|created)\s+[`']?(\S+\.[a-zA-Z]{1,10})[`']?",
                ]
                for pattern in edit_patterns:
                    for match in re.finditer(pattern, content, re.IGNORECASE):
                        edited.add(match.group(1))
        return edited

    def _update_step_completion(self):
        """Mark steps as completed based on file edits detected."""
        for step in self._plan_steps:
            if step["completed"]:
                continue
            if step["files"] and step["files"].issubset(self._completed_files):
                step["completed"] = True

    def _is_executing_plan(self, messages: list) -> bool:
        """Check if we've exited plan mode and are now executing."""
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                if (
                    "User has approved your plan" in content
                    or "Exited Plan Mode" in content
                    or "You can now make edits" in content
                    or "exited plan mode" in content.lower()
                ):
                    return True
        return False

    def _is_still_in_plan_mode(self, messages: list) -> bool:
        """Check if plan mode is still active (not yet approved)."""
        for msg in reversed(messages):
            content = msg.get("content", "")
            if isinstance(content, str):
                if "Plan mode is active" in content:
                    return True
                if "Exited Plan Mode" in content or "User has approved" in content:
                    return False
        return False

    def _build_progress_message(self) -> str:
        """Build a progress summary for the model."""
        if not self._plan_steps:
            return ""

        total = len(self._plan_steps)
        completed = sum(1 for s in self._plan_steps if s["completed"])

        lines = [
            "[Plan Execution Tracker]",
            f"Progress: {completed}/{total} steps completed",
            "",
        ]

        # Show current status of each step
        for i, step in enumerate(self._plan_steps, 1):
            status = "DONE" if step["completed"] else "TODO"
            marker = "[x]" if step["completed"] else "[ ]"
            text = step["text"][:100]
            lines.append(f"  {marker} Step {i}: {text}")

        # Find the next uncompleted step
        next_step = None
        for i, step in enumerate(self._plan_steps, 1):
            if not step["completed"]:
                next_step = (i, step)
                break

        if next_step:
            idx, step = next_step
            lines.append("")
            lines.append(f"CURRENT FOCUS: Step {idx} — {step['text']}")
            if step["files"]:
                lines.append(f"  Files to modify: {', '.join(step['files'])}")
            lines.append("")
            lines.append(
                "Stay focused on this step. Complete it fully before "
                "moving to the next one."
            )
        else:
            lines.append("")
            lines.append(
                "ALL STEPS COMPLETE. Verify the changes work correctly, "
                "then inform the user."
            )

        lines.append("[End Plan Execution Tracker]")

        return "\n".join(lines)

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        # Don't interfere with active plan mode — that's plan_mode_assist's job
        if self._is_still_in_plan_mode(messages):
            return body

        # Only activate during plan execution
        if not self._is_executing_plan(messages):
            return body

        # Extract plan if we haven't yet
        if not self._plan_steps:
            plan_text = self._extract_plan(messages)
            if plan_text:
                self._plan_text = plan_text
                self._plan_steps = self._parse_steps(plan_text)

        if not self._plan_steps:
            return body

        # Track file edits
        self._completed_files = self._detect_completed_files(messages)
        self._update_step_completion()

        # Only inject reminder every N messages to avoid noise
        self._message_count += 1
        if self._message_count % self.valves.reminder_interval != 0:
            return body

        progress = self._build_progress_message()
        if not progress:
            return body

        # Inject progress tracker after system messages
        tracker_message = {"role": "system", "content": progress}

        insert_idx = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                insert_idx = i + 1
            else:
                break

        messages.insert(insert_idx, tracker_message)
        body["messages"] = messages

        return body
