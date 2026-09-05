# Contributing to Desktop-MCP

Read [CLAUDE.md](CLAUDE.md) first, then the references relevant to the change.
The retained `windows_mcp` package is upstream groundwork; put supervised
behavior in `desktop_mcp` rather than duplicating the Windows/UIA implementation.
Preserve the original license and attribution notices.

Use Windows, Python 3.14+ and UV. The existing development tools are pytest and
Ruff. See [TESTING.md](TESTING.md) before running anything that might exercise
desktop input, create windows or capture images.

```powershell
python -m uv sync --frozen --extra dev
.\.venv\Scripts\python.exe -m pytest tests
.\.venv\Scripts\python.exe -m ruff check src\desktop_mcp
```

## Working conventions

- Preserve existing work and work on `main` unless the user requests otherwise.
  Commit and sync a baseline before delegating edits.
- Follow [agent-work.md](agent-work.md): exclusive file ownership, explicit
  invariants, current-revision evidence and the required review model.
- Use typed interfaces and shared contracts. Do not bypass the controller in a
  compatibility tool, invent a second coordinate conversion, or catch an input
  error and report success.
- Keep the native UI thread independent of the serial action lock. Revocation
  must invalidate old queued work; local re-arming is not permission to revive it.
- Keep conversation independent of desktop permission. Transcript reads must not
  hold the input sequence lock, replies acknowledge only their listener's message,
  and visibility toggles never arm input. Keep local composer drafts on send errors.
- Use Ruff formatting, 100-character lines, descriptive snake_case functions,
  type annotations and concise docstrings for public interfaces.
- Update the canonical documentation for any changed contract. Follow
  [DOCUMENTATION.md](DOCUMENTATION.md), not a chronological session diary.
- Keep screenshots, credentials, private window content and virtual environments
  out of commits. Never mutation-test cleanup/path selection or touch a user's
  existing apps as a test fixture.

Changes involving geometry, focus, input or the global stop need a controlled
real Windows exercise in addition to source-level tests. Use only a harmless
fixture owned by that exercise. A simulated backend is not proof of real input,
and a transport image assertion is not proof a particular model client sees it.

Package, lockfile, extension and registry-template versions are coupled through
`scripts/check_versions.py`. The manually started packaging workflow uploads
artifacts without writing repository contents or publishing to PyPI. Dependency
updates and merges are owner-controlled, not delegated to bots.
