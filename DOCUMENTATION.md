# Documentation ownership

Keep a short entrypoint and task-specific references, not a growing session diary.

| Information | Canonical owner |
|---|---|
| Safety and task routing | `CLAUDE.md` |
| Delegation, model and review workflow | `agent-work.md` |
| Architecture and coupled contracts | `SYSTEM_MEMORY.md` |
| Deliberate product choices | `DECISIONS.md` |
| Installation and ordinary operation | `README.md` |
| Validation prerequisites and hazards | `TESTING.md` |

Update the rule where it lives. Explain the reason and owning symbol when that
prevents a future regression. Keep measured evidence separate from source-based
inferences; a decided feature is not automatically an implemented feature.
Preserve meaningful exceptions, field names, coordinate units, lock ordering,
failure behavior and privacy boundaries. Do not copy other projects' secrets,
host inventories or domain-specific rules.

Use patch tools for manual Markdown edits. Keep links and commands real. Put
temporary reports and screenshots outside the repository, not in these guides.
