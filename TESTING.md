# Validation scope and safe fixtures

## Environment

Use Windows and Python 3.14+. The project already uses pytest and Ruff:

```powershell
python -m uv sync --frozen --extra dev
.\.venv\Scripts\python.exe -m pytest tests\test_desktop_app.py tests\test_desktop_tools.py tests\test_desktop_teaching_tools.py tests\test_desktop_teaching_ui.py tests\test_desktop_packaging.py
.\.venv\Scripts\python.exe -m ruff check src\desktop_mcp
```

Use the smallest relevant pytest selection while developing. The retained
upstream tests are in `tests`; new controller, UI-rendering and observation tests
live alongside them. Dependency versions are resolved in `uv.lock`.
Do not reinstall dependencies before validation unless a manifest changed or
the selected command reports a missing dependency.

`test_screenshot_capture.py` includes a synthetic execution of the installed
DXCAM `grab`/recovery call chain, COM-failure fallback and cancellation checks.
No display is disconnected or modified to provoke recovery. Unknown DXCAM
versions must fail over until their one-shot recovery boundary is verified.

Core input/revocation checks are in `test_desktop_runtime.py` and
`test_desktop_native.py`; observation checks are in `test_desktop_vision.py`.
`test_desktop_control_ui.py` and `test_desktop_cursor.py` use native fakes and
synthetic renderings. `test_desktop_teaching.py` exercises the pure teaching model
and renderer. These are not evidence of real Windows hook behavior or appearance.

## Opt-in native exercise

`tests\test_desktop_live.py` is skipped unless `DESKTOP_MCP_LIVE=1`. Run it only on
an unlocked interactive desktop, with no other Desktop-MCP process owning the stop
hotkey. It creates its own harmless EDIT window; never replace that fixture with a
user's app. It locally arms only for the fixture and leaves the controller stopped
at shutdown.

```powershell
$env:DESKTOP_MCP_LIVE = '1'
$env:DESKTOP_MCP_LIVE_APPEARANCE = '0'
# Use a NEW, uniquely named directory under your session artifact folder.
$env:DESKTOP_MCP_LIVE_ARTIFACTS = 'C:\path\session\files\native-unique-run'
.\.venv\Scripts\python.exe -m pytest tests\test_desktop_live.py -q -s
```

The harness checks real Unicode input, sampled pointer movement, extra buttons,
both wheel axes, MCP image blocks, and an actual registered Ctrl+Shift+H during a
held-button drag. A DPI-aware, opaque owned fixture covers its monitor's work area
before either interface starts. Captures require owned insets inside that backing
window and refuse unowned windows overlapping the region. This prevents a
capture-excluded interface from revealing an underlying user application.

For native interface/cursor/ink appearance evidence, run the command again in a
**separate process** with a new artifact directory and
`DESKTOP_MCP_LIVE_APPEARANCE=1`. This diagnostic-only run disables optional display
affinity on owned windows at creation; acknowledged hide/flush guards remain real
and are still checked. The production-default run retains display affinity and
saves the actual returned MCP fixture image. Do not confuse the two evidence
scopes. Appearance artifacts remain local and must be viewed; passing a synthetic
image assertion does not verify native layout.

The same fixture selects Teach through the real local controls, checks transcript
stacking/pinning without focus theft, renders/erases ink and a laser, verifies their
capture exclusion, and reaches a cursor-dwell target. It tests both the local
instruction-window Stop button and the global hotkey during a teaching wait.
The trusted fixture emulates only that harmless stop chord and learner motion;
MCP still has no remote arm or teaching-mode input path.
It also opens only Desktop-MCP's own system menus and verifies capture-guard
acknowledgement plus both control/transcript shutdowns while Windows is running
those modal menu loops.
Set `DESKTOP_MCP_LIVE_BACKEND=auto` for a separate owned-fixture run of the actual
DXCAM/MSS/Pillow selection path; its chosen backend is reported. The default
native harness backend remains `mss` for deterministic fixture checks.
The artifact directory records the exact owned PID/window handles for recovery
if the fixture fails. Never terminate a process by name or infer a cleanup root.

The opt-in `native_control_accessibility_and_compact_layout` selector reads
native owned-control text and verifies a compact panel's bounds without arming
input or requiring foreground permission. Fake geometry cases cover high DPI
and small/negative-origin work areas; they do not change Windows display settings.

After integration, validate distribution metadata with
`.\.venv\Scripts\python.exe scripts\check_versions.py`, and use the existing
`python -m uv build` packaging path. A local build is not package publication.

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
