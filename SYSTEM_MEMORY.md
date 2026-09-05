# Desktop-MCP system map

## Baseline and ownership

The project retains Windows-MCP at upstream revision
`1ea8690a6d9bfa55abb24d534f6a30590acf47d5`. Its engine lives in
`src/windows_mcp`; MIT attribution remains in `LICENSE.md`. The supervised
implementation lives under `src/desktop_mcp`.

| Layer | Owner and contract |
|---|---|
| Shared types | `desktop_mcp/contracts.py`; coordinates are physical virtual-desktop pixels unless explicitly identified as image pixels. |
| Controller and input | `runtime.py`, `actions.py`, `native.py`; serial actions, generation-based revocation, cancellation, fast Unicode input and minimum-jerk pointer motion. |
| Control window and overlay | Native Windows UI; local arming, global stop, input takeover, capture exclusion and rounded monochrome cursor. |
| Observation service | Frame identity, image encoding, unchanged-frame detection, adaptive bounded waits and coordinate conversion. |
| Teaching state/rendering | `teaching.py`, `teaching_render.py`; bounded transcript/ink, context expiry and learner dwell state without OS input. |
| Teaching windows/tools | `teaching_ui.py`, `teaching_tools.py`; native transcript, separate click-through ink/laser layer and six explicitly registered presentation/cursor tools. |
| Capture/Windows engine | Reuse `windows_mcp.desktop.screenshot` and `windows_mcp.uia`; do not copy the UIA implementation. |
| MCP entry point | `app.py`, `tools.py`, `policy.py`, `__main__.py`; explicit supervised registration and dispatch-time request tickets. Stdout is reserved for MCP. |
| Shared host and launcher | `service.py`, `pipe_transport.py`, `stdio_bridge.py`, `launcher.py`; one Windows user/session host, per-client MCP streams, explicit Quit, searchable local launch. |
| Accessibility worker | `accessibility.py`; inspect one selected foreground window in an owned, cancellable subprocess with a five-second bound. |
| Optional image files | `image_files.py`; bounded exports in a uniquely owned temporary directory, disabled by default. |

## Cross-layer invariants

- A stopped controller rejects input and capture. Status and Stop remain usable.
  Only local UI can arm. A generation change invalidates queued operations.
  Closed is terminal: late UI notifications or input-release failures may retain
  diagnostics, but cannot make the controller available again.
- `DesktopApplication.request_exit` revokes input and signals the host without
  joining UI threads from a Windows callback. The host closes every MCP stream
  and then both surfaces. Either main X uses this route; minimize does not.
- `serve` is a stdio bridge, not another GUI host. `service.run_host` owns the
  application once; each MCP connection uses `create_server(...,
  manage_application=False)` so a client cannot reinitialize/close the shared
  native state. A client that used the desktop revokes control on disconnect.
- Per-user/session named pipes have protected current-user/SYSTEM ACLs, reject
  remote clients, and carry bounded UTF-8 JSON messages, never pickled objects.
  Overlapped I/O cancellation is acknowledged before buffers/handles are freed.
  Mutexes serialize host startup and disappear safely on process exit.
- An explicit local Quit is recorded under the current user's Desktop-MCP state
  folder. Existing bridges exit and never reconnect/replay. New automatic clients
  report how to reopen the app; the Start-menu launcher can explicitly start it.
  State stores only lifecycle metadata, not screen contents or tool arguments.
- Host startup requests Windows job breakaway, not just a hidden console. A
  client job that forbids breakaway gets an explicit Start-first instruction;
  it never owns a shared host that disappears when that client exits. Bridges
  monitor the actual pipe-server process independently of stdout backpressure.
- FastMCP's low-level stream adapter is isolated in `service._rpc_stream` and
  follows its stdio lifecycle contract. FastMCP is pinned; transport tests cover
  real simultaneous clients, framing, reconnect and host-driven exit.
- The UI thread never waits on a long-running action lock. A stop sets revocation
  before releasing input; a backend emission rechecks revocation under its
  short input lock so no new key-down can race after the release.
  Key/button ownership begins inside that permitted emission callback immediately
  before the native down attempt. A rejected emission owns nothing; an attempted
  native send still receives cleanup if the backend reports partial failure.
- `SendInput` can wait for the UI thread's low-level hook. Therefore local Stop
  and Arm must not block acquiring the input lock on that UI thread. Contended
  stop cleanup is coalesced onto one release worker; re-arming waits until release
  is complete. This prevents a stop handler and input hook deadlocking each other.
- `ControlPolicy` stamps a ContextVar request ticket before tool offloading.
  Revocation applies to work waiting in the MCP worker queue as well as work
  already waiting for the controller's sequence lock.
- A screenshot stores image dimensions, capture bounds, context and input
  revision. Image coordinates include both scale and crop origin when mapped.
- A request's shape is validated before emitting input. Failure after partial
  execution reports completed actions and does not replay them.
  Zero duration is rejected for every action that moves to a coordinate, not
  only Move/Click/Drag. The shared motion helper applies an 80 ms minimum to
  explicit pointer durations; zero waits and unpaced literal text are unchanged.
- Captures exclude our overlays. Images, window titles and typed text are not
  telemetry or committed artifacts.
  DXCAM uses only the verified 0.3.0 one-shot recovery boundary: access loss fails
  immediately to MSS/Pillow instead of entering its unbounded recovery loop.
  Unknown versions and already-threaded cameras are not used. Failed owned
  one-shot cameras are released before fallback, with the capture guard still held.
  Controller checkpoints before/after capture stages propagate cancellation
  separately from native/COM failures; no timed-out capture thread is abandoned.
- `DesktopApplication` owns both native surfaces. `window_handles` combines every
  control/transcript/canvas/cursor handle for input targeting and capture exclusion.
  Its capture guard enters both acknowledged hide/flush guards with `ExitStack`;
  a failed guard never falls back to an unguarded capture.
  Base-UI requests are also serviced by the native wake-message handler during
  Windows modal move/menu loops, with reentrancy protection. Shutdown sends
  `WM_CANCELMODE` to the owned panel so those loops cannot strand its UI thread.
  Local minimization tracks the last non-owned foreground using the composed HWND
  list. It restores that target only when Windows selects another owned window or
  briefly has no foreground, never over a different user-selected application.
  Base-panel layout scale is capped by available work-area client dimensions,
  separately from actual monitor/cursor DPI. Native STATIC children expose
  rejection/activity text; the takeover button's native name carries its On/Off
  state. These extra HWNDs remain in the combined input-target exclusion list.
  Completed window movement also reflows the panel, covering same-DPI monitors
  with different work areas without resizing or activating it during the drag.
- Teaching starts before the control surface makes local arming available.
  Shutdown stops/releases input before closing either UI, and attempts all cleanup
  even if one surface fails. UI-thread snapshots never acquire the operation lock.
- `CaptureContext.scope` distinguishes desktop observations from fullscreen active
  windows. `teaching_context` checks geometry without pixels or an operation lock;
  own foreground windows and unavailable targets have no teaching context.
- Guidance and input share one locally armed session; there is no mode selector
  or mode field. `input_active` tracks an actual automated input sequence, while
  `awaiting_user` tracks a bounded cursor wait. Both are operation state, not
  permission grants. `Controller._input_activity` spans input validation through
  owned-input release; nested emission reuses that scope. Interruption pauses
  apply only during that activity, not idle instruction reading or learner waits.
  `Controller.learner_turn` requires an active operation and no held input;
  it cannot inject input and clears on success/error/cancellation without rearming.
  Physical clicks/keys always invalidate frames and context-sensitive guidance.
  Teaching point mapping preserves an input-revision ticket through the model's
  initial authorization, rather than taking a new baseline after frame resolution.
- Teaching model commits validate the combined mark/wait canvas before publishing.
  Native sizing uses the renderer's same stroke/glow-aware bounds and allocation
  limits; an oversized transient scene is hidden with a diagnostic, not a UI crash.
  Outline ink keeps its requested hue above a neutral contrast edge; shared bounds
  include the extra stroke and antialiasing margin. Cursor-wait progress uses a
  second status line when space permits. Work-area-clamped clients reduce spacing
  and use compact status copy so readable instructions and the Stop row still fit.
- `Transcript` explicitly publishes bounded plain text; it is not a CLI token
  mirror. Front/back requests use no-activate window operations, and local pinning
  wins. Only the main panel is visible at startup; the transcript appears when
  the first instruction is published and then remains accessible through Alt-Tab.
  Docking and minimum sizes share the current monitor's work-area/DPI constraints;
  panel minimum tracking sizes never apply to the separately sized ink canvas.
  Transcript close/fatal-exit handling sends owned `WM_CANCELMODE`, so native
  menu/move/resize loops cannot strand shutdown.
- Accessibility is optional and heavier than capture. Its subprocess receives
  one window handle, never an unrestricted background-window traversal. Stop or
  timeout terminates only that owned worker and does not strand the action lock.
  Snapshot pins context/input revision across both UIA and image phases. The
  worker validates the supplied ticket before launch and after inspection;
  the tool rejects mismatches before returning a combined tree/image result.
- Image exports are explicit, or enabled by `DESKTOP_MCP_IMAGE_FILES`. Only files
  created by that instance are unlinked; directory removal is nonrecursive.
  An unchanged image can keep its previous image content without another export.
- Model/client image support is separate from successful MCP negotiation.
  Never claim that text metadata proves vision.

The canonical product choices are in `DECISIONS.md`; implementation status is
updated here when the corresponding entry point and consumers are wired.
