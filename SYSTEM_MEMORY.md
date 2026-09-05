# Desktop-MCP system map

## Baseline and ownership

The project retains Windows-MCP at upstream revision
`1ea8690a6d9bfa55abb24d534f6a30590acf47d5`. Its engine lives in
`src/windows_mcp`; MIT attribution remains in `LICENSE.md`. The supervised
implementation is being added under `src/desktop_mcp`.

| Layer | Owner and contract |
|---|---|
| Shared types | `desktop_mcp/contracts.py`; coordinates are physical virtual-desktop pixels unless explicitly identified as image pixels. |
| Controller and input | Parent-owned runtime; serial actions, generation-based revocation, cancellation, fast Unicode input, smooth pointer motion. |
| Control window and overlay | Native Windows UI; local arming, global stop, input takeover, capture exclusion and rounded monochrome cursor. |
| Observation service | Frame identity, image encoding, unchanged-frame detection, adaptive bounded waits and coordinate conversion. |
| Capture/Windows engine | Reuse `windows_mcp.desktop.screenshot` and `windows_mcp.uia`; do not copy the UIA implementation. |
| MCP entry point | Explicit supervised tool registration; stdout is reserved for MCP messages. |

## Cross-layer invariants

- A stopped controller rejects input and capture. Status and Stop remain usable.
  Only local UI can arm. A generation change invalidates queued operations.
- The UI thread never waits on a long-running action lock. A stop sets revocation
  before releasing input; a backend emission rechecks revocation under its
  short input lock so no new key-down can race after the release.
- A screenshot stores image dimensions, capture bounds, context and input
  revision. Image coordinates include both scale and crop origin when mapped.
- A request's shape is validated before emitting input. Failure after partial
  execution reports completed actions and does not replay them.
- Captures exclude our overlays. Images, window titles and typed text are not
  telemetry or committed artifacts.
- Model/client image support is separate from successful MCP negotiation.
  Never claim that text metadata proves vision.

The canonical product choices are in `DECISIONS.md`; implementation status is
updated here when the corresponding entry point and consumers are wired.
