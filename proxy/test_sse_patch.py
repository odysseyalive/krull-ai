"""Regression tests for sse_patch.py — focused on the slash-command
dispatch logic, since misclassification there produces the worst failure
mode (model describes the skill instead of running it → hallucinated
content).

Run: `python proxy/test_sse_patch.py` from the repo root. Exits non-zero
on any failure. Add a case whenever a real-world input misclassifies.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

# Stub third-party deps so sse_patch.py imports in a bare env (tests only
# exercise pure-Python classifiers — HTTP client + web framework aren't
# needed).
sys.modules.setdefault("httpx", types.ModuleType("httpx"))

_fastapi = types.ModuleType("fastapi")
class _FastAPI:  # minimal stub — any attribute returns a no-op decorator
    def __init__(self, *a, **kw): pass
    def __getattr__(self, _name):
        def method(*a, **kw):
            def deco(fn): return fn
            return deco
        return method
class _Request: pass
_fastapi.FastAPI = _FastAPI
_fastapi.Request = _Request
sys.modules["fastapi"] = _fastapi

_fastapi_resp = types.ModuleType("fastapi.responses")
class _StreamingResponse:
    def __init__(self, *a, **kw): pass
class _Response:
    def __init__(self, *a, **kw): pass
_fastapi_resp.StreamingResponse = _StreamingResponse
_fastapi_resp.Response = _Response
sys.modules["fastapi.responses"] = _fastapi_resp

# Redirect log output to a tempdir so the module-level open() succeeds.
os.environ["KRULL_LOG_DIR"] = tempfile.mkdtemp(prefix="sse-test-logs-")

sys.path.insert(0, str(Path(__file__).parent))
import sse_patch  # noqa: E402


def _check(label: str, got, expected) -> bool:
    ok = got == expected
    marker = "OK  " if ok else "FAIL"
    print(f"{marker}  {label}  expected={expected!r}  got={got!r}")
    return ok


# ---------------------------------------------------------------------------
# _looks_like_meta_question
# ---------------------------------------------------------------------------
# Rule: false negatives (treat meta-question as task) are SAFE — the model
# just runs the procedure. False positives (treat task as meta-question)
# are DANGEROUS — the model describes the skill and hallucinates content.
# Weight the test corpus accordingly: lots of real tasks that must be False.
META_QUESTION_CASES = [
    # --- Real tasks — MUST be False (hallucinate-risk if True) ---
    ("translate alpha bravo charlie", False),
    ("translate hello world", False),
    ("build a landing page for example corp", False),
    ("describe the output format for the pipeline", False),
    ("explain how this module works in markdown", False),
    ("reply to colleague asking \"is it ready?\"", False),
    ("run the full pipeline on input.csv", False),
    ("write a post about the deploy", False),
    ("status", False),
    ("research SSE proxies", False),
    ("implement TICKET-001", False),
    ("log finding: cache works", False),
    ("", False),
    # --- Meta-questions — SHOULD be True ---
    ("what can this skill do?", True),
    ("what does this skill do", True),
    ("can you translate both directions?", True),
    ("how does mode selection work", True),
    ("tell me about the available modes", True),
    ("which models are supported?", True),
    ("is it able to do reverse lookup", True),
    ("will this work on large files", True),
]


def test_meta_question_detector() -> bool:
    print("\n=== _looks_like_meta_question ===")
    ok = True
    for args, expected in META_QUESTION_CASES:
        got = sse_patch._looks_like_meta_question(args)
        if not _check(f"args={args!r}", got, expected):
            ok = False
    return ok


# ---------------------------------------------------------------------------
# _parse_slash_command
# ---------------------------------------------------------------------------
PARSE_CASES = [
    # Shape "initial": raw slash-command user input from Claude Code
    (
        "<command-name>/skill-x</command-name>\n"
        "<command-args>translate alpha bravo charlie</command-args>",
        ("skill-x", "translate alpha bravo charlie", "initial"),
    ),
    # Shape "loaded": Claude Code's skill-content injection
    (
        "Base directory for this skill: /home/x/.claude/skills/skill-x\n"
        "ARGUMENTS: translate alpha bravo charlie",
        ("skill-x", "translate alpha bravo charlie", "loaded"),
    ),
    # Non-slash content: must return None so we don't fire directives on
    # plain follow-up questions.
    ("what does the skill-x skill do?", None),
    ("", None),
]


def test_parse_slash_command() -> bool:
    print("\n=== _parse_slash_command ===")
    ok = True
    for content, expected in PARSE_CASES:
        got = sse_patch._parse_slash_command(content)
        if not _check(f"content={content[:60]!r}", got, expected):
            ok = False
    return ok


# ---------------------------------------------------------------------------
# End-to-end: real regression — `/skill-x translate ...` MUST get the
# strict followthrough template, not the meta-answer template.
# ---------------------------------------------------------------------------
def test_loaded_task_uses_strict_template() -> bool:
    print("\n=== /skill-x translate → strict followthrough ===")
    loaded_content = (
        "Base directory for this skill: /home/x/.claude/skills/skill-x\n"
        "ARGUMENTS: translate alpha bravo charlie delta echo"
    )
    messages = [{"role": "user", "content": loaded_content}]
    out = sse_patch.inject_slash_command_protocol(messages)
    # Followthrough appends to the user message; new forcing directive
    # appends a system message at the end. Check the user message.
    user_msg = next(m for m in out if m.get("role") == "user")
    injected = sse_patch._content_text(user_msg.get("content", ""))
    ok = True
    if "[Krull Skill Follow-Through]" not in injected:
        print("FAIL  expected [Krull Skill Follow-Through] in injected content")
        ok = False
    else:
        print("OK    [Krull Skill Follow-Through] present")
    if "[Krull Skill Question]" in injected:
        print("FAIL  unexpected [Krull Skill Question] (meta-answer misfire)")
        ok = False
    else:
        print("OK    [Krull Skill Question] correctly absent")
    return ok



def test_forcing_directive_on_loaded_turn() -> bool:
    """Loaded-shape slash command with a task input (non-meta) MUST get
    the end-of-messages forcing directive — this is what prevents the
    '9B refuses as out of scope' failure."""
    print("\n=== forcing directive → fires on loaded task turn ===")
    loaded_content = (
        "Base directory for this skill: /home/x/.claude/skills/skill-x\n"
        "ARGUMENTS: translate alpha bravo charlie delta echo"
    )
    messages = [{"role": "user", "content": loaded_content}]
    out = sse_patch.inject_slash_command_protocol(messages)
    # Forcing directive must be the LAST message and a system role
    last = out[-1]
    ok = _check("last msg is system", last.get("role"), "system")
    ok = _check(
        "contains Skill Execution marker",
        "[Skill Execution — Next Action]" in sse_patch._content_text(last.get("content", "")),
        True,
    ) and ok
    # Idempotent
    out2 = sse_patch.inject_slash_command_protocol(out)
    count = sum(
        1 for m in out2
        if m.get("role") == "system"
        and "[Skill Execution — Next Action]" in sse_patch._content_text(m.get("content", ""))
    )
    ok = _check("idempotent (exactly 1 forcing directive)", count, 1) and ok
    return ok


def test_forcing_directive_skipped_on_meta_question() -> bool:
    """Meta-question on a loaded turn must NOT get the forcing directive
    — those questions are answered, not executed."""
    print("\n=== forcing directive → skipped on meta-question ===")
    loaded_content = (
        "Base directory for this skill: /home/x/.claude/skills/skill-x\n"
        "ARGUMENTS: what does skill-x do"
    )
    messages = [{"role": "user", "content": loaded_content}]
    out = sse_patch.inject_slash_command_protocol(messages)
    found = any(
        m.get("role") == "system"
        and "[Skill Execution — Next Action]" in sse_patch._content_text(m.get("content", ""))
        for m in out
    )
    return _check("forcing NOT injected on meta-question", found, False)


def test_quote_unquoted_paths_with_spaces() -> bool:
    """Auto-quote unquoted .sh paths with spaces; leave others alone."""
    print("\n=== _quote_unquoted_paths_with_spaces ===")
    cases = [
        # Spaced unquoted path — MUST be fixed
        (
            'bash /home/francis/Google Drive/uni/lib/s.sh "arg"',
            'bash "/home/francis/Google Drive/uni/lib/s.sh" "arg"',
        ),
        # Already quoted — leave alone
        (
            'bash "/home/francis/Google Drive/uni/lib/s.sh" "arg"',
            'bash "/home/francis/Google Drive/uni/lib/s.sh" "arg"',
        ),
        # Single-quoted — leave alone
        (
            "bash '/home/x y/s.sh' arg",
            "bash '/home/x y/s.sh' arg",
        ),
        # No space in path — no change
        (
            'bash /home/x/s.sh one two three',
            'bash /home/x/s.sh one two three',
        ),
        # cd prefix, then bash with spaced path
        (
            'cd /tmp && bash /a b/c.sh x',
            'cd /tmp && bash "/a b/c.sh" x',
        ),
        # No .sh — no change
        ('echo hello world', 'echo hello world'),
        # Empty
        ('', ''),
        # sh interpreter also handled
        (
            'sh /some path/thing.sh foo',
            'sh "/some path/thing.sh" foo',
        ),
    ]
    ok = True
    for cmd, expected in cases:
        got = sse_patch._quote_unquoted_paths_with_spaces(cmd)
        if not _check(f"cmd={cmd!r}", got, expected):
            ok = False
    return ok


def test_fix_tool_call_params_quotes_bash() -> bool:
    """End-to-end: fix_tool_call_params must rewrite the Bash command
    when the path has spaces and isn't quoted."""
    print("\n=== fix_tool_call_params → rewrites Bash command ===")
    original = '{"command": "bash /home/x y/script.sh \\"My sentence\\""}'
    fixed = sse_patch.fix_tool_call_params("Bash", original)
    fixed_obj = __import__("json").loads(fixed)
    expected = 'bash "/home/x y/script.sh" "My sentence"'
    return _check("rewritten command", fixed_obj["command"], expected)


def test_build_procedure_map() -> bool:
    """Mechanical extraction must surface headers, in-tree refs (only
    those that resolve), code-block invocations. No semantic guesses."""
    print("\n=== _build_procedure_map ===")
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        skill = Path(tmp) / "skill-x"
        (skill / "lib").mkdir(parents=True)
        (skill / "refs").mkdir()
        (skill / "lib" / "helper.sh").write_text("#!/bin/bash\necho hi\n")
        (skill / "refs" / "rules.md").write_text("# rules\n")
        content = (
            "# Top\n"
            "## Phase 1: Lookup\n"
            "Use [helper](lib/helper.sh) to do the lookup.\n"
            "Then check refs/rules.md for guidance.\n"
            "## Phase 2: Verify\n"
            "Also see fakes/missing.md (does not exist - filtered).\n"
            "```bash\n"
            "bash lib/helper.sh word1 word2\n"
            "```\n"
            "External: [link](https://example.com/x)\n"
        )
        got = sse_patch._build_procedure_map(content, skill.resolve())

    ok = True
    for must in ("Phase 1: Lookup", "Phase 2: Verify",
                 "lib/helper.sh", "refs/rules.md",
                 "bash lib/helper.sh"):
        ok = _check(f"contains {must!r}", must in got, True) and ok
    for must_not in ("fakes/missing.md", "https://example.com"):
        ok = _check(f"absent {must_not!r}", must_not not in got, True) and ok
    return ok


def main() -> int:
    results = [
        test_meta_question_detector(),
        test_parse_slash_command(),
        test_loaded_task_uses_strict_template(),
        test_quote_unquoted_paths_with_spaces(),
        test_fix_tool_call_params_quotes_bash(),
        test_forcing_directive_on_loaded_turn(),
        test_forcing_directive_skipped_on_meta_question(),
        test_build_procedure_map(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{passed}/{total} test groups passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
