# Desktop-MCP

Visible, supervised Windows desktop control, built on
[Windows-MCP](https://github.com/CursorTouch/Windows-MCP).

An MCP client supplies the model and decides what to do. Desktop-MCP supplies the
real mouse, keyboard, screenshots, and a local control window. It is not another
AI model, a remote-desktop service, or a sandbox.

## Instructions for the connected model

[AGENT_GUIDE.md](src/desktop_mcp/AGENT_GUIDE.md) is the canonical operating
explanation for agents. The server includes its contents directly in MCP
initialization instructions and also serves it as `desktop-mcp://guide`.
The guide ships inside the package; clients do not need access to this repository.

Loading an MCP server does not automatically load its repository's Markdown
files. This connection-level guide covers outcome-first planning, the normal
observe/act/adjust workflow, scrolling to reveal clipped or incomplete content,
combined teaching/control, transcript listening and replies, visibility and local
authorization. You can ask for the desktop task normally rather than
having to restate its tool-call procedure. The client decides how it presents
MCP instructions to the model; documentation is not a guarantee of model compliance.

After installing an application update, reopen Desktop-MCP and reconnect
existing MCP clients. For guide-only edits in the active installation,
reconnecting is enough: each new server connection reads the packaged guide.
An already connected client can also reread `desktop-mcp://guide` if it supports
resources. There is no separate hardcoded copy to keep synchronized.

## What changes from stock Windows-MCP

- During automated input, a rounded black/grey arrow overlay follows real pointer movement. Pointer
  moves, including movement before clicks, accelerate and decelerate smoothly.
- Left, right, middle and extra mouse buttons; modifier-aware drags; native
  horizontal/vertical wheel input; named keys, chords, repeats and batch-scoped
  key/button holds.
- Fast Unicode typing without an artificial per-character speed limit or
  clipboard replacement. Long input stays cancellable.
- An Alt-Tab-accessible control window and a global **Ctrl+Shift+H** stop.
  Control starts stopped and can only be allowed/resumed locally.
- One shared local application for multiple Copilot sessions. Closing either
  main window with **X quits the application**, not just its taskbar window.
- Short serial input batches with a single final observation, instead of a
  model round trip for every key.
- Cropped, resized, efficiently encoded observations with frame IDs and
  server-side coordinate conversion. Bounded adaptive waits detect changes
  without continuously sending redundant screenshots.
- Lightweight local image comparisons can point out an approximate changed
  area and suggest a closer crop for small details. This adds no local AI model
  and does not pretend to identify controls or understand application outcomes.
- Optional local image files for clients whose native image reader works but
  whose MCP image-result forwarding does not.
- Teaching and control in **one armed session**: explain, circle a control,
  click the next tab, and keep explaining without changing modes.
- Smooth local laser sweeps and continuously orbiting circles, with bounded
  lifetimes, eased fades and no movement of the user's real pointer.
- A short, wide, Alt-Tab-accessible transcript ribbon with optional expanded
  history, local pinning, top/bottom docking and an explicit taskbar-edge choice.
  Type questions into its message box and receive agent replies in the same
  window. The main panel has a **Transcript: On/Off** toggle.

## Requirements and installation

Use an interactive **Windows 10/11 x64 (Intel/AMD)** desktop. The package requires
Python **3.14+**; the setup script provisions the tested, standard Python 3.14
runtime for you. Internet access and a writable local installation folder are
needed for the first setup.

### One-step setup

Extract the complete source ZIP or clone this repository, then double-click
**`Setup.cmd`**. It prepares this folder's `.venv`, installs the **Desktop-MCP**
Start-menu shortcut, and registers the supervised server with Copilot CLI.
Python and uv do not need to be installed beforehand.

Keep the installation folder in place. Use a fresh source download on another
laptop rather than copying an existing `.venv`. Paths are resolved from the
script's location; the shortcut and client receive that laptop's computed
absolute paths so launching works from any working directory.

Setup does not install Copilot, sign in, start the desktop application, or Arm
control. Open **Desktop-MCP** from Start, then use a new Copilot session or
reconnect it in `/mcp`. Only you choose **Arm / Resume** locally.

The installer preserves other MCP servers, environment settings, tool
restrictions and valid longer timeouts. Conflicting installations or invalid
configuration stop setup rather than being reset. Quit Desktop-MCP normally
before updating an existing installation; setup never force-closes it.

For a read-only plan or to omit an integration:

```powershell
.\Setup.cmd -WhatIf
.\Setup.cmd -SkipCopilot
.\Setup.cmd -SkipShortcut
```

`-CopilotConfig` selects another configuration file. Managed downloads and
Python/cache files stay under the ignored `.desktop-mcp-setup-cache` folder.
Setup does not add global PATH entries, services, startup tasks or compilers.
Arbitrary linked/junction installation trees are refused; uv's own managed
Python alias is accepted only when its plain target is validated inside this
installation's cache. Configuration recovery behavior is documented in
[SECURITY.md](SECURITY.md).

### Manual development setup

In PowerShell, inside this checkout:

```powershell
# If uv is missing:
python -m pip install --user uv

python -m uv sync --frozen --extra dev
.\.venv\Scripts\desktop-mcp.exe install-shortcut
```

UV can install the required Python interpreter into its managed environment.
Dependencies remain in this project's `.venv`.

This project is local-first. Do **not** assume `uvx desktop-mcp` installs this
fork: PyPI and MCP registry publication are not part of setting up the checkout.
`server.json` is release metadata, not a publication receipt.

## Open it from Windows Start

Press the Windows key and search for **Desktop-MCP**. The per-user shortcut
opens the interface without a console, or reveals the existing instance.
It does not arm desktop access.

`install-shortcut` above is a one-time setup step; a Git checkout or a Python
console command does not automatically appear in Windows Start search.
It creates only the Desktop-MCP shortcut and its application icon.

From the project folder you can also run:

```powershell
.\.venv\Scripts\desktop-mcp.exe open
```

Opening the interface before Copilot is fine. Starting more Copilot sessions
connects them to the same application rather than spawning competing windows
and global-hotkey registrations.

## Connect to Copilot CLI

From the project folder:

```powershell
copilot.cmd mcp add desktop-mcp --timeout 45000 -- "$((Get-Location).Path)\.venv\Scripts\python.exe" -m desktop_mcp serve
```

For an npm-installed Copilot on Windows, use `copilot.cmd`: the PowerShell shim
can consume `--` and then misinterpret Python's `-m`. A native executable
installation can use `copilot` with the same arguments.

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
      "timeout": 45000,
      "tools": ["*"]
    }
  }
}
```

Use the virtual environment's absolute Python path so the connection does not
depend on the client's working directory or PATH. `serve` is now a small stdio
bridge. A single shared host owns the native windows and Ctrl+Shift+H; several
Copilot sessions can connect without competing for that hotkey.

If loading fails, open **Desktop-MCP** from Start, then use Copilot's `/mcp`
panel to reconnect the server (disable/enable it if needed). A session created
before the configuration was installed may need a fresh Copilot session.

```powershell
.\.venv\Scripts\desktop-mcp.exe doctor
```

`doctor` reports whether the shared host is running and its latest startup or
explicit-quit state; it never starts or arms desktop control. The first cold
startup can take longer than subsequent connections, so the example allows 45
seconds. Do not respond to a loading problem by disabling the local stop gate.
Some MCP clients deliberately put their tools in a Windows job that forbids
independent child applications. In that case, open Desktop-MCP from **Start
first**, then connect Copilot. The bridge reports this explicitly rather than
starting a shared host that would be killed when the first client exits.

Clients still use STDIO. The bridge uses a Windows named pipe restricted to the
current account and interactive session; remote pipe clients are rejected.
No TCP listener, firewall rule, administrator elevation, or login startup task
is required. All connected clients share the same local Arm/Stop state;
connect only clients you intend to give desktop access.

## Start, stop and take over

The application and transcript open together. Desktop access starts **stopped**.
Text conversation and the transcript toggle work even while desktop control is
paused. Press **Arm / Resume** locally when you want screenshots or desktop actions.
That single authorization enables guidance, observations and desktop input
together; there is no Control/Teach selector. The panel minimizes so it does not intercept input;
it remains reachable through Alt-Tab. If Windows activates the transcript instead
of the target app during local minimization, the panel returns to the last
non-Desktop-MCP window. It does not override a different app selected by the user.
The panel compacts to fit the current monitor's work area without changing the
physical pointer scale. Native accessibility text exposes takeover On/Off,
arm-rejection details and current activity, rather than relying only on painted text.

**Ctrl+Shift+H** and the panel's Stop control revoke input, captures and annotations.
Text conversation stays available so you can ask questions or explain a problem. Pending
commands from the old generation stay cancelled even after you resume. Keys and
buttons held by Desktop-MCP are released. The model has no `Arm` or `Resume` tool.
**X on either the control window or the instruction window quits Desktop-MCP**:
input is revoked, both windows/overlays and the hotkey are released, and the
application and connected bridges exit. Use the ordinary minimize button if you
want it to remain running.

After an explicit Quit, automatic MCP reconnects do not reopen the app behind
your back. Open **Desktop-MCP** from Start, then reconnect it in `/mcp`. The new
instance starts stopped. Closing a Copilot terminal alone leaves the desktop
application available; disconnecting the interactive task owner revokes access
until local re-arming. A rejected non-owner request or a chat-only client leaving
does not stop the owner's task.

**Pause on interruption** stops an active automated input sequence when you use
the mouse or keyboard. Moving while reading instructions or while the assistant
waits for your cursor does not require another Arm click. The local checkbox can
disable interruption pauses; the emergency hotkey always remains enabled.
Physical clicks/keys still invalidate prior observations.

The boundary is **this server**. The hotkey does not terminate Copilot, revoke
its shell tools, stop another MCP server, undo completed actions, or erase
information already delivered to a model. Do not work around a stop using other
tools. Normal Windows integrity restrictions still apply; input to an elevated
or locked desktop may be refused.

## Tool surface

| Tool | Purpose |
|---|---|
| `DesktopStatus` | State, running-host identity, task owner, feedback and transcript status. |
| `DesktopStop` | Latch a stop; never resumes control. |
| `DesktopControl` | Claim/release a multi-step interactive task; never grants local Arm. |
| `DesktopBatch` | Validate and run a short ordered sequence; observe once afterward. |
| `Screenshot` | Fast visual observation, adaptive waiting, encoding and frame references. |
| `Click`, `Move`, `Scroll` | Smooth pointer movement, any supported button, drags and wheel gestures. |
| `Keyboard`, `Shortcut`, `Type` | Keys/chords/repeats and fast literal text. |
| `Wait` | A cancellable delay with optional observation. |
| `App` | List/focus windows or explicitly launch an executable without a shell. |
| `DisplayInventory` | Physical monitor bounds, DPI and scale. |
| `Snapshot` | Optional heavier Windows accessibility inspection plus an image. |
| `Transcript` | Publish/reply, show/hide, or request front/back stacking without taking focus. |
| `TranscriptRead` | Listen for your next typed message and acknowledge it through a reply. |
| `Laser` | Point, trace a path, or circle a region without moving the real pointer. |
| `Draw`, `Erase` | Persistent context-bound ink; erase only our annotations, never app content. |
| `Cursor`, `WaitForCursor` | Observe the real pointer and wait for vicinity plus continuous dwell. |

Upstream PowerShell, registry, filesystem, process-killing and network-scraping
tools are deliberately not registered. The retained `python -m windows_mcp`
module is the upstream implementation, **not** an alternative supervised
connection. Both installed console aliases, `desktop-mcp` and `windows-mcp`,
launch the supervised entry point.

### About an independent agent cursor

The current backend controls the shared Windows desktop. Its monochrome arrow
identifies automated movement; ordinary clicks still use the system pointer.
Laser/ink can point independently without moving that pointer.

A genuine independent input workspace is possible with an executor inside a
separate interactive session/VM or dedicated machine and a passive display viewer.
That is a different boundary from painting a second cursor on this desktop.
Browser-only GUI-event adapters can help a dedicated browser workspace but do
not cover native dialogs or arbitrary Blender interaction. No virtualization,
drivers, remote accounts or other machine settings are installed implicitly.
`DesktopStatus.host.workspace` states the active backend's capabilities.

## Explain and act in the same session

Arm once. The assistant can publish instructions, highlight a button, operate it,
and publish the next explanation using ordinary tool calls. It can also wait for
you to try a step before continuing. No mode switches or extra authorization are
needed unless you stop or interrupt an active automated input sequence.

One interactive MCP session owns the multi-step task. The agent can explicitly
claim it with `DesktopControl(action="claim", task="short label")`; first desktop
use also claims automatically. Research helpers must return facts to that owner,
not interleave edits in the same application. Releasing ownership stops desktop
access, and local Stop/Resume permits a new owner.

Unanswered transcript corrections block new desktop-changing calls until the
agent reads and acknowledges them. Observations and chat remain available, so
it can inspect the current state and answer before continuing. This does not
disarm the session or turn a check-in into a request to abandon the task.

The transcript opens as a **1120 × 184 logical-pixel outer ribbon**, including
window chrome, where the monitor permits. Its compact view places recent history
beside the composer and Send button. Assistant and user messages have separate,
rounded, role-labelled boxes with contrasting slate backgrounds and alignment.
Readable Segoe UI Variable Text is preferred when available, with Segoe UI as
the native fallback.
Narrow displays stack/wrap the controls and increase height to remain readable,
with geometry always bounded by the selected monitor.
**Expand** provides a preferred 440-DIP-tall history view; **Compact** returns to
the ribbon. Both retain your draft, selection, reading position and per-mode
window size during the session. **Latest** returns to the newest message and
marks unread replies when you are deliberately reading older history.

While the transcript is focused, **Ctrl++ / Ctrl+=** increases text size and
**Ctrl+-** decreases it; keypad plus/minus work too. There are exactly three
sizes: **Small (12 DIP), Medium (14 DIP, default), and Large (16 DIP)**, scaled
normally for the display. The choice stays with the open window through docking,
resizing, expansion and hiding; these shortcuts do not change other apps' zoom.

New visible messages use a short eased arrival transition only when you are
already following the conversation. Text is available immediately, not slowly
typed out. Reading or selecting an older message does not move your view, and
the Windows reduced-motion preference is respected.

The history, composer and long-message text use slim dark scrollbars: an
**8-DIP gutter** with a roughly 6-DIP thumb, rather than stock white scrollbars.
Drag the thumb, click the track, use the wheel, or Tab to a bar and use arrows,
Page Up/Down, Home/End. Long messages keep their complete native text in a bounded
inner scrolling area. Selection and Ctrl+A/C work within each message;
Ctrl+Page Up/Down moves between messages. Reflow
batches child positioning and completes background/control repainting after
font and geometry changes, avoiding leftover borders or text during resizing.

The message box stays separate from the read-only history: **Enter sends**,
**Shift+Enter adds a line**, and **Send** also works with input methods such as IME.
Drag the title bar or use **Top**/**Bottom** to dock inside the work area with an
8-DIP inset. **Taskbar edge** explicitly places it flush with the full monitor
bottom; use **Pin first** if the taskbar would otherwise cover it. This does not
reserve taskbar space, change taskbar settings or automatically enable pinning.
A model `Transcript(action="back")` request cannot override a local pin.
Use **Transcript: On/Off** in the main panel to show or hide it without deleting
messages or changing desktop permissions. The model can use
`Transcript(action="show")` or `"hide"` directly; it does not need to Alt+Tab and
click protected application controls. `DesktopStatus.transcript` reports
`enabled`, actual `visible`, pending-message count and listener status.
Its `layout` record reports `compact`, `dock`, physical outer `bounds`, actual
`dpi`, `text_size`, logical `font_dip`, actual `font_face`, physical `font_height`,
`scrollbar_width` and `split` once laid out. Bounds describe the last completed
layout, not a live drag sample.

Closing either main window quits the application. Minimize the instruction window
instead when you just want it out of the way.

### Have a conversation here

Start a task in Copilot with a prompt such as:

> Use Desktop-MCP to teach me Blender. Put explanations and replies in the
> transcript, and keep using TranscriptRead to listen for my questions until
> I say we're done.

Then write in the transcript and press Enter or Send. The active agent receives
the message through `TranscriptRead`, answers with `Transcript(reply_to=...)`,
and listens again. You do not need to return to the terminal for each question.
Only one MCP session listens at a time, so unrelated Copilot tabs cannot both
consume the same question. It can release its listener explicitly; disconnection
releases it automatically. A silent listener lease expires after two minutes.

The status line distinguishes **Agent listening**, **Awaiting reply** and
**Queued**. A queued message is retained until answered, not silently discarded.
The app does not contain its own AI and cannot wake a completely idle/disconnected
Copilot model. Keep the Copilot task active (Autopilot can do that); if nobody is
listening, ask the intended Copilot session to use the transcript.

Chat is bounded and held in memory: up to 32 displayed entries and 32 unanswered
messages, with 16,000 characters per message. A full queue reports an error and
keeps your draft. Closing Desktop-MCP clears its local chat; the connected
Copilot client may retain its normal session history.
Send reads the complete native message buffer, not a truncated window-caption
helper, so long accepted drafts are not silently cut to a short prefix.

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

The combined ink/laser/wait canvas is limited to 8,192 pixels per side and
16,777,216 pixels total. Oversized combinations are rejected before publication;
erase older marks before guiding across widely separated monitors.

`WaitForCursor` automatically gives you a turn. Its `radius` is physical pixels;
`dwell` requires continuously staying nearby. It returns `reached`, `timeout`,
`context_changed` or `input_changed`; being nearby is **not proof of a click or
successful app action**. A stop cancels the operation rather than returning a
false success. During that bounded wait, automated input cannot take your pointer
away; afterward input tools are available again if access is still armed.
Marks disappear when their context becomes stale or control stops.

The transcript is not automatic mirroring of every Copilot CLI token. The model
uses `TranscriptRead` and `Transcript` to receive and answer messages. Ink, laser, cursor and control
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
Teaching tools carry the input-revision ticket through coordinate mapping and
annotation/wait authorization; a learner click cannot silently refresh an old frame.

Coordinate-bound batches guard the observed foreground window. If an action
opens a new dialog or switches applications, use the returned fresh observation
before deciding the next coordinate-based action.

`Snapshot` binds its accessibility tree and image to the same context and input
revision. A switch or input change during the compound inspection is an error,
not a tree from one window paired with another window's image.

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

Keep observation enabled for meaningful actions. The result contains its
post-action frame in the same round trip, with a brief change wait when a suitable
previous image exists. If the agent deliberately skips the picture, status and
the result say that observation is due; a long `TranscriptRead` yields rather
than hiding the unfinished visual check behind another 25-second wait.
The same signal can interrupt a read that was already waiting.

`wait_for_change` is explicitly 0..5 seconds and `settle` is 0..1 in the tool
schema. Compact text is returned alongside one complete structured metadata
record and the image. Use `detail="full"` on Screenshot/DesktopBatch if a client
needs the complete metadata repeated as text.

Input delivery is never a saved/finished-task claim. Check the application's
actual postcondition: a dialog closed, text editing is active, the intended
wording appears, or saving really completed. `Saving...`, a Find/Replace match
count or whole-image settlement is not proof of persistence.

Automatic capture prefers the verified one-shot DXCAM path, then MSS/Pillow.
Display access loss falls back instead of retrying DXCAM recovery indefinitely.
An unverified DXCAM version is skipped until its recovery behavior is checked;
normal native capture calls still depend on Windows returning promptly.

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
Requests below 80 ms use an 80 ms pointer-motion minimum so approaches and drags
still contain visible acceleration/deceleration steps. Any action that moves to
`loc` requires a positive explicit duration; zero-length waits remain valid.

Do not automatically replay a failed input request. The error identifies how
many complete steps ran; the current step can be partially applied. An error
from the observation after a successful batch explicitly says the input already
completed.

### When a target is refused

Screenshots hide Desktop-MCP's interfaces, so an unobstructed image does not prove
that a transcript or permission control is absent. Target errors set MCP `isError`
and structured `is_error=true`. Their `denial` record contains:

- `code`: `protected_target`, `foreground_mismatch`, or `target_indeterminate`.
- `target_point`: the resolved physical point, plus `expected_window` and
  `actual_foreground`; `matched` identifies the role, `window_id`, `root_id`,
  bounds and visibility of the matched surface when known.
- `routing`: whether foreground/focus, pointer hit, mouse capture or a modal menu
  caused the rejection; `request` and validated `frame_ids` identify the caller.
- `input`: whether nothing was delivered, input was partial, or input completed
  before its observation failed. Never replay a completed sequence blindly.

`DesktopStatus.interaction.last_denial` retains the attributed record.
`DesktopStatus.protected_windows` and `observation.protected_windows` expose
content-free window geometry without drafts or control text. `effective_visible`
accounts for root visibility/minimization, not occlusion. `capture_excluded`
reports native display affinity, or `null` when unavailable; acknowledged capture
guards additionally hide our surfaces. Neither field grants permission to click.
The guard uses actual input routing rather than rejecting every overlapping
owned rectangle. Unresolved or changing routing fails explicitly, not optimistically.

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

The [September 5 manual-desktop trial report](DESKTOP-MCP-TEST-FINDINGS-2026-09-05.md)
is preserved unchanged. It records an unfinished application task, not a
controlled benchmark or a verified identification of the old running host.
The remediation strengthens the current implementation and operating guide;
it does not establish which protected window caused that historical denial,
prove an old document was saved, or complete the abandoned presentation.
Use the running `DesktopStatus.host` version, instance and package fingerprint
when reporting a new incident. The fingerprint describes package files at host
startup, not a Git commit or a promise that an older host reloaded new code.

MIT terms are in [LICENSE.md](LICENSE.md). Bundled UIAutomation attribution and
Apache 2.0 terms are preserved in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)
and [LICENSE-UIAUTOMATION.txt](LICENSE-UIAUTOMATION.txt).
