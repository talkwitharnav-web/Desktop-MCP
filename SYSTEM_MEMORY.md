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
| Accessibility worker | `accessibility.py`; inspect one selected foreground window in an owned, cancellable subprocess with a five-second bound. |
| Optional image files | `image_files.py`; bounded exports in a uniquely owned temporary directory, disabled by default. |

## Cross-layer invariants

- A stopped controller rejects input and capture. Status and Stop remain usable.
  Only local UI can arm. A generation change invalidates queued operations.
  Closed is terminal: late UI notifications or input-release failures may retain
  diagnostics, but cannot make the controller available again.
- The UI thread never waits on a long-running action lock. A stop sets revocation
  before releasing input; a backend emission rechecks revocation under its
  short input lock so no new key-down can race after the release.
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
- Captures exclude our overlays. Images, window titles and typed text are not
  telemetry or committed artifacts.
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
- Teaching starts before the control surface makes local arming available.
  Shutdown stops/releases input before closing either UI, and attempts all cleanup
  even if one surface fails. UI-thread snapshots never acquire the operation lock.
- `CaptureContext.scope` distinguishes desktop observations from fullscreen active
  windows. `teaching_context` checks geometry without pixels or an operation lock;
  own foreground windows and unavailable targets have no teaching context.
- Control/Teach is a local-only selector. Teach permits observation/presentation
  but blocks native emission and app launch/focus. Learner motion does not revoke
  the session; clicks/keys invalidate frames and context-sensitive guidance.
- `Transcript` explicitly publishes bounded plain text; it is not a CLI token
  mirror. Front/back requests use no-activate window operations, and local pinning
  wins. The transcript is available through Alt-Tab before the first message.
  Docking and minimum sizes share the current monitor's work-area/DPI constraints;
  panel minimum tracking sizes never apply to the separately sized ink canvas.
- Accessibility is optional and heavier than capture. Its subprocess receives
  one window handle, never an unrestricted background-window traversal. Stop or
  timeout terminates only that owned worker and does not strand the action lock.
- Image exports are explicit, or enabled by `DESKTOP_MCP_IMAGE_FILES`. Only files
  created by that instance are unlinked; directory removal is nonrecursive.
  An unchanged image can keep its previous image content without another export.
- Model/client image support is separate from successful MCP negotiation.
  Never claim that text metadata proves vision.

The canonical product choices are in `DECISIONS.md`; implementation status is
updated here when the corresponding entry point and consumers are wired.
