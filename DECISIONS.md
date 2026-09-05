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

Use local stdio, an Alt-Tab-visible control window, and an explicitly selected
GUI tool surface. No remote listener, autorun task, registry editor, arbitrary
shell tool, or auto-arming default is necessary for this project.
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

## D-009: Teaching is guidance, not mouse ownership

Local teaching mode permits observations, laser/ink overlays, cursor-vicinity
waits and transcript updates, but never mouse/keyboard injection or application
launch/focus. The learner's physical movement does not trigger control-mode
takeover pauses. Physical clicks/keys invalidate observations; Ctrl+Shift+H still
stops every agent operation.

Laser/ink are separate visual layers, not movements of the user's pointer and
not edits to the underlying application. Erase removes only our annotations.
The transcript is an independently draggable, Alt-Tab-accessible window with
local pin/dock controls. The model publishes content explicitly through tools;
this is not automatic mirroring of every Copilot terminal token.

Cursor proximity is not proof that a button was clicked or an application action
succeeded. Dwell and context checks prevent accidental advancement, and the model
must inspect the resulting UI when correctness depends on an actual app change.
