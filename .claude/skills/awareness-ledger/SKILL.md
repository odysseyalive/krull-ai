---
name: awareness-ledger
description: "Institutional memory for your project. Commands: record, consult, review. Usage: /awareness-ledger [command] [args]"
allowed-tools: Read, Glob, Grep, Write, Edit, Task, TaskCreate, TaskUpdate, TaskList, TaskGet
---

# Awareness Ledger

Institutional memory that persists incidents, decisions, patterns, and flows across sessions.

## Quick Commands

| Command | Action |
|---------|--------|
| `/awareness-ledger record [type]` | Create a new record (INC, DEC, PAT, FLW) |
| `/awareness-ledger consult [topic]` | Query ledger for relevant history before making changes |
| `/awareness-ledger review` | Health check: stale entries, missing links, tag drift, statistics |

## Directives

*(No user directives yet. User rules will be recorded here verbatim when provided.)*

### Auto-Consultation (READ)

During research and planning -- before formulating any plan, recommendation, or
code change proposal -- automatically consult the ledger:

1. **Index scan** -- Read `ledger/index.md` and match tags against the files,
   directories, and components under discussion. This is free -- the index is
   small. Do this as part of your initial research, alongside reading source
   files.
2. **Record review** -- If matching records exist, read the full record files.
   Incorporate warnings, known failure modes, and relevant decisions into your
   thinking before presenting any plan to the user. This is cheap -- records
   are short.
3. **Agent escalation** -- If high-risk overlap is detected (matching INC records
   with active status, or multiple record types matching the same change area),
   spawn consultation agents proportionally per the agent table in
   `references/consultation-protocol.md`. Present agent findings as part of
   your recommendation. This is expensive -- only when warranted.

The consultation must happen during planning, not at edit time. By the time
code is being written, the plan has already been presented and approved. The
ledger's value is in shaping the plan itself -- surfacing past failures,
challenging assumptions, and providing historical context that changes what
you recommend.

Skip auto-consultation for:
- Changes to `.claude/` infrastructure files
- Trivial edits (typos, formatting, comments)
- Areas with no tag overlap in the index

### Auto-Capture Suggestion (WRITE)

When the current conversation produces institutional knowledge, suggest recording it **after** resolving the immediate issue. Never interrupt active problem-solving to suggest capture.

Automatically suggest capture when you encounter:
- **Bug investigation** with timeline, root cause analysis, or contributing factors -> INC record
- **Architectural decisions** with trade-offs discussed and option chosen -> DEC record
- **Recurring patterns** observed across multiple instances or confirmed by evidence -> PAT record
- **User/system flows** traced step-by-step with code paths identified -> FLW record

Capture suggestions are always user-confirmed. Present the suggestion with:
- Suggested record type and ID
- Key content to capture (quoted from conversation)
- One-line confirmation prompt: "Record this in the awareness ledger? (confirm/skip)"

## Record Command

When invoked with `/awareness-ledger record [type]`:

1. Read the appropriate template from `references/templates.md`
2. Walk through the template fields with the user, pre-filling from conversation context
3. Generate the record ID: `[TYPE]-YYYY-MM-DD-slug`
4. Write to `ledger/[type-plural]/[ID].md`
5. Update `ledger/index.md` with the new entry

## Consult Command

When invoked with `/awareness-ledger consult [topic]`:

1. Read `ledger/index.md` and match tags against [topic]
2. Read matching record files
3. Apply proportional agent spawning per `references/consultation-protocol.md`
4. Synthesize findings into the Consultation Briefing Format
5. Suggest capture if conversation contains unrecorded knowledge

## Review Command

When invoked with `/awareness-ledger review`:

1. Scan all records for staleness (records older than 90 days with active status)
2. Check for broken cross-references (related records that don't exist)
3. Report tag drift (tags used inconsistently across records)
4. Generate statistics table (counts by type and status)

## Grounding

Before using any template or protocol:
1. Read the relevant file from `references/`
2. State which template/protocol you are using

Reference files:
- [references/templates.md](references/templates.md) -- Record type templates (INC/DEC/PAT/FLW)
- [references/consultation-protocol.md](references/consultation-protocol.md) -- Agent spawning rules, synthesis, proportional overhead
- [references/capture-triggers.md](references/capture-triggers.md) -- Conversation signals that suggest a record should be created
