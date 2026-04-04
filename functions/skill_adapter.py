"""
Open WebUI Inlet Filter: Claude Code Skill Adapter

Replicates Claude Code's skill loading pipeline so local models can
execute skills the same way Claude Code does. Handles two paths:

1. Claude Code path: Parses "Primary working directory:" from system
   messages to discover the project directory automatically.
2. Browser path: User sends "/project <path>" to set the working directory.
   Sends "/project" with no args to list discovered projects.

Once a project is set, the filter:
- Loads CLAUDE.md for project context
- Discovers all skills in .claude/skills/
- Expands /skill-name invocations with full SKILL.md content
- Injects using the same XML tags Claude Code uses
"""

import os
import re
import glob
import yaml
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=0, description="Filter priority (runs BEFORE other filters to intercept /commands)"
        )
        enabled: bool = Field(
            default=True, description="Enable/disable skill adapter"
        )
        user_home: str = Field(
            default="",
            description="User home directory (must be mounted into container). "
            "Auto-detected from /home/ if empty.",
        )
        project_scan_dirs: str = Field(
            default="",
            description="Comma-separated directories to scan for projects "
            "(dirs containing .claude/ or CLAUDE.md). "
            "Auto-scans user home subdirectories if empty.",
        )
        max_skill_tokens: int = Field(
            default=4000,
            description="Max estimated tokens for skill content injection",
        )

    def __init__(self):
        self.valves = self.Valves()
        # Per-chat state (keyed by chat_id)
        self._chat_projects = {}  # chat_id -> project_dir
        self._chat_claude_md = {}  # chat_id -> claude_md_content
        self._skill_registry = {}  # skill_name -> {path, frontmatter, project_dir}
        self._projects_cache = None  # list of discovered project dirs
        self._registry_built = False

    # --- Auto-detection ---

    def _get_user_home(self) -> str:
        """Get user home directory from valve or auto-detect."""
        if self.valves.user_home:
            return self.valves.user_home

        # Auto-detect: look for a home dir that has .claude/
        home_base = "/home"
        if os.path.isdir(home_base):
            try:
                for entry in os.scandir(home_base):
                    if entry.is_dir() and os.path.isdir(
                        os.path.join(entry.path, ".claude")
                    ):
                        return entry.path
            except PermissionError:
                pass

        # Fallback to environment
        return os.environ.get("HOME", "/root")

    def _get_scan_dirs(self) -> list:
        """Get directories to scan from valve or auto-detect."""
        if self.valves.project_scan_dirs:
            return [
                d.strip()
                for d in self.valves.project_scan_dirs.split(",")
                if d.strip()
            ]

        # Auto-detect: scan common locations under user home
        home = self._get_user_home()
        candidates = []
        if os.path.isdir(home):
            try:
                for entry in os.scandir(home):
                    if entry.is_dir(follow_symlinks=True):
                        # Include directories that contain subdirectories
                        # with .claude/ (i.e., parent dirs of projects)
                        candidates.append(entry.path)
            except PermissionError:
                pass

        return candidates if candidates else [home]

    # --- Project Discovery ---

    def _discover_projects(self) -> list:
        """Find all directories that contain .claude/ or CLAUDE.md."""
        if self._projects_cache is not None:
            return self._projects_cache

        projects = []
        scan_dirs = self._get_scan_dirs()

        for scan_dir in scan_dirs:
            if not os.path.isdir(scan_dir):
                continue
            # Walk one level deep looking for .claude/ or CLAUDE.md
            try:
                for entry in os.scandir(scan_dir):
                    if entry.is_dir(follow_symlinks=True):
                        project_path = entry.path
                        has_claude_dir = os.path.isdir(
                            os.path.join(project_path, ".claude")
                        )
                        has_claude_md = os.path.isfile(
                            os.path.join(project_path, "CLAUDE.md")
                        )
                        if has_claude_dir or has_claude_md:
                            projects.append(project_path)
                        # Also check one level deeper for nested projects
                        if entry.is_dir(follow_symlinks=True):
                            try:
                                for sub in os.scandir(project_path):
                                    if sub.is_dir(follow_symlinks=True):
                                        sub_path = sub.path
                                        if os.path.isdir(
                                            os.path.join(sub_path, ".claude")
                                        ) or os.path.isfile(
                                            os.path.join(sub_path, "CLAUDE.md")
                                        ):
                                            if sub_path not in projects:
                                                projects.append(sub_path)
                            except PermissionError:
                                pass
            except PermissionError:
                pass

        self._projects_cache = sorted(projects)
        return self._projects_cache

    # --- Skill Registry ---

    def _build_skill_registry(self):
        """Scan all known locations for skills and build a name -> path map."""
        if self._registry_built:
            return

        registry = {}

        # User-level skills
        user_skills_dir = os.path.join(self._get_user_home(), ".claude", "skills")
        self._scan_skills_dir(user_skills_dir, None, registry)

        # Project-level skills from all discovered projects
        for project_dir in self._discover_projects():
            skills_dir = os.path.join(project_dir, ".claude", "skills")
            self._scan_skills_dir(skills_dir, project_dir, registry)

        self._skill_registry = registry
        self._registry_built = True

    def _scan_skills_dir(self, skills_dir: str, project_dir: Optional[str], registry: dict):
        """Scan a .claude/skills/ directory for SKILL.md files."""
        if not os.path.isdir(skills_dir):
            return

        try:
            for entry in os.scandir(skills_dir):
                if entry.is_dir(follow_symlinks=True):
                    skill_md = os.path.join(entry.path, "SKILL.md")
                    if os.path.isfile(skill_md):
                        fm, content = self._parse_skill_file(skill_md)
                        if fm:
                            name = fm.get("name", entry.name)
                            user_invocable = fm.get("user-invocable", False)
                            # Store in registry (later entries override earlier)
                            registry[name] = {
                                "path": skill_md,
                                "skill_dir": entry.path,
                                "project_dir": project_dir,
                                "frontmatter": fm,
                                "content": content,
                                "user_invocable": user_invocable,
                                "dir_name": entry.name,
                            }
                            # Also register by directory name if different from name
                            if entry.name != name:
                                registry[entry.name] = registry[name]
        except PermissionError:
            pass

    def _parse_skill_file(self, path: str) -> tuple:
        """Read a SKILL.md and parse its YAML frontmatter."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            return None, ""

        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if match:
            try:
                fm = yaml.safe_load(match.group(1))
                content = match.group(2)
                return fm if isinstance(fm, dict) else {}, content
            except yaml.YAMLError:
                return {}, text
        return {}, text

    # --- CLAUDE.md Loading ---

    def _load_claude_md(self, project_dir: str) -> str:
        """Load CLAUDE.md for a project, mimicking Claude Code's hierarchy."""
        parts = []

        # User-level CLAUDE.md
        user_claude = os.path.join(self._get_user_home(), ".claude", "CLAUDE.md")
        if os.path.isfile(user_claude):
            try:
                with open(user_claude, "r", encoding="utf-8") as f:
                    parts.append(f"Contents of {user_claude}:\n\n{f.read()}")
            except Exception:
                pass

        # Project CLAUDE.md
        project_claude = os.path.join(project_dir, "CLAUDE.md")
        if os.path.isfile(project_claude):
            try:
                with open(project_claude, "r", encoding="utf-8") as f:
                    parts.append(
                        f"Contents of {project_claude} (project instructions):\n\n{f.read()}"
                    )
            except Exception:
                pass

        # Project .claude/CLAUDE.md
        dot_claude_md = os.path.join(project_dir, ".claude", "CLAUDE.md")
        if os.path.isfile(dot_claude_md):
            try:
                with open(dot_claude_md, "r", encoding="utf-8") as f:
                    parts.append(f"Contents of {dot_claude_md}:\n\n{f.read()}")
            except Exception:
                pass

        # .claude/rules/*.md
        rules_dir = os.path.join(project_dir, ".claude", "rules")
        if os.path.isdir(rules_dir):
            try:
                for rule_file in sorted(glob.glob(os.path.join(rules_dir, "*.md"))):
                    with open(rule_file, "r", encoding="utf-8") as f:
                        parts.append(f"Contents of {rule_file}:\n\n{f.read()}")
            except Exception:
                pass

        if not parts:
            return ""

        return (
            "Codebase and user instructions are shown below. "
            "Be sure to adhere to these instructions. "
            "IMPORTANT: These instructions OVERRIDE any default behavior "
            "and you MUST follow them exactly as written.\n\n"
            + "\n\n---\n\n".join(parts)
        )

    # --- CWD Detection ---

    def _detect_cwd_from_messages(self, messages: list) -> Optional[str]:
        """Parse 'Primary working directory:' from system messages."""
        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content", "")
                if isinstance(content, str):
                    match = re.search(
                        r"Primary working directory:\s*(.+?)(?:\n|$)", content
                    )
                    if match:
                        cwd = match.group(1).strip()
                        if os.path.isdir(cwd):
                            return cwd
        return None

    # --- /project Command ---

    def _handle_project_command(self, args: str) -> Optional[str]:
        """Handle /project command. Returns response text or None."""
        args = args.strip()

        if not args:
            # List available projects
            projects = self._discover_projects()
            if not projects:
                return (
                    "No projects found. Check that project directories are "
                    "mounted and contain .claude/ or CLAUDE.md."
                )
            lines = ["**Available projects:**\n"]
            for p in projects:
                name = os.path.basename(p)
                has_skills = os.path.isdir(os.path.join(p, ".claude", "skills"))
                skill_count = 0
                if has_skills:
                    try:
                        skill_count = sum(
                            1
                            for d in os.scandir(
                                os.path.join(p, ".claude", "skills")
                            )
                            if d.is_dir()
                        )
                    except Exception:
                        pass
                skills_note = f" ({skill_count} skills)" if skill_count else ""
                lines.append(f"- `{name}` → `{p}`{skills_note}")
            lines.append(
                "\nUsage: `/project <name>` or `/project <full-path>`"
            )
            return "\n".join(lines)

        # Try to resolve the argument to a project path
        # First: exact path
        if os.path.isdir(args):
            return args  # Return path, not display text

        # Second: match by basename against discovered projects
        projects = self._discover_projects()
        for p in projects:
            if os.path.basename(p).lower() == args.lower():
                return p
            # Also try partial match
            if args.lower() in os.path.basename(p).lower():
                return p

        # Third: try as relative to home
        home_path = os.path.join(self._get_user_home(), args)
        if os.path.isdir(home_path):
            return home_path

        return None

    # --- Skill Invocation Detection ---

    def _detect_skill_invocation(self, content: str) -> Optional[dict]:
        """Detect /skill-name pattern or <command-name> tags in message content."""
        # Claude Code XML tags (when coming through LiteLLM)
        match = re.search(
            r"<command-name>/?(\w[\w-]*)</command-name>", content
        )
        if match:
            args_match = re.search(
                r"<command-args>(.*?)</command-args>", content, re.DOTALL
            )
            return {
                "name": match.group(1),
                "args": args_match.group(1).strip() if args_match else "",
                "source": "claude-code",
            }

        # Direct /command pattern (browser WebUI)
        match = re.match(r"^/(\w[\w-]*)\s*(.*)", content.strip())
        if match:
            name = match.group(1)
            args = match.group(2).strip()
            # Don't match common non-skill commands
            if name not in ("project", "help", "clear", "reset"):
                return {
                    "name": name,
                    "args": args,
                    "source": "browser",
                }

        return None

    # --- Skill Expansion ---

    def _expand_skill(self, skill_info: dict, args: str) -> str:
        """Expand a skill into the injection format."""
        fm = skill_info["frontmatter"]
        content = skill_info["content"]
        skill_dir = skill_info["skill_dir"]
        name = fm.get("name", skill_info["dir_name"])

        # Substitute ${CLAUDE_SKILL_DIR}
        content = content.replace("${CLAUDE_SKILL_DIR}", skill_dir)

        # Build the injection in Claude Code's format
        base_dir_line = f"Base directory for this skill: {skill_dir}\n\n"

        parts = [
            f"<command-message>{name}</command-message>",
            f"<command-name>/{name}</command-name>",
            f"<command-args>{args}</command-args>",
            base_dir_line + content,
        ]

        if args:
            parts.append(f"\n\nARGUMENTS: {args}")

        return "\n".join(parts)

    def _estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    # --- Inlet ---

    async def inlet(self, body: dict, __user__: Optional[dict] = None, __metadata__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        # Build skill registry on first use
        if not self._registry_built:
            self._build_skill_registry()

        # Get chat_id for per-chat state
        chat_id = None
        if __metadata__:
            chat_id = __metadata__.get("chat_id", None)
        if not chat_id:
            chat_id = "__default__"

        # --- Detect working directory ---

        # 1. Check if already set for this chat
        project_dir = self._chat_projects.get(chat_id)

        # 2. Try to parse from Claude Code system messages
        if not project_dir:
            cwd = self._detect_cwd_from_messages(messages)
            if cwd:
                # Walk up to find .claude/ or CLAUDE.md
                check = cwd
                while check and check != "/":
                    if os.path.isdir(
                        os.path.join(check, ".claude")
                    ) or os.path.isfile(os.path.join(check, "CLAUDE.md")):
                        project_dir = check
                        break
                    check = os.path.dirname(check)
                if project_dir:
                    self._chat_projects[chat_id] = project_dir
                    # Rebuild registry to include this project
                    self._registry_built = False
                    self._build_skill_registry()

        # --- Handle user messages ---

        last_msg = messages[-1]
        if last_msg.get("role") != "user":
            return body

        content = last_msg.get("content", "")
        if not isinstance(content, str):
            return body

        # --- /project command ---

        project_match = re.match(r"^/project\s*(.*)", content.strip())
        if project_match:
            project_args = project_match.group(1).strip()
            result = self._handle_project_command(project_args)

            if result and os.path.isdir(result):
                # It's a path — set the project
                project_dir = result
                self._chat_projects[chat_id] = project_dir
                # Rebuild registry to prioritize this project's skills
                self._registry_built = False
                self._build_skill_registry()
                # Load CLAUDE.md
                claude_md = self._load_claude_md(project_dir)
                self._chat_claude_md[chat_id] = claude_md

                # Build skill list for this project
                project_skills = []
                for sname, sinfo in self._skill_registry.items():
                    if sinfo.get("project_dir") == project_dir and sinfo.get("user_invocable"):
                        if sname == sinfo["frontmatter"].get("name", sinfo["dir_name"]):
                            desc = sinfo["frontmatter"].get("description", "")
                            project_skills.append(f"  /{sname} — {desc[:80]}")

                response_lines = [
                    f"Project set to: `{project_dir}`",
                ]
                if claude_md:
                    response_lines.append("CLAUDE.md loaded.")
                if project_skills:
                    response_lines.append(f"\n**Available skills ({len(project_skills)}):**")
                    response_lines.extend(sorted(project_skills))

                # Replace user message with the response
                last_msg["content"] = (
                    f"[System: Project initialized to {project_dir}]\n\n"
                    + "\n".join(response_lines)
                    + "\n\nAcknowledge the project is set and list the available skills."
                )

                # Inject CLAUDE.md as system message
                if claude_md:
                    system_msg = {"role": "system", "content": claude_md}
                    # Insert after any existing system messages
                    insert_idx = 0
                    for i, msg in enumerate(messages):
                        if msg.get("role") == "system":
                            insert_idx = i + 1
                        else:
                            break
                    messages.insert(insert_idx, system_msg)
                    body["messages"] = messages

                return body

            elif result:
                # It's display text (project list or error)
                last_msg["content"] = (
                    f"[System: Project listing]\n\n{result}\n\n"
                    "Show this project listing to the user."
                )
                return body
            else:
                last_msg["content"] = (
                    f"[System: Project not found: '{project_args}']\n\n"
                    f"Could not find a project matching '{project_args}'. "
                    "Use `/project` to list available projects."
                )
                return body

        # --- Skill invocation ---

        invocation = self._detect_skill_invocation(content)
        if invocation:
            skill_name = invocation["name"]
            skill_args = invocation["args"]

            # Look up in registry
            skill_info = self._skill_registry.get(skill_name)

            if skill_info:
                # If we don't have a project set yet, use the skill's project
                if not project_dir and skill_info.get("project_dir"):
                    project_dir = skill_info["project_dir"]
                    self._chat_projects[chat_id] = project_dir
                    # Load CLAUDE.md for this project
                    claude_md = self._load_claude_md(project_dir)
                    self._chat_claude_md[chat_id] = claude_md

                # Expand the skill
                expansion = self._expand_skill(skill_info, skill_args)

                # Check token budget
                tokens = self._estimate_tokens(expansion)
                if tokens > self.valves.max_skill_tokens:
                    # Truncate content portion, keep frontmatter and structure
                    max_chars = self.valves.max_skill_tokens * 4
                    expansion = expansion[:max_chars] + "\n\n[Content truncated for context budget]"

                # Replace the user message content with the expansion
                if invocation["source"] == "browser":
                    last_msg["content"] = expansion
                # For claude-code source, the tags are already there;
                # we just need to inject the skill content

                # Inject CLAUDE.md if not already done for this chat
                claude_md = self._chat_claude_md.get(chat_id, "")
                if not claude_md and project_dir:
                    claude_md = self._load_claude_md(project_dir)
                    self._chat_claude_md[chat_id] = claude_md

                if claude_md:
                    # Check if CLAUDE.md is already in the messages
                    already_injected = any(
                        "CLAUDE.md" in msg.get("content", "")
                        for msg in messages
                        if msg.get("role") == "system"
                    )
                    if not already_injected:
                        system_msg = {"role": "system", "content": claude_md}
                        insert_idx = 0
                        for i, msg in enumerate(messages):
                            if msg.get("role") == "system":
                                insert_idx = i + 1
                            else:
                                break
                        messages.insert(insert_idx, system_msg)
                        body["messages"] = messages

            else:
                # Skill not found — let the model know
                available = [
                    f"/{n}"
                    for n, info in self._skill_registry.items()
                    if info.get("user_invocable")
                    and n == info["frontmatter"].get("name", info["dir_name"])
                ]
                suggestion = ""
                if available:
                    suggestion = (
                        f"\n\nAvailable skills: {', '.join(sorted(available)[:15])}"
                    )
                    if not project_dir:
                        suggestion += (
                            "\n\nTip: Use `/project <name>` to set a project "
                            "context first."
                        )

                last_msg["content"] = (
                    f"[System: Skill '/{skill_name}' not found]{suggestion}\n\n"
                    f"Tell the user that the skill '/{skill_name}' was not found. "
                    f"Suggest available skills if any."
                )

        return body
