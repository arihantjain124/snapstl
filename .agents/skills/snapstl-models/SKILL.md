---
name: snapstl-models
description: Design or revise printable parametric CAD models for SnapSTL from a plain-language brief, export STL previews, and iterate with the user in the Models view.
---

# Build SnapSTL models

Read the repository's [AGENTS.md](../../../AGENTS.md) and [Models plan](../../../docs/models-workspace-plan.md).
For the organizer, also read the [measurement survey and pipeline](../../../docs/parametric-model-pipeline.md)
and the [scaling rules](../../../docs/scaling-rules-v1.1-handoff.md). For any existing design, read its
manifest, CAD and design notes before proposing a change.

## Agree on the design before building

On the first turn of a new design, ask the user a short set of focused clarifying questions
**before writing or generating CAD**. Translate their description into a proposed shape and use.
Use their answers and any authorized references to resolve the design choices. Prioritize questions
that materially affect what will be built:

- What does it hold, mate with or attach to? Ask for the critical measurements or a photo with scale
  if fit depends on an existing object.
- Which dimensions, appearance details or interfaces matter most? For multipart designs, ask how
  the parts should fit and assemble, such as glue-in, removable slip fit or fasteners.
- Which values should be adjustable in Models? Ask about acceptable sizes, print faces, material
  or printer constraints when they affect geometry. For public release, establish which reference
  assets the owner controls and ask which similarities are essential.

Ask only the unresolved questions that change the design. Offer a plain-language choice or explain
how to measure a dimension when the user lacks CAD terms. Inspect available references first and
use measurements from them where the confidence is adequate. Label measured values, user-approved
targets and estimates separately. Await the answers or explicit direction to proceed with proposed
defaults. Confirm the agreed brief in a few lines, including the deliverable and how the owner will
judge fit. For a requested revision, apply clear changes directly and clarify consequential ambiguity
before editing. Carry prior approvals forward.

## Build one meaningful draft

1. Work in the model development branch and an isolated SnapSTL instance. Start with the implemented
   manifest and preview contract in the checkout. The Models plan describes the proposed contract.
   If the tab or renderer is unavailable, use the local OpenSCAD CLI for an honest standalone
   preview and report which in-app step remains unavailable. A text-only conversation needs a
   workspace with access to the CAD toolchain for generated artifacts.
2. Capture the accepted brief and pass/fail measures in the design notes. Choose shared parameters
   for mating geometry, name dimensions in millimetres, and document the origin, assembly transform
   and intended print face for each part. Use simple reusable 2D profiles and extrusions where they
   describe the design clearly. Keep engineering dimensions and decorative choices adjustable
   independently when feasible. Derive dimensions from the same source for interacting parts.
3. Expose the controls the owner needs in the model manifest. Give each a stable ID, readable
   label, validated range and default, with units where relevant. Declare plates, assets and
   assembly positions. For compatible parameter changes, preserve existing IDs and defaults
   unless the user asks to change them. New models use the same dropdown, form and viewer.
4. Export each printable part with the pinned local OpenSCAD build and explicitly registered fonts
   when applicable. Validate inputs, capture renderer diagnostics, and keep generated files in the
   preview output area. For public work, use synthetic text and distributable assets with their
   notices. Keep references and customer data local. Preserve the MakerWorld path and its guards.

## Verify geometry and fit

Start with the default configuration and at least one meaningful boundary case. Add cases for
interactions likely to break, such as a narrow compartment beside a thick divider, an extreme
aspect ratio, or text with counters, descenders or separate dots. Use the existing mesh utilities
and implemented preview tools. Check the exported files in millimetres:

- Confirm expected part and component counts, finite coordinates, positive volume for closed
  solids, watertightness, consistent winding, valid build faces and intended bed placement.
  Inspect thin sections, accidental gaps, overlapping shells and tiny islands.
- Apply the assembly transforms. Check overlap and required contact, available insertion path,
  recess depth and the clearance along the full mating profile. Validate after any applicable
  scaling. A bounding-box comparison alone cannot prove that parts seat. Treat print clearance
  estimated from geometry as provisional until a printed fit test.
- For the organizer, keep the NO AMS base and name separate, with a single line of text. Use
  Scaling v1.1 and its compartment table. Build the insert and its recess from the same glyph
  geometry with a deliberate clearance offset. Check pocket count, floor and wall thickness,
  counters and small pieces. Preserve both production fit checkpoints. Use a coupon or a full
  pair to verify clearance as appropriate.

Record actual checks and their outcomes. If rendering or validation fails, diagnose the first
failure, adjust the smallest relevant part of the design, and rerun the affected checks. Keep a
failed draft labelled as such and keep the previous successful preview available for comparison.

## Review and iterate with the owner

Offer a complete first preview of the agreed design. Tell the owner which dropdown entry and
revision to open, which parameter values are active, and what to inspect in each relevant view.
Ask for targeted feedback on shape, dimensions, assembly and the next change. For example,
“Open Models → Name Desk Organizer → Assembly. Does the name sit where you want it, and is there
enough room in the end compartments for what you’ll store?”

On follow-up, read the latest notes and change the relevant CAD or parameters. Keep unrelated
geometry consistent. Render again, verify the changed dimension and any mating interfaces, and
compare with the prior preview. Confirm that the revised model is selectable and updated in the
development app when that workflow is available. Record accepted decisions, open fit questions and
test results in the design notes for the next agent. Report editable source, STL links, the preset
used and any checks still requiring a printed part. Treat approval for a preview as approval for
that iteration. Any production pilot or public release is its own decision.
