"""
Open WebUI Inlet Filter: Plan Mode Assistant
Detects when plan mode instructions are present and reinforces them
for local models that may struggle with complex multi-phase workflows.
Adds structured guidance, phase tracking, and guardrails to help
smaller models follow the plan mode protocol correctly.
"""

from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=3, description="Filter priority (runs after other filters)"
        )
        enabled: bool = Field(
            default=True, description="Enable/disable plan mode assistance"
        )
        reinforce_readonly: bool = Field(
            default=True,
            description="Add extra emphasis on read-only restrictions during planning",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _is_plan_mode(self, messages: list) -> bool:
        """Check if plan mode instructions are present in the conversation."""
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                if "plan mode" in content.lower() and (
                    "MUST NOT make any edits" in content
                    or "Plan mode is active" in content
                    or "ExitPlanMode" in content
                ):
                    return True
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text", "")
                        if "plan mode" in text.lower() and (
                            "MUST NOT make any edits" in text
                            or "Plan mode is active" in text
                            or "ExitPlanMode" in text
                        ):
                            return True
        return False

    def _detect_phase(self, messages: list) -> int:
        """Estimate which phase of plan mode we're in based on conversation."""
        tool_uses = {"glob": 0, "grep": 0, "read": 0, "agent": 0, "write": 0}
        plan_file_written = False
        has_design_discussion = False

        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                content_lower = content.lower()
                for tool in tool_uses:
                    if tool in content_lower:
                        tool_uses[tool] += 1
                if "plan file" in content_lower or ".md" in content_lower:
                    if "write" in content_lower or "edit" in content_lower:
                        plan_file_written = True
                if "design" in content_lower or "architecture" in content_lower:
                    has_design_discussion = True

        total_exploration = tool_uses["glob"] + tool_uses["grep"] + tool_uses["read"]

        if plan_file_written:
            return 5  # Ready to exit
        if has_design_discussion and total_exploration > 3:
            return 4  # Writing plan
        if total_exploration > 3:
            return 3  # Review phase
        if total_exploration > 0:
            return 2  # Design phase
        return 1  # Initial understanding

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        if not self._is_plan_mode(messages):
            return body

        phase = self._detect_phase(messages)

        phase_guidance = {
            1: (
                "You are in Phase 1: INITIAL UNDERSTANDING.\n"
                "Your job right now is to EXPLORE the codebase. Use Glob to find "
                "relevant files, Grep to search for patterns, and Read to understand "
                "the code. Do NOT write anything yet. Do NOT propose solutions yet.\n"
                "Focus on: What files exist? What's the structure? What's relevant "
                "to the task?"
            ),
            2: (
                "You are in Phase 2: DESIGN.\n"
                "You've explored the codebase. Now think about the implementation "
                "approach. Consider:\n"
                "- What files need to change?\n"
                "- What's the safest order of changes?\n"
                "- What could go wrong?\n"
                "- Are there existing patterns to follow?\n"
                "Do NOT edit any code files. You may only write to the plan file."
            ),
            3: (
                "You are in Phase 3: REVIEW.\n"
                "Read the critical files you identified in your design. Make sure "
                "you understand the existing code before finalizing your plan. "
                "Check for edge cases, dependencies, and potential conflicts.\n"
                "Do NOT edit any code files."
            ),
            4: (
                "You are in Phase 4: WRITE THE PLAN.\n"
                "Write your implementation plan to the plan file. Include:\n"
                "- Summary of what needs to change\n"
                "- Ordered list of specific changes per file\n"
                "- Any risks or considerations\n"
                "The plan file is the ONLY file you may write to."
            ),
            5: (
                "You are in Phase 5: FINALIZE.\n"
                "Your plan is written. Call ExitPlanMode to present it to the user "
                "for approval. Do NOT make any code changes."
            ),
        }

        guidance = phase_guidance.get(phase, phase_guidance[1])

        reinforcement = {
            "role": "system",
            "content": (
                "[Plan Mode Assistant — Local Model Guidance]\n\n"
                f"{guidance}\n\n"
                "CRITICAL REMINDERS:\n"
                "- You are in PLAN MODE. Do NOT edit code files.\n"
                "- Do NOT run destructive commands.\n"
                "- Do NOT make commits.\n"
                "- The ONLY file you may write to is the plan file.\n"
                "- Use read-only tools: Glob, Grep, Read.\n"
                "- Think step by step. Be thorough before moving to the next phase.\n"
                "[End Plan Mode Assistant]"
            ),
        }

        # Insert reinforcement right after system messages, before conversation
        insert_idx = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                insert_idx = i + 1
            else:
                break

        messages.insert(insert_idx, reinforcement)
        body["messages"] = messages

        return body
