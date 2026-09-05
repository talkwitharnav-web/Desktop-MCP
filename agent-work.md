# Agent and rubber-duck workflow

Read before delegation. [CLAUDE.md](CLAUDE.md) supplies the safety rules;
[DOCUMENTATION.md](DOCUMENTATION.md) supplies the documentation format.

## Model

All new subagents and rubber ducks use **`gpt-6-astra` with
`reasoning_effort: "max"`**. This is the user's current choice, including
review-only agents. A running agent cannot be changed mid-run; launch subsequent
work with the current setting rather than copying an old brief.

## Before dispatch

1. Commit and sync the baseline. Do not hand an agent the only copy of
   uncommitted work.
2. Trace the feature through its producers, consumers, routes and rendered
   controls. Give each editing agent an exclusive file list. The coordinator
   owns shared schemas/helpers and resolves cross-file decisions.
3. Find the serial bottleneck before adding agents. More agents do not
   parallelize a shared file. Respect the runtime's actual concurrency limit.
4. State the task, allowed files, forbidden writes, required invariants and
   validation scope. Read-only agents must not create scratch files in the
   repository or run mutating checks.
5. For large investigations, give each report a unique path in the session
   workspace outside the repository. Do not require every reader to load
   every domain document; name the sections relevant to the assignment.

## While work runs

- Do not edit files or restart services that an agent is currently exercising.
  Collect evidence against a stable revision, then fix it.
- If a finding affects someone else's file, relay it to that owner rather than
  allowing a second writer.
- Announce shared names, payload shape changes and semantic changes to every
  affected owner together. A shared field can change meaning without changing
  its name or failing compilation.
- Avoid speculative duplicate investigations. Independent tasks should own
  distinct questions; a broad defect needs cross-file context, not blind
  per-file certification.
- Do not change or weaken a product rule to accommodate an agent's proposed
  remedy. Check [DECISIONS.md](DECISIONS.md) for deliberate exceptions.
- If a tool refuses access, respect the refusal. Do not retry through another
  tool, source or phrasing to evade it.

## Report format

For each finding provide:

- **Claim and impact:** what breaks, for whom, and under which condition.
- **Evidence:** revision/artifact, file and symbol or quoted line text, and the
  reproduction or concrete call chain.
- **Confidence:** observed, established from source, or requires runtime
  evidence. Keep unsuccessful reproductions and evidence limitations explicit.
- **Remedy:** proposed change and which existing behaviour it must preserve.
- **Checked and fine:** what was actually inspected and ruled out.

Line numbers alone are fragile; pair them with a symbol or quoted text. Counts
and measurements need an artifact, scope and date. Distinguish "the test passed"
from "the fixture could express the failure": an inconclusive run is not a pass.

## Rubber-duck rounds

Give reviewers the diff, its intent, the relevant invariants and the evidence
already collected. Review changed behaviour and important caller interactions,
not a prose claim that a fix works.

Use a clear severity bar: losing work or access, misreporting persisted state,
breaking the requested behaviour, and other demonstrated regressions. Record
cosmetic findings separately. Do not pressure a reviewer to label an unresolved
issue clean for scheduling reasons.

Recheck findings against the current revision before acting. A proposal can be
wrong even when the measurement is right. A coordinator's correction receives
the same scrutiny as the original patch.

Run further review after substantive fixes. Wait for every assigned reviewer
before releasing; a clean verdict applies to the reviewed revision, not later
code. Only report completion when the requested behaviour and relevant
cross-file paths are covered. UI source inspection does not replace a browser
for geometry, hit testing or focus behaviour; see [TESTING.md](TESTING.md).

## Destructive-work boundary

Never mutation-test cleanup or path selection. A mutant of a deletion path
performs the deletion; it does not simulate it. Verification scratch directories
must be explicitly created and owned by the operation, never inferred from
`cwd`. Do not test negative permissions by running the dangerous command.

This guide was adopted from the user's existing workflow. Unrelated project
incident records are not copied here; the destructive-work prohibition remains
binding without requiring those records.

## Updating this guide

Follow [DOCUMENTATION.md](DOCUMENTATION.md). Update the rule that changed, keep a
brief reason or decisive counterexample, and link longer evidence. Do not add
agent-count recaps or a new story for every review round.
