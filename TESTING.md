# Validation scope and safe fixtures

## Environment

Use Windows and Python 3.14+. The project already uses pytest and Ruff:

```powershell
uv sync --extra dev
uv run pytest tests
uv run ruff check src\desktop_mcp
```

Use the smallest relevant pytest selection while developing. The retained
upstream tests are in `tests`; new controller, UI-rendering and observation tests
live alongside them. Dependency versions are resolved in `uv.lock`.

## Safety

Most tests must use injected fake input/capture providers and synthetic images.
They must not type into an existing application, modify a user's clipboard,
launch arbitrary executables, change registry settings, or capture private
windows. A real UI exercise uses a specifically created harmless fixture and
only its own window rectangle.

Never mutation-test cleanup or path selection. Never infer scratch ownership
from the working directory. Do not stop processes by name. The stop-control
exercise must not rely on a dangerous command being refused.

An image encoder or model-metadata assertion is not an end-to-end vision
assertion. Distinguish an actual image result, local rendered UI evidence, and
the separate question of whether a particular MCP client forwards those pixels
to its model. Do not replace failed or inconclusive evidence with a success.
