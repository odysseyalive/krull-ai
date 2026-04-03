# Consultation Protocol

How agents query the ledger during consultation.

## Triage Rules

Before spawning agents, determine the match scope:

1. Read `ledger/index.md`
2. Match tags against the current context (file paths, function names, component names)
3. Identify which record types have matching entries

## Proportional Agent Spawning

| Match Scope | Agents Spawned | Rationale |
|-------------|----------------|-----------|
| No records match | None | Zero overhead until records exist |
| Only incidents/flows match | Regression Hunter only | Single relevant perspective |
| Only decisions/patterns match | Skeptic only | Single relevant perspective |
| Risk/failure language detected | Premortem Analyst only | Targeted premortem |
| Multiple record types match | All three agents (full panel) | Cross-referencing needed |

## Synthesis Rules

After agents return findings:

- **Agreement** (2+ agents flag the same concern) = **HIGH confidence** warning
- **Disagreement** (agents conflict on a concern) = **the signal** -- investigate the disagreement itself, don't average the opinions
- **Single agent finding** = **MEDIUM confidence** consideration

## Consultation Briefing Format

```markdown
## Ledger Consultation

### Warnings (HIGH confidence -- agents agree)

- **[Warning]** -- [INC/DEC/PAT/FLW reference] -- [one-line explanation]

### Considerations (agents disagree -- investigate the disagreement)

- **[Topic]**
  - Regression Hunter: [finding]
  - Skeptic: [finding]
  - Premortem Analyst: [finding]

### Context (relevant records, no warnings)

- [ID] -- [why it's relevant but not a warning]

### No Records

No ledger records match the current context. Proceeding without historical consultation.

### Capture Opportunity

The current conversation contains knowledge not yet in the ledger:
- **Suggested type:** [INC/DEC/PAT/FLW]
- **Suggested ID:** [auto-generated from context]
- **Source material:** [quote from conversation]

Confirm to record, or skip.
```
