# Desktop-MCP

Visible, supervised Windows desktop control, built on
[Windows-MCP](https://github.com/CursorTouch/Windows-MCP).

An MCP client supplies the model and decides what to do. Desktop-MCP supplies the
real mouse, keyboard, screenshots, and a local control window. It is not another
AI model, a remote-desktop service, or a sandbox.

## What changes from stock Windows-MCP

- A rounded black/grey arrow overlay follows real pointer movement. Pointer
  moves, including movement before clicks, accelerate and decelerate smoothly.
- Left, right, middle and extra mouse buttons; modifier-aware drags; native
  horizontal/vertical wheel input; named keys, chords, repeats and batch-scoped
  key/button holds.
- Fast Unicode typing without an artificial per-character speed limit or
  clipboard replacement. Long input stays cancellable.
- An Alt-Tab-accessible control window and a global **Ctrl+Shift+H** stop.
  Control starts stopped and can only be allowed/resumed locally.
- Short serial input batches with a single final observation, instead of a
  model round trip for every key.
- Cropped, resized, efficiently encoded observations with frame IDs and
  server-side coordinate conversion. Bounded adaptive waits detect changes
  without continuously sending redundant screenshots.
- Optional local image files for clients whose native image reader works but
  whose MCP image-result forwarding does not.
- A local **Teach** mode for guidance without injected input: laser pointing and
  circling, persistent erasable screen ink, and real learner-cursor dwell waits.
- A draggable, Alt-Tab-accessible transcript with local pin and top/bottom docking.
  The model publishes instruction steps explicitly; presentation never moves the
  learner's pointer or steals keyboard focus.

## Requirements and installation

Use an interactive Windows 10/11 desktop and Python **3.14+**. The package
metadata, not old upstream installation guides, is authoritative.

In PowerShell, inside this checkout:

```powershell
# If uv is missing:
python -m pip install --user uv

python -m uv sync --frozen --extra dev
```

UV can install the required Python interpreter into its managed environment.
Dependencies remain in this project's `.venv`.

This project is local-first. Do **not** assume `uvx desktop-mcp` installs this
fork: PyPI and MCP registry publication are not part of setting up the checkout.
`server.json` is release metadata, not a publication receipt.

## Connect to Copilot CLI

From the project folder:

```powershell
copilot mcp add desktop-mcp -- "$((Get-Location).Path)\.venv\Scripts\python.exe" -m desktop_mcp serve
```

Alternatively, use `/mcp add` inside Copilot and enter:

| Field | Value |
|---|---|
| Name | `desktop-mcp` |
| Type | Local / STDIO |
| Command | `"C:\path\Desktop-MCP\.venv\Scripts\python.exe" -m desktop_mcp serve` |
| Tools | `*` (the supervised tools listed below) |

The equivalent configuration is:

```json
{
  "mcpServers": {
    "desktop-mcp": {
      "type": "local",
      "command": "C:\\path\\Desktop-MCP\\.venv\\Scripts\\python.exe",
      "args": ["-m", "desktop_mcp", "serve"],
      "tools": ["*"]
    }
  }
}
```

Use the virtual environment's absolute Python path so the connection does not
depend on the client's working directory or PATH. Do not start a second copy
manually while the MCP client is already running it: only one process can own
the global stop hotkey.

To launch without an MCP client for local development:

```powershell
.\.venv\Scripts\desktop-mcp.exe serve
```

STDIO is the only supported transport. No network listener, firewall rule,
administrator elevation, or login startup task is required.

## Start, stop and take over

The control window starts **stopped**. Select **Control** or **Teach** and press
**Arm** (or **Resume**) locally when ready. Changing modes stops the session and requires
another local allow action. The panel minimizes so it does not intercept input;
it remains reachable through Alt-Tab.

**Ctrl+Shift+H** and the panel's Stop control revoke input and captures. Pending
commands from the old generation stay cancelled even after you resume. Keys and
buttons held by Desktop-MCP are released. The model has no `Arm` or `Resume` tool.
Closing the panel stops control rather than leaving an invisible active agent.

In Control mode, human mouse/keyboard input pauses automation by default. The
local window can change that preference; the emergency hotkey remains enabled.
In Teach mode, the learner can freely move the mouse without takeover pauses.
Physical clicks/keys invalidate observations, and all injected mouse/keyboard
input and app launching/focusing are blocked. Local revocation is enforced by the
service, not merely by a sentence asking the model to behave.

The boundary is **this server**. The hotkey does not terminate Copilot, revoke
its shell tools, stop another MCP server, undo completed actions, or erase
information already delivered to a model. Do not work around a stop using other
tools. Normal Windows integrity restrictions still apply; input to an elevated
or locked desktop may be refused.

## Tool surface

| Tool | Purpose |
|---|---|
| `DesktopStatus` | State, stop reason, input revision and activity, without a capture. |
| `DesktopStop` | Latch a stop; never resumes control. |
| `DesktopBatch` | Validate and run a short ordered sequence; observe once afterward. |
| `Screenshot` | Fast visual observation, adaptive waiting, encoding and frame references. |
| `Click`, `Move`, `Scroll` | Smooth pointer movement, any supported button, drags and wheel gestures. |
| `Keyboard`, `Shortcut`, `Type` | Keys/chords/repeats and fast literal text. |
| `Wait` | A cancellable delay with optional observation. |
| `App` | List/focus windows or explicitly launch an executable without a shell. |
| `DisplayInventory` | Physical monitor bounds, DPI and scale. |
| `Snapshot` | Optional heavier Windows accessibility inspection plus an image. |
| `Transcript` | Publish instruction text or request front/back stacking without taking focus. |
| `Laser` | Point, trace a path, or circle a region without moving the real pointer. |
| `Draw`, `Erase` | Persistent context-bound ink; erase only our annotations, never app content. |
| `Cursor`, `WaitForCursor` | Observe the real pointer and wait for vicinity plus continuous dwell. |

Upstream PowerShell, registry, filesystem, process-killing and network-scraping
tools are deliberately not registered. The retained `python -m windows_mcp`
module is the upstream implementation, **not** an alternative supervised
connection. Both installed console aliases, `desktop-mcp` and `windows-mcp`,
launch the supervised entry point.

## Teaching without taking over

Select Teach and press Arm/Resume in the local panel. The floating instruction window is
available immediately, including through Alt-Tab before the first model message.
Drag its title bar, use **Top**/**Bottom** to dock, or **Pin** to keep it above other
windows. A model `Transcript(action="back")` request cannot override a local pin.
Closing the instruction window minimizes it; closing the control window stops the
session.

An agent can publish a step, mark the relevant area, and wait for your pointer:

```json
{"text": "Move your cursor over the Add menu.", "title": "Next step"}
```

Send that to `Transcript`; use `Laser(bounds=[left,top,right,bottom], frame_id=...)`
to circle the area in an observed image, or `Draw` for persistent paths,
rectangles and ellipses. Coordinates without `frame_id` are physical desktop
pixels. These marks are separate click-through visual layers; they never move
your real pointer or modify Blender. `Erase` and the local **Clear ink** button
remove only Desktop-MCP marks.

`WaitForCursor` is available in Teach mode. Its `radius` is physical pixels;
`dwell` requires continuously staying nearby. It returns `reached`, `timeout`,
`context_changed` or `input_changed`; being nearby is **not proof of a click or
successful app action**. A stop cancels the operation rather than returning a
false success. Marks disappear when their context becomes stale or control stops.

The transcript is not automatic mirroring of every Copilot CLI token. The model
must call `Transcript` to publish a useful step. Ink, laser, cursor and control
windows are excluded from server screenshots so guidance does not feed back into
the model's view of the application.

## Use frames, not guessed coordinate math

`Screenshot` defaults to the active application. Use `scope="desktop"` for the
full desktop, or supply an explicit physical-pixel `region=[left,top,right,bottom]`.
Its response contains a real MCP image block, capture/image dimensions and a
`frame_id`.

When clicking a point measured in that image:

```json
{
  "loc": [340, 210],
  "frame_id": "<the Screenshot frame_id>"
}
```

The server converts image pixels using the actual image dimensions and capture
origin, including negative monitor origins and independently rounded x/y scales.
Do not multiply coordinates yourself when supplying `frame_id`.

Without `frame_id`, coordinates are explicitly physical virtual-desktop pixels.
Frame references expire, are bounded in memory, and are rejected after input
changes or relevant window/display geometry changes. A reference is not an
eternal guarantee that an application has not redrawn its own contents.

Coordinate-bound batches guard the observed foreground window. If an action
opens a new dialog or switches applications, use the returned fresh observation
before deciding the next coordinate-based action.

## Faster observations and actions

MCP is a request/response protocol, not a video stream into a model. The efficient
loop is:

1. Observe at a decision point.
2. Send a short batch of already-understood actions.
3. Receive one fresh observation.
4. When waiting for rendering, use `Screenshot(since=..., wait_for_change=...)`
   instead of repeatedly asking the model to poll.

An unchanged image can be omitted while fresh frame metadata is returned,
explicitly referencing the prior image. Use `since` only when the caller already
has that image; omit it when starting a new agent/context.

The service adapts its polling interval within a bounded wait, briefly settles
changed frames, and encodes only the observation it returns. Crop deliberately
and tune `max_dimension`, `encoding` and `quality` instead of capturing a giant
desktop for a small dialog. Timing and encoded-size metadata describe actual
work; these choices do not remove model inference latency.

A short batch in an already-focused blank editor:

```json
{
  "actions": [
    {"kind": "text", "text": "Hello from Desktop-MCP."},
    {"kind": "key", "keys": ["enter"]}
  ],
  "observe": true
}
```

Batch kinds also include `move`, `click`, `drag`, `scroll`, `wait`, `key_down`,
`key_up`, `button_down` and `button_up`. A hold lasts only within that batch and
is always released at its end. Use `keys` as mouse modifiers, for example
`{"kind":"drag","button":"middle","keys":["shift"],"start":[100,100],"loc":[300,200]}`.
Do not enter literal text while batch-held modifier keys are down.

Movement uses a minimum-jerk curve: zero initial/final velocity and acceleration,
without overshooting a click target. Its default duration adapts to distance;
an explicit positive `duration` can slow a demonstration. Text has no corresponding
speed cap.

Do not automatically replay a failed input request. The error identifies how
many complete steps ran; the current step can be partially applied. An error
from the observation after a successful batch explicitly says the input already
completed.

## If your client cannot see MCP images

Successful MCP negotiation does not prove a client forwards image pixels to its
model. `Screenshot(export_image=true)` returns both the normal image block and
an `image_path` that a native image-reading tool can open. Omit `since` when
requesting a full exported image.

For a client needing this regularly, set `DESKTOP_MCP_IMAGE_FILES=true` in its
MCP environment configuration. Full observations then include a temporary file
as well. This costs disk I/O and possibly another tool round trip, so it is a
compatibility path, not the fastest default.

Images remain in memory by default. Explicit exports are private screen content
on disk, retained among the latest 16 exports until server exit. They are never
committed. A local server does not make Copilot/model processing offline; only
show applications whose content you intend to share with your model service.

## Development and documentation

See [CLAUDE.md](CLAUDE.md), [SYSTEM_MEMORY.md](SYSTEM_MEMORY.md),
[DECISIONS.md](DECISIONS.md), [agent-work.md](agent-work.md), and
[TESTING.md](TESTING.md) for the relevant contracts and safe development workflow.

The preserved Windows engine is in `src/windows_mcp`. Supervision, the native
interface, observation service and explicit MCP surface live in `src/desktop_mcp`.
The repository preserves upstream history; `upstream` points to Windows-MCP and
`origin` points to this fork.

MIT terms are in [LICENSE.md](LICENSE.md). Bundled UIAutomation attribution and
Apache 2.0 terms are preserved in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)
and [LICENSE-UIAUTOMATION.txt](LICENSE-UIAUTOMATION.txt).
