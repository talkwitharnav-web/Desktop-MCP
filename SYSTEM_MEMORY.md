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
| Task ownership and feedback | `interaction.py`, `policy.py`; per-session task owner, caller identity, unobserved-action receipts and running-host identity. |
| Control window and overlay | Native Windows UI; local arming, global stop, input takeover, capture exclusion and rounded monochrome cursor. |
| Observation service | Frame identity, image encoding, unchanged-frame detection, adaptive bounded waits and coordinate conversion. |
| Teaching state/rendering | `teaching.py`, `teaching_render.py`; bounded ink, context expiry and learner dwell state without OS input. |
| Conversation state | `conversation.py`; canonical user/assistant history, pending messages, bounded async reads, per-MCP-session delivery/acknowledgement and listener lease. |
| Teaching windows/tools | `teaching_ui.py`, `teaching_tools.py`; native two-way transcript, visibility toggle, separate ink/laser layer and seven presentation/conversation/cursor tools. |
| Chat history | `transcript_chat.py`, `transcript_chat_native.py`, `_transcript_chat_scrollbar.py`; role-separated native message controls, bounded inner/outer scrolling, selection/reading anchors and short arrival motion. |
| Capture/Windows engine | Reuse `windows_mcp.desktop.screenshot` and `windows_mcp.uia`; do not copy the UIA implementation. |
| MCP entry point | `app.py`, `tools.py`, `policy.py`, `__main__.py`; explicit supervised registration and dispatch-time request tickets. Stdout is reserved for MCP. |
| Agent operating instructions | Packaged `AGENT_GUIDE.md`, read by `app.read_agent_guide` for initialization and `desktop-mcp://guide`; no client filesystem dependency. |
| Shared host and launcher | `service.py`, `pipe_transport.py`, `stdio_bridge.py`, `launcher.py`; one Windows user/session host, per-client MCP streams, explicit Quit, searchable local launch. |
| Accessibility worker | `accessibility.py`; inspect one selected foreground window in an owned, cancellable subprocess with a five-second bound. |
| Optional image files | `image_files.py`; bounded exports in a uniquely owned temporary directory, disabled by default. |

## Cross-layer invariants

- A stopped controller rejects input, capture and annotations. Status, Stop and
  plain-text conversation/visibility remain usable without granting desktop access.
  Only local UI can arm. A generation change invalidates queued operations.
  Closed is terminal: late UI notifications or input-release failures may retain
  diagnostics, but cannot make the controller available again.
- `DesktopApplication.request_exit` revokes input and signals the host without
  joining UI threads from a Windows callback. The host closes every MCP stream
  and then both surfaces. Either main X uses this route; minimize does not.
- `serve` is a stdio bridge, not another GUI host. `service.run_host` owns the
  application once; each MCP connection uses `create_server(...,
  manage_application=False)` so a client cannot reinitialize/close the shared
  native state. The interactive task owner's disconnect revokes control before
  waiting for shielded synchronous tool workers to finish.
  Chat-only connections do not revoke another client's desktop work; their
  registered conversation session ids are released from listener ownership.
- Desktop ownership is task-wide, not merely a per-call mutex. Policy claims
  the initialized MCP session under the same generation as the request ticket.
  Local revocation invalidates ownership; only the current owner can release it.
  Service disconnect cleanup uses registered owner ids, not a pre-validation
  `tools/call` flag, so a denied helper cannot stop the coordinator.
- `Interaction` tracks delivered actions separately from returned observations;
  neither means an application outcome was verified. Automatic image references
  are scoped to the same client and generation and recorded only after successful
  response preparation. A long transcript read yields for pending observation,
  including when an action finishes after the read started.
- Pending user corrections prevent another changing desktop call until replied
  to, while screenshots and chat remain usable. The rule does not disarm access.
- Status identifies the running version, PID, instance and a fingerprint of the
  installed package files at startup, not an inferred Git revision.
- `diagnostics.py` carries only validated frame identifiers and input-delivery
  receipts in a request-local context. The MCP policy recognizes the actual
  protected-target exception through batch/tool wrappers, never an arbitrary
  exception's `details` attribute. The response uses MCP `isError` and structured
  `is_error`, with caller/generation, native geometry and completion warnings;
  `Interaction.last_denial` retains the same attributed record. Native/control
  text and unsent drafts are not diagnostic fields. Status and observations also
  expose content-free `protected_windows` snapshots after capture guards restore
  the local surfaces; these snapshots are not historical replay authorizations.
  Image responses summarize known hidden per-message chat descendants with
  `hidden_chat_controls_omitted`, retaining roots, visible controls and uncertain
  records. Full status and actual target-denial diagnostics remain unfiltered;
  presentation compaction never changes native protection or HWND registration.
- After successful focus, only a temporary no-foreground condition is retried
  for at most 0.5 seconds. A different selected window aborts recovery; focus,
  launch and input are never replayed to obtain an image.
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
  `vision._tile_fingerprint` adds at most 16x16 tile digests to a frame, not cached
  screenshots. Only the final sample is spatially hashed; unchanged references
  reuse those hashes. The full-resolution digest remains the equality authority.
  `spatial_change` compares compatible final/reference images and reports
  approximate changed-tile bounds plus a padded inspection crop in physical,
  half-open LTRB coordinates. It never identifies controls, intermediate events
  or successful application outcomes; incompatible references carry a reason.
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
  `window_roles` combines their stable, content-free role mappings for
  `WindowsInput` / `WindowTargets`. Target protection follows the actual hit
  receiver, process/root ownership, active/focused controls and owned modal/capture
  routing rather than any intersecting rectangle. Layered click-through overlays
  use a bounded Z-order/hit-test query; ambiguity is a diagnostic denial.
  Active capture uses the HWND returned by `ensure_observable_foreground` before
  reading title/geometry, not a second unchecked foreground sample. Desktop-scope
  captures omit titles of registered owned foreground windows.
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
  `Mark.laser_bounds` retains exact physical ellipse geometry while the compatible
  path remains available. `teaching_render._laser_path` caches only bounded
  arc-length geometry, never visibility/authorization. Closed paths orbit across
  a continuous seam for the existing lifetime; open paths sweep once and rest.
  Laser-only scenes supersample local patches instead of the whole canvas.
  Callers explicitly close each returned RGBA image after use. Animation is
  elapsed-time based; native scheduling must drop missed frames, not replay a
  backlog or freeze the clock that also determines expiry.
  Outline ink keeps its requested hue above a neutral contrast edge; shared bounds
  include the extra stroke and antialiasing margin. Cursor-wait progress uses a
  second status line when space permits. Work-area-clamped clients reduce spacing
  and use compact status copy so readable instructions and the Stop row still fit.
- `Transcript` explicitly publishes bounded plain text; it is not a CLI token
  mirror. `TranscriptRead` waits at most 30 seconds for a user message without
  holding the desktop sequence lock. Replies include the received message id;
  the message remains pending until the listener acknowledges it with a reply.
  Listener ownership is session-scoped, expires after 120 inactive seconds, and
  is released when that MCP connection ends. Nothing launches an AI model.
- Both main and transcript windows open at startup. The main visibility toggle
  posts asynchronously to the transcript UI thread so it cannot block the stop
  hotkey loop. Temporary capture hiding preserves the user's on/off intent.
  Front/back requests use no-activate operations, and local pinning wins.
  The composer and Send button are included in own-window input protection.
  A real user click can activate the chat window normally so typing reaches
  its composer; programmatic show/front updates still never steal keyboard focus.
  Enter sends once, Shift+Enter inserts a line, and an active IME composition
  receives Enter instead of accidentally sending an incomplete message.
  Layout fits history/status/composer/Send/Stop using a compact font scale when
  necessary, separately from physical monitor/cursor DPI.
  `transcript_layout.py` shares responsive physical geometry across native sizing,
  minimum tracking and tests. The default compact ribbon places history beside
  the composer; narrow clients wrap/stack rather than overflowing fixed minima.
  `FONT_SIZES` contains exactly 12/14/16 DIP, with Medium14 as the host default.
  `_text_size_key` handles Ctrl+plus/equals/minus and keypad variants only within
  focused transcript controls, preserving IME and suppressing duplicate events.
  Logical text size remains separate from monitor/layout scale and persists
  across local reflow/show operations, not as an OS-wide zoom setting.
  Expand/Compact retains per-mode sizes and UTF-16 selection/reading anchors.
  Incoming replies follow only an already-following history view; Latest exposes
  unread replies without forcibly scrolling a reader. Layout changes do not
  restore composer selection/scroll while IME composition is active.
  Taskbar-edge placement is an explicit full-monitor choice, not an appbar,
  implicit Pin, work-area reservation or taskbar-setting change. The cheap
  `layout_status` snapshot describes the last completed layout without any text.
  Native sizing supplies the prospective width during synchronous
  `WM_GETMINMAXINFO` callbacks; the old narrow-window minimum must not push a
  newly widened ribbon outside its work area. Native fakes model pywin32's
  six-field scroll-info result, and maximized-state queries use the actual
  `user32.IsZoomed` export rather than an invented pywin32 method.
  History control 301 is `NativeChatHistory`, not an EDIT compatibility shim.
  It owns labelled assistant/user bubbles containing real read-only native text;
  per-message selection stores UTF-16 anchor and active endpoints, not merely
  sorted selection bounds. Unchanged selections must not be reset on arrival.
  Long text stays complete in bounded inner viewports rather than huge HWNDs.
  Outer history scroll uses pixels; composer/message scroll uses native lines.
  Their owned slim bars share `transcript_scroll.py` range/track math.
  Native text range comes from actual EDIT
  line/formatting APIs and GDI text metrics; `GetScrollInfo` is invalid without
  a native scroll style. Per-owned-EDIT comctl32 subclasses retain native text,
  IME and accessibility handling while synchronizing wheel/navigation changes.
  The history borrows its font: supply a replacement before deleting the old
  HFONT and close history before releasing the final font. Failed partial
  message replacement closes/clears the owned history instead of leaving stale
  layout entries or HWND roles. All dynamic message/bar handles stay protected.
  Thumb capture is cancelled on up, cancel, hide, reflow, deactivation and
  destruction; scrollbars remain in content-free role/target protection.
  Inner and outer thumb holds suspend following even at the bottom. Page-wheel
  spillover converts its remaining fraction to an outer page, not one text line.
  Thumb drags retain the grab's exact position and pointer coordinate; mapping
  back through a rounded thumb must not shift a stationary reader.
  Brief arrival motion applies only to genuinely new, visible, following
  messages; reduced motion, reading, selection, reflow and hiding cancel it.
  Text is never withheld for animation and timer frames do not rebuild history.
  Automatic arrival clocks begin after native message preparation, not before
  layout work that could consume the entire transition. Tick the chat with a
  fresh clock after other UI work; explicit renderer-test clocks stay supported.
  Reflow batches child positioning without copied pixels or intermediate paints,
  suppresses redraw only on the eligible composer (never the history host,
  root visibility or active IME),
  and finishes with background erasure and a full root/children repaint.
  Send reads a bounded complete `WM_GETTEXT` buffer; window-caption helpers can
  truncate long messages and must not serve as the draft reader.
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
