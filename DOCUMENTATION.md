# Documentation ownership

Keep a short entrypoint and task-specific references, not a growing session diary.

| Information | Canonical owner |
|---|---|
| Safety and task routing | `CLAUDE.md` |
| Delegation, model and review workflow | `agent-work.md` |
| Architecture and coupled contracts | `SYSTEM_MEMORY.md` |
| Deliberate product choices | `DECISIONS.md` |
| Installation and ordinary operation | `README.md` |
| Instructions sent to connected MCP agents | `src/desktop_mcp/AGENT_GUIDE.md` |
| Validation prerequisites and hazards | `TESTING.md` |

Update the rule where it lives. Explain the reason and owning symbol when that
prevents a future regression. Keep measured evidence separate from source-based
inferences; a decided feature is not automatically an implemented feature.
Preserve meaningful exceptions, field names, coordinate units, lock ordering,
failure behavior and privacy boundaries. Do not copy other projects' secrets,
host inventories or domain-specific rules.

Use patch tools for manual Markdown edits. Keep links and commands real. Put
temporary reports and screenshots outside the repository, not in these guides.

The packaged agent guide is consumed directly by `app.create_server` and the
`desktop-mcp://guide` resource. Update that file when operating instructions
change; do not recreate an inline instruction string or rely on a client reading
the server's filesystem. Contributor rules in `CLAUDE.md` are a separate concern.
