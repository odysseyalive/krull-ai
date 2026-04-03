"""
Open WebUI Inlet Filter: General-Purpose Skill Adapter for Local Models

Automatically adapts ANY Claude Code skill for smaller models and shorter
context windows. Rather than hardcoding per-skill logic, this filter:

1. Detects when a skill is invoked (any skill, not just known ones)
2. Parses the skill content from the conversation to understand its structure
3. Extracts and prioritizes: directives > current command > workflow steps
4. Condenses reference material and grounding sections to save context
5. Simplifies multi-agent/hook patterns into sequential steps
6. Tracks which command within a skill is being executed
7. Reinforces constraints the model tends to forget over long conversations
"""

import re
from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=5, description="Filter priority (runs after plan filters)"
        )
        enabled: bool = Field(
            default=True, description="Enable/disable skill adaptation"
        )
        max_skill_tokens: int = Field(
            default=2000,
            description="Max estimated tokens for skill content in context. "
            "Skill instructions exceeding this are condensed.",
        )
        condense_references: bool = Field(
            default=True,
            description="Condense reference/grounding sections to file pointers",
        )
        simplify_agents: bool = Field(
            default=True,
            description="Convert multi-agent patterns to sequential steps",
        )
        simplify_hooks: bool = Field(
            default=True,
            description="Simplify hook descriptions into plain instructions",
        )
        reinforce_interval: int = Field(
            default=4,
            description="Re-inject skill focus reminder every N messages",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._active_skill = None
        self._active_command = None
        self._skill_directives = []
        self._skill_steps = []
        self._message_count = 0

    # --- Detection ---

    def _detect_skill_invocation(self, messages: list) -> Optional[dict]:
        """Detect skill invocation and extract skill name + arguments."""
        recent = messages[-6:] if len(messages) > 6 else messages
        for msg in reversed(recent):
            content = msg.get("content", "")
            if isinstance(content, str):
                # <command-name> tag (how Claude Code injects skills)
                match = re.search(
                    r"<command-name>([\w][\w-]*)</command-name>", content
                )
                if match:
                    # Try to extract arguments too
                    args_match = re.search(
                        r"<command-args>(.*?)</command-args>",
                        content,
                        re.DOTALL,
                    )
                    return {
                        "name": match.group(1),
                        "args": args_match.group(1).strip()
                        if args_match
                        else "",
                        "content": content,
                    }

                # Skill tool invocation
                match = re.search(
                    r'[Ss]kill.*?["\'](\w[\w-]*)["\']', content
                )
                if match:
                    return {
                        "name": match.group(1),
                        "args": "",
                        "content": content,
                    }
        return None

    # --- Parsing ---

    def _extract_directives(self, content: str) -> list:
        """Extract user directives (sacred, never modify)."""
        directives = []

        # Match immutable origin blocks
        immutable_blocks = re.findall(
            r"<!-- origin: user.*?immutable: true.*?-->(.*?)<!-- /origin -->",
            content,
            re.DOTALL,
        )
        for block in immutable_blocks:
            # Extract quoted directives
            quotes = re.findall(r'>\s*\*\*"(.*?)"\*\*', block, re.DOTALL)
            directives.extend(quotes)

        # Match ## Directives section
        dir_match = re.search(
            r"## Directives\s*\n(.*?)(?=\n## |\n---|\Z)", content, re.DOTALL
        )
        if dir_match:
            section = dir_match.group(1)
            # Extract blockquotes
            quotes = re.findall(r">\s*\*\*\"?(.*?)\"?\*\*", section, re.DOTALL)
            for q in quotes:
                if q not in directives:
                    directives.append(q)

        return directives

    def _extract_commands_table(self, content: str) -> list:
        """Extract available commands from markdown tables."""
        commands = []
        # Match table rows with command patterns
        rows = re.findall(
            r"\|\s*`/?([^`|]+)`\s*\|([^|]*)\|", content
        )
        for cmd, desc in rows:
            cmd = cmd.strip()
            desc = desc.strip()
            if cmd and not cmd.startswith("-") and "Command" not in cmd:
                commands.append({"command": cmd, "description": desc})
        return commands

    def _extract_workflow_steps(self, content: str, command: str = "") -> list:
        """Extract numbered workflow steps for a specific command."""
        steps = []

        # Find the section most relevant to the command
        if command:
            # Look for a section header matching the command
            pattern = rf"(?:^|\n)##+ .*?{re.escape(command)}.*?\n(.*?)(?=\n## |\Z)"
            match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
            if match:
                section = match.group(1)
            else:
                section = content
        else:
            section = content

        # Extract numbered steps
        for match in re.finditer(
            r"^\s*(\d+)\.\s+\*?\*?(.*?)\*?\*?\s*$", section, re.MULTILINE
        ):
            step_text = match.group(2).strip()
            if step_text and len(step_text) > 3:
                steps.append(step_text)

        return steps

    def _estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    # --- Condensing ---

    def _condense_references(self, content: str) -> str:
        """Replace verbose reference/grounding sections with compact pointers."""
        # Condense ## Grounding sections
        content = re.sub(
            r"## Grounding\s*\n.*?(?=\n## |\Z)",
            "## Grounding\n"
            "Reference files are in the skill's references/ directory. "
            "Read them as needed — do not try to memorize their contents.\n",
            content,
            flags=re.DOTALL,
        )

        # Condense long reference file lists
        content = re.sub(
            r"Reference files:\s*\n((?:\s*-\s*\[.*?\].*?\n){5,})",
            "Reference files: See the skill's references/ directory.\n",
            content,
        )

        # Condense origin-tagged modifiable blocks that are purely structural
        # Keep user/immutable blocks intact
        def condense_origin_block(match):
            tag = match.group(1)
            inner = match.group(2)
            if "immutable: true" in tag:
                return match.group(0)  # Keep sacred content
            # Keep the content but strip the origin tags to save tokens
            return inner.strip()

        content = re.sub(
            r"<!-- origin: (.*?) -->(.*?)<!-- /origin -->",
            condense_origin_block,
            content,
            flags=re.DOTALL,
        )

        return content

    def _simplify_agent_patterns(self, content: str) -> str:
        """Convert multi-agent instructions to sequential steps."""
        # Replace "spawn N agents" / "launch agents" patterns
        content = re.sub(
            r"[Ss]pawn\s+(\d+)\s+(?:parallel\s+)?agents?.*?(?:\n|$)",
            r"Work through \1 aspects sequentially:\n",
            content,
        )
        content = re.sub(
            r"[Ll]aunch\s+(?:a\s+)?(?:Plan\s+)?agents?\s+.*?(?:\n|$)",
            "Analyze this aspect step by step:\n",
            content,
        )
        # Replace "Agent tool" references
        content = re.sub(
            r"(?:use|call|invoke)\s+the\s+Agent\s+tool",
            "think through this carefully",
            content,
            flags=re.IGNORECASE,
        )
        return content

    def _simplify_hooks(self, content: str) -> str:
        """Convert hook specifications to plain-language reminders."""
        simplified = content

        # Extract hook blocks from YAML frontmatter and convert to reminders
        hook_section = re.search(
            r"^hooks:\s*\n((?:\s+.*\n)*?)(?=\S|\Z)",
            content,
            re.MULTILINE,
        )
        if hook_section:
            hook_text = hook_section.group(1)
            reminders = []

            # Extract PreToolUse hooks
            pre_hooks = re.findall(
                r'prompt:\s*"(.*?)"', hook_text, re.DOTALL
            )
            for hook in pre_hooks:
                # Condense to the core check
                if "persona" in hook.lower() or "uniqu" in hook.lower():
                    reminders.append(
                        "Before creating agents: check that the persona "
                        "is unique across all existing agents."
                    )
                elif "directive" in hook.lower() or "verbatim" in hook.lower():
                    reminders.append(
                        "After editing skill files: verify that directives "
                        "were not reworded or removed."
                    )
                elif "deny" in hook.lower() or "reject" in hook.lower():
                    reminders.append(
                        "Validate changes before applying them."
                    )

            if reminders:
                reminder_text = "\n## Validation Reminders\n" + "\n".join(
                    f"- {r}" for r in reminders
                )
                # Insert reminders after directives
                dir_end = simplified.find("## Commands")
                if dir_end == -1:
                    dir_end = simplified.find("## Quick")
                if dir_end == -1:
                    simplified += reminder_text
                else:
                    simplified = (
                        simplified[:dir_end]
                        + reminder_text
                        + "\n\n"
                        + simplified[dir_end:]
                    )

        return simplified

    def _detect_active_command(self, messages: list, commands: list) -> Optional[str]:
        """Figure out which specific command within a skill is being run."""
        recent = messages[-5:] if len(messages) > 5 else messages
        for msg in reversed(recent):
            content = msg.get("content", "")
            if isinstance(content, str):
                # Check for command-args
                args_match = re.search(
                    r"<command-args>(.*?)</command-args>", content, re.DOTALL
                )
                if args_match:
                    args = args_match.group(1).strip()
                    # First word of args is usually the sub-command
                    first_word = args.split()[0] if args else ""
                    for cmd in commands:
                        cmd_name = cmd["command"].split()[0]
                        if cmd_name == first_word:
                            return cmd_name

                # Check for command mentions in recent conversation
                content_lower = content.lower()
                for cmd in commands:
                    cmd_name = cmd["command"].split()[0].lower()
                    if f"/{cmd_name}" in content_lower or f"`{cmd_name}`" in content_lower:
                        return cmd_name
        return None

    # --- Main adaptation ---

    def _build_adaptation(
        self,
        skill_name: str,
        skill_content: str,
        active_command: Optional[str],
        is_continuation: bool,
    ) -> str:
        """Build the full adaptation message for a skill invocation."""
        parts = []

        parts.append(f"[Skill Adapter: /{skill_name}]")

        # 1. Always include directives (sacred, compact)
        directives = self._extract_directives(skill_content)
        if directives:
            parts.append("\nDIRECTIVES (these rules are absolute):")
            for i, d in enumerate(directives, 1):
                # Truncate very long directives but keep the core
                if len(d) > 200:
                    d = d[:200] + "..."
                parts.append(f"  {i}. {d}")

        # 2. Show available commands (compact)
        commands = self._extract_commands_table(skill_content)
        if commands and not active_command:
            parts.append("\nAvailable commands:")
            for cmd in commands[:10]:  # Cap at 10 to save context
                parts.append(f"  /{skill_name} {cmd['command']} — {cmd['description'][:60]}")

        # 3. If we know which command is active, extract its steps
        if active_command:
            steps = self._extract_workflow_steps(skill_content, active_command)
            if steps:
                parts.append(f"\nSTEPS for '{active_command}' (follow in order):")
                for i, step in enumerate(steps, 1):
                    if len(step) > 150:
                        step = step[:150] + "..."
                    parts.append(f"  {i}. {step}")

        # 4. Execution guidance for local models
        parts.append("\nEXECUTION GUIDANCE:")
        parts.append("- Work through ONE step at a time. Finish each before starting the next.")
        parts.append("- Call ONE tool at a time. Wait for the result before deciding next action.")
        parts.append("- If a step says to read a file, READ it — do not guess its contents.")
        parts.append("- If you are unsure what to do next, re-read the skill instructions above.")

        if is_continuation:
            parts.append("- You are continuing a skill that was already started.")
            parts.append("  Check what has been done so far before repeating work.")

        # 5. Constraint reinforcement
        constraints = []
        content_lower = skill_content.lower()
        if "display mode" in content_lower or "display/execute" in content_lower:
            constraints.append(
                "This skill has display/execute modes. Default is DISPLAY "
                "(show what would change). Only apply changes if --execute was specified."
            )
        if "self-exclusion" in content_lower:
            constraints.append(
                "This skill excludes itself from its own actions unless 'dev' prefix is used."
            )
        if "never rewrite" in content_lower or "never reword" in content_lower:
            constraints.append(
                "NEVER rewrite, paraphrase, or summarize user directives. Move content, don't rewrite it."
            )
        if "task" in content_lower and "taskcreate" in content_lower:
            constraints.append(
                "Use TaskCreate to build a numbered task list before executing. "
                "Mark each task done with TaskUpdate as you complete it."
            )

        if constraints:
            parts.append("\nCONSTRAINTS:")
            for c in constraints:
                parts.append(f"  - {c}")

        parts.append("\n[End Skill Adapter]")
        return "\n".join(parts)

    # --- Inlet ---

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        # Detect skill invocation
        invocation = self._detect_skill_invocation(messages)

        if invocation:
            skill_name = invocation["name"]
            skill_content = invocation["content"]

            # Apply condensing transformations to the skill content we analyze
            if self.valves.condense_references:
                skill_content = self._condense_references(skill_content)
            if self.valves.simplify_agents:
                skill_content = self._simplify_agent_patterns(skill_content)
            if self.valves.simplify_hooks:
                skill_content = self._simplify_hooks(skill_content)

            # Extract commands and detect which one is active
            commands = self._extract_commands_table(skill_content)
            active_command = self._detect_active_command(messages, commands)

            is_continuation = (
                self._active_skill == skill_name and self._message_count > 0
            )

            # Build adaptation
            adaptation = self._build_adaptation(
                skill_name, skill_content, active_command, is_continuation
            )

            # Check if adaptation fits in token budget
            adaptation_tokens = self._estimate_tokens(adaptation)
            if adaptation_tokens > self.valves.max_skill_tokens:
                # Trim by removing command list and shortening steps
                adaptation = self._build_adaptation(
                    skill_name,
                    skill_content,
                    active_command or (commands[0]["command"].split()[0] if commands else None),
                    is_continuation,
                )

            self._active_skill = skill_name
            self._active_command = active_command
            self._skill_directives = self._extract_directives(skill_content)
            self._skill_steps = self._extract_workflow_steps(
                skill_content, active_command or ""
            )
            self._message_count = 0

            # Inject adaptation
            adaptation_msg = {"role": "system", "content": adaptation}
            insert_idx = 0
            for i, msg in enumerate(messages):
                if msg.get("role") == "system":
                    insert_idx = i + 1
                else:
                    break
            messages.insert(insert_idx, adaptation_msg)
            body["messages"] = messages
            return body

        # If a skill is active and we're in a continuation, reinforce periodically
        if self._active_skill:
            self._message_count += 1

            if (
                self.valves.reinforce_interval > 0
                and self._message_count % self.valves.reinforce_interval == 0
            ):
                reminder_parts = [
                    f"[Skill Reminder: /{self._active_skill}]",
                ]

                if self._skill_directives:
                    reminder_parts.append("Active directives:")
                    for d in self._skill_directives[:3]:
                        short = d[:120] + "..." if len(d) > 120 else d
                        reminder_parts.append(f"  - {short}")

                if self._active_command:
                    reminder_parts.append(
                        f"\nYou are executing: {self._active_command}"
                    )
                    reminder_parts.append(
                        "Continue working through the steps sequentially."
                    )

                reminder_parts.append(
                    "Call one tool at a time. Wait for results."
                )
                reminder_parts.append("[End Skill Reminder]")

                reminder_msg = {
                    "role": "system",
                    "content": "\n".join(reminder_parts),
                }
                insert_idx = 0
                for i, msg in enumerate(messages):
                    if msg.get("role") == "system":
                        insert_idx = i + 1
                    else:
                        break
                messages.insert(insert_idx, reminder_msg)
                body["messages"] = messages

        return body
