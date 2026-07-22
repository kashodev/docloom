<!--
Fill in every section that applies and delete the ones that do not. A section
left as an unedited prompt is worse than a deleted one — it reads as answered
when it isn't.

Keep it honest: if something is untested, partially done, or a known trade-off,
say so here rather than letting a reviewer discover it.
-->

## What

<!-- A high-level summary of the change and its scope. What does this PR do, and
     just as importantly, what is deliberately *not* in it? -->

## Why

<!-- The problem being solved and why this solution. Link the ticket/issue.
     If this is a fix, describe the failure it addresses — ideally the symptom a
     user or a test would see. If a previous approach was replaced, say what was
     wrong with it. -->

Closes #

## How

<!-- High-level implementation notes: the shape of the change, key design
     decisions, and the trade-offs behind them.

     - Alternatives considered, and why they were rejected.
     - Anything that constrains the design (a protocol, determinism, the exactness
       of the golden data, a licence).
     - Follow-ups deliberately left out of scope (link the TODO entry). -->

## Testing

<!-- How the change was verified. Be specific enough that a reviewer could repeat it.

     - New/changed automated tests and what property each pins down.
     - Full-suite result (e.g. `NNN passed, N skipped`).
     - Manual verification steps, with the commands used.
     - Anything gated/skipped in CI (emulators, Chromium, cloud creds) and how to
       run it locally.
     - What is *not* covered, and why. -->

- [ ] Full test suite passes locally
- [ ] New behaviour has tests that fail without the change

## Visuals

<!-- Rendering or UI changes: before/after images. For generated documents,
     attach or reference the sample files that demonstrate the change.
     For complex features, a diagram of the flow or data model.
     Delete this section if the change has no visible output. -->

| Before | After |
| --- | --- |
|  |  |

## Reviewer focus

<!-- Direct attention where it is most needed.

     - The files/functions with the real complexity, and what to check about them.
     - Assumptions worth challenging.
     - What can be skimmed (mechanical renames, generated files, moved code with
       no behaviour change).
     - Any part you are unsure about and want a second opinion on. -->

## Risk & rollout

<!-- Delete if not applicable.

     - Backwards compatibility: schema/protocol/config changes, migrations.
     - Effect on existing runs or already-generated data.
     - New dependencies and their licences.
     - How to roll back if this goes wrong. -->
