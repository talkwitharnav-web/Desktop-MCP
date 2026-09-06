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
Those observation cases also cover bounded final-sample tile hashing,
`spatial_change` geometry/crop hints, tiny changes lost by image downscaling,
unchanged reuse, incompatible/expired references and cancellation during local
preprocessing. Tool-response cases retain complete metadata and image blocks
while making the optional detail-crop hint explicit about physical coordinates.
These synthetic comparisons do not measure model accuracy or native capture latency.
`test_desktop_window_targets.py` uses a strict fake Win32 query port for receiver
ordering, minimized roots/children, click-through overlays, modal routing and
content-free metadata. `test_desktop_diagnostics.py` carries those native fakes
through real MCP calls: scaled frame coordinates, caller/generation attribution,
partial/completed input warnings, error flags, and exclusion of arbitrary
exception fields or unvalidated frame arguments.
`test_desktop_control_ui.py` and `test_desktop_cursor.py` use native fakes and
synthetic renderings. `test_desktop_teaching.py` exercises the pure teaching model
and renderer. These are not evidence of real Windows hook behavior or appearance.
`test_desktop_teaching_render.py` covers continuous laser seams, fractional ellipse
geometry, lifetime-independent orbit speed, open-path/point fades, clipping,
bounded supersampling/cache and image ownership. Its synthetic frame/cost
comparisons do not establish native timer/upload performance or universal 60Hz;
heavy mixed or multi-laser scenes can exceed a single frame budget.

`test_desktop_conversation.py` covers the real MCP chat tools around a fake
desktop: incoming delivery, reply acknowledgements, exclusive listeners, queue
limits, timeout/cancellation, paused chat and visibility without input grants.
Service tests use `StdioTransport(keep_alive=False)` when testing actual client
disconnection; leaving a transport alive is not evidence of a disconnect.
Transcript UI tests cover Send/draft retention, Enter/Shift+Enter/IME handling,
compact history/composer/status/control layout, and capture-safe local toggles.
The two `test_desktop_transcript_chat*` suites cover native-message presentation
with fakes: role boxes, complete long/Unicode text, directional selection,
reading/pruning, inner/outer slim scrolling, page-wheel spillover, held-thumb
follow suppression, arrival lifecycle and terminal cleanup after partial failure.
The fake distinguishes ordered `EM_GETSEL` bounds from anchor/active endpoints.

`test_desktop_interaction.py` covers task-wide ownership, rejected-helper
disconnects, queued corrections, timely post-input observation, per-client image
reuse, schema bounds and bounded observation-only focus recovery. These use
synthetic desktops/images and real MCP/IPC where relevant, never the report's
live Chrome/Slides document or historical frame ids.

Launch/lifecycle changes use `test_desktop_pipe_transport.py` and
`test_desktop_service.py`: real current-user Windows named pipes and subprocess
stdio clients around a fake desktop, without hotkeys, input or screenshots.
They cover simultaneous clients, cancelled I/O, a client leaving early, and X
ending bridges even while the client still holds stdin open. `create_server`
tests retain the default owned lifespan; the production shared host uses one
application and independent per-client protocol lifespans.

`test_desktop_launch_live.py` is a separately opt-in, **no-input** native launch
exercise. It starts only its own instance, sends a known message to its owned
composer/Send-command handler, reads/replies through the real MCP connection,
checks visibility, and posts X only to its own window handles. It requires the
application to exit. This verifies native control plumbing, not physical
click/foreground behavior. It does not arm, inject desktop input, capture, or
close other apps. Stop any previous Desktop-MCP instance normally before running it.
It also checks compact geometry, protected HWND roles, draft/selection retention
through Expand/Compact, and the actual hit receiver above the taskbar after a
local Pin + Taskbar edge choice, without clicking the taskbar.

## Opt-in native exercise

`tests\test_desktop_live.py` is skipped unless `DESKTOP_MCP_LIVE=1`. Run it only on
an unlocked interactive desktop, with no other Desktop-MCP process owning the stop
hotkey. Its input exercise creates a harmless EDIT window in a separately owned
GUI subprocess; never replace it with a user's app. The strict same-process
permission/composer guard is not weakened to accommodate a test window.
`tests\desktop_live_fixture.py` exchanges only fixture HWND/PID metadata and
mouse-event records over a small local JSON protocol. It locally arms only for
the fixture and leaves the controller stopped at shutdown.
If Windows refuses the initial foreground request, the input exercise skips
before Arm or input. This is unavailable coverage, not a successful input test;
do not force foreground to make the test green.

```powershell
$env:DESKTOP_MCP_LIVE = '1'
$env:DESKTOP_MCP_LIVE_APPEARANCE = '0'
# Use a NEW, uniquely named directory under your session artifact folder.
$env:DESKTOP_MCP_LIVE_ARTIFACTS = 'C:\path\session\files\native-unique-run'
.\.venv\Scripts\python.exe -m pytest tests\test_desktop_live.py -q -s
```

The harness checks real Unicode input, sampled pointer movement, extra buttons,
both wheel axes, MCP image blocks, and an actual registered Ctrl+Shift+H during a
held-button drag. It double-clicks its own EDIT and checks native edit focus
before replacing text, then verifies the actual resulting control buffer.
A DPI-aware, opaque owned fixture covers its monitor's work area
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
The `native_compact_transcript_appearance_without_foreground` selector provides
separate compact/expanded UI evidence without arming, moving the pointer or
requesting foreground. It pins only its owned transcript above an opaque owned
backdrop, retains the overlap/privacy checks, and also requires the explicit
appearance flag and artifact directory. It does not replace real input,
hotkey, IME or multi-monitor transition coverage.
Read-only appearance fixtures use a non-activating backdrop. Local Pin-button
behavior is completed before measuring the separate no-focus-stealing contract
for programmatic transcript updates; capture guards are never disabled.
`test_desktop_scroll_live.py` uses the same opt-in and privacy boundary for
repeated native resizing, scrolling and repaint verification. Its first image
does **not** call ShowWindow, repositioning or RedrawWindow: comparing it to a
subsequent forced full erase/repaint detects stale pixels rather than repairing
them before inspection. It checks draft/selection/reading anchors, visible dark
scrollbar pixels, separate message/history/composer wheel/page/track/thumb paths
and capture release. Native history is inspected through its actual message
EDIT children, not invented EDIT messages sent to container 301.
Its arrival probe records real window positions after native render callbacks
and external samples while respecting the Windows animation preference. External
queries may arrive after a short transition; neither trace measures compositor FPS.
The recorded physical monitor DPI is distinct from controlled
`WM_DPICHANGED` reflows used to exercise other rendering scales. No Windows DPI
settings are changed; these reflows are not evidence of real monitor transitions.

The same fixture uses the same Arm authorization for guidance and input, checks transcript
stacking/pinning without focus theft, renders/erases ink and a laser, verifies their
capture exclusion, and reaches a cursor-dwell target. It tests both the local
instruction-window Stop button and the global hotkey during a teaching wait.
The trusted fixture emulates only that harmless stop chord and learner motion;
MCP still has no remote Arm. `test_desktop_unified.py` also covers the real tool
chain around a fake desktop: explain/highlight/click/explain, and learner
dwell followed by an agent click without a second authorization. Ordinary
idle movement does not stop the session; interruption during automated input
and Ctrl+Shift+H still revoke it.
It also opens only Desktop-MCP's own system menus and verifies capture-guard
acknowledgement plus both control/transcript shutdowns while Windows is running
those modal menu loops.
Set `DESKTOP_MCP_LIVE_BACKEND=auto` for a separate owned-fixture run of the actual
DXCAM/MSS/Pillow selection path; its chosen backend is reported. The default
native harness backend remains `mss` for deterministic fixture checks.
The artifact directory records the exact host/fixture PIDs and window handles for recovery
if the fixture fails. Never terminate a process by name or infer a cleanup root.

The opt-in `native_control_accessibility_and_compact_layout` selector reads
native owned-control text and verifies a compact panel's bounds without arming
input or requiring foreground permission. Fake geometry cases cover high DPI
and small/negative-origin work areas; they do not change Windows display settings.

After integration, validate distribution metadata with
`.\.venv\Scripts\python.exe scripts\check_versions.py`, and use the existing
`python -m uv build` packaging path. A local build is not package publication.
`test_desktop_packaging.py` also checks that MCP initialization and the guide
resource return the packaged Markdown, even outside the repository directory.
Distribution inspection must confirm the guide is included in the wheel/source
archive, not merely present in an editable checkout.

## Portable setup checks

`tests\test_desktop_setup.py` uses owned fixture folders and mocked bootstrap
processes/network responses; it does not install uv/Python, register a real
shortcut, or read/write the user's Copilot configuration. Cases cover invocation
from another working directory, spaces and Unicode, bounded checksum-verified
downloads, incompatible uv fallback, nested reparse rejection and the narrowly
validated owned uv Python alias.

Configuration cases cover protected permissions before payload writes, original
backups and incomplete-commit blocking, case-insensitive Windows recovery names,
actual `pywintypes.error` handling, completed-marker publication, longer timeouts
and preservation of other server/settings data. Partial native replacement
failures are modeled on owned data; never provoke them against live configuration
or use cleanup/path-selection mutation testing.

Run these with the existing pytest environment. `Setup.cmd -WhatIf` provides a
read-only installation plan. These checks do not claim a clean-machine bootstrap
or support for ARM/32-bit Windows. Source-distribution inspection must also find
`Setup.cmd`, `scripts/setup.ps1` and `scripts/configure_copilot.py`.

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
