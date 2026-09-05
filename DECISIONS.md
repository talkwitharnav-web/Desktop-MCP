# Desktop-MCP product decisions

## D-001: Build on Windows-MCP

Retain the Windows-MCP engine and its MIT notices. Our supervised adapter is a
separate package so upstream capture/UIA improvements can be integrated without
mixing product controls into every low-level COM wrapper. The `desktop-mcp`
entry point is the supported connection; the retained upstream module is not a
second supervised server.

## D-002: Human stop outranks automation

Ctrl+Shift+H and the local Stop button revoke Desktop-MCP access. Start stopped;
re-arming is available only in the local control window, never through MCP.
Plain-text conversation can continue while desktop actions are paused (D-011);
it does not grant input or capture permission.
Commands queued before a stop must not become executable after re-arming.
The control window and hotkey must remain responsive during work. This is not
a sandbox and cannot stop unrelated Copilot shell tools or another MCP server.

## D-003: Visible motion without slow typing

The cursor is a familiar arrow in black/grey, with rounded edges. Movement has
smooth acceleration and deceleration, including moves preceding clicks and
drags. Text input has no artificial per-character throttle. Responsiveness and
cancellation are still required during long input.

## D-004: One physical desktop, one input sequence

Serialize input, validate a batch before its first side effect, and release
owned keys/buttons on every exit path. Prefer atomic chords and drags over
holding a key across model round trips. Do not retry non-idempotent input
automatically after partial success.

## D-005: Efficient observations, not imaginary video streaming

MCP tool responses are request/response observations, not a continuous video
feed into the model. Reuse unchanged image content, adapt polling inside bounded
wait-for-change calls, crop deliberately, and provide actual encoded image
blocks with exact coordinate metadata. Input references a bounded-lifetime frame;
the server, not the model, translates image pixels to desktop pixels.

## D-006: Local, explicit control surface

Use local stdio clients connected to a single per-user/session desktop host,
an Alt-Tab-visible control window, and an explicitly selected GUI tool surface.
A local Windows named pipe has a current-user ACL and rejects remote clients.
No network listener, autorun task, registry editor, arbitrary shell tool, or
auto-arming default is necessary for this project.
Screen contents and typed text must not be logged or committed.

## D-007: GitHub sync is not package publication

Commit and sync to the requested GitHub repository. A manually started read-only
workflow may build artifacts, but automatic publication is disabled. `server.json`
is a release metadata template, not proof that this package name is owned or
published on PyPI or registered with the MCP registry. Run the local checkout
through its virtual environment rather than downloading an unrelated package
with the same name.

## D-008: Owner-controlled personal development

Only the owner's account is granted repository write access; the assistant uses
that same authorized account for requested changes. No Dependabot/Renovate
update configuration, automated dependency PRs, auto-merge, or Actions approval
of pull requests. Actions receive read-only repository access. Dependency changes
are deliberate owner/assistant work, not unattended edits.

Public read access and upstream license notices do not grant anyone write access.
Keep required attribution while preventing automated changes.

## D-009: Guidance and control share one authorization

The user explicitly removed the mutually exclusive Teach/Control modes. One
local Arm enables observations, laser/ink, cursor waits and input. Transcript
chat is available independently of that desktop grant (D-011).
An assistant can explain, highlight, click the next tab and explain again without
a mode switch or re-arming. Stop remains latched and can never be bypassed.

Interruption pauses apply while an automated input sequence is active, not while
the learner is reading or moving during a cursor wait. A bounded `learner_turn`
temporarily reserves the pointer for the user and blocks injected input within
that wait; it is automatic operation state, not a second permission mode.
Physical clicks/keys invalidate observations regardless of interruption preference.

Laser/ink are separate visual layers, not movements of the user's pointer and
not edits to the underlying application. Erase removes only our annotations.
The transcript is an independently draggable, Alt-Tab-accessible window with
local pin/dock controls. Per the user's follow-up, it opens with the main app
and has a main-panel visibility toggle. The model publishes content explicitly through tools;
this is not automatic mirroring of every Copilot terminal token.

Cursor proximity is not proof that a button was clicked or an application action
succeeded. Dwell and context checks prevent accidental advancement, and the model
must inspect the resulting UI when correctness depends on an actual app change.

## D-010: A close button quits; launching is independent of the MCP client

X on either main window quits the whole Desktop-MCP host and closes its client
bridges. Minimize remains the explicit way to keep it running. Ctrl+Shift+H is a
pause/revocation, not Quit. An explicit Quit latches automatic startup off until
the user opens Desktop-MCP again, so a reconnect cannot reverse a close click.

The searchable Start-menu entry opens or reveals the same host. MCP sessions
have distinct protocol connections but share one physical controller and local
permission state. Only one host owns the hotkey; startup is serialized through
Windows mutexes, not a stale PID file. No launcher or reconnect arms the desktop.

## D-011: Two-way conversation is not a desktop permission grant

The local transcript composer sends user messages to a bounded, in-memory queue.
An active MCP agent listens with `TranscriptRead`, answers via `Transcript` with
the received message id, and continues listening while that conversation is active.
No idle-model wakeup or automatic mirroring of CLI output is claimed.

Only one MCP session holds the transcript listener lease. Messages stay pending
until a matching reply; disconnection or lease expiry permits another listener
without losing the question. The UI reports actual listening/delivery/queue state.

Text chat and transcript show/hide work while desktop access is stopped. They
never arm input, capture pixels, launch/focus applications, or permit writing
into the protected composer through desktop-input tools. Ctrl+Shift+H stops
desktop work, not conversation. X still quits the whole application.
