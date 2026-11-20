---
name: snapstl-models
description: Create new SnapSTL CAD models or revise designs in Development, export STL previews, and iterate on fit with the user in Models. Production designs must be reopened by the owner before editing.
---

# Build SnapSTL models

Read the repository's [AGENTS.md](../../../AGENTS.md) and [Models plan](../../../docs/models-workspace-plan.md).
For the organizer, also read the [measurement survey and pipeline](../../../docs/parametric-model-pipeline.md)
and the [scaling rules](../../../docs/scaling-rules-v1.1-handoff.md). For any existing design, read its
manifest, CAD and model-level `DESIGN.md` before proposing a change. Verify its current stage
in Models or the active instance's model approval record. This skill authors new models and
Development designs. If the selected design is in Production, ask the owner to use **Move to
Development** in Models and wait for that change before editing its package or tolerances.

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

## Choose and name the design concepts

Keep one model entry per request. Use the number of designs the owner asks for. When they leave
the count open, build one if the brief fixes the design direction, or propose two to three if
different shapes, construction methods or visual styles would give the owner a meaningful choice.
Explain the difference in a line per concept and confirm it with the initial design discussion.

Give every concept a descriptive name and stable design ID. A different silhouette, layout,
assembly approach or visual language can justify a new design. A different value of an exposed
size, font or colour parameter is a preset of the same design. A refinement of one approach is
a new revision of its design. Keep each named concept independently selectable and printable.

## Build the agreed drafts

### CADAM-style design and importing a CADAM export

Use the owner's Codex or Claude Code session to build and revise OpenSCAD directly. Follow the
CADAM workflow of exposing meaningful dimensions, rendering and inspecting the result from several
directions, and correcting the source through repeated preview. CADAM's public project and examples
are at https://github.com/Adam-CAD/CADAM. This workflow uses the current agent and SnapSTL's
OpenSCAD renderer.

When the owner supplies a CADAM `.scad` export, read the complete file and any accompanying assets.
Preserve the supplied geometry and parameter defaults where possible. Identify every `include`,
`use`, `import`, and font dependency. Confirm the owner can use and distribute those assets before
adding them to the public repository. Declare each required local asset in the manifest. The preview
renderer supplies only the package's declared files, so system-installed libraries and paths outside
the package are not available. If a file is missing, ask for it or replace the dependency with an
owner-approved equivalent and record the change.

CADAM's OpenSCAD Customizer annotations can suggest control labels, ranges and choices. Inspect
the source and map the controls the owner needs to explicit `model.json` parameters. Use descriptive
stable IDs. A comment like `// [10:1:200]` can inform minimum, step and maximum, but do not guess
units or safe fit limits. Mark fit adjustments as Development fields. For each printable part, add
a manifest part and a `part` selector in the SCAD source. Preserve the original design's dimensions,
origins and mating relationships when adapting a multi-part export. If one script displays a whole
assembly, separate the printable parts and record their assembly transforms before claiming they are
individually printable.

Before presenting the draft, render and inspect the assembly and each part from isometric, front,
back, left, right, top and bottom views as applicable. Use the existing Models viewer or local
OpenSCAD image exports and the image viewer available to the agent. Correct missing geometry,
disconnected parts and visibly incorrect features, then render again. Record what was checked in
`DESIGN.md`, including dimensions and any views or physical tests still pending. The owner's
review in Models and the normal Production approval remain necessary.

1. Work on the shared `change/local-parametric-model` branch and use an isolated SnapSTL instance.
   Merge that branch into `main` together only after the owner approves all models for production.
   Start with the implemented
   manifest and preview contract in the checkout. The Models plan describes the proposed contract.
   If the tab or renderer is unavailable, use the local OpenSCAD CLI for an honest standalone
   preview and report which in-app step remains unavailable. A text-only conversation needs a
   workspace with access to the CAD toolchain for generated artifacts. On Apple silicon, check
   `/usr/sbin/sysctl -n hw.optional.neon` before launching the CLI from a restricted shell. If the
   probe is denied, use the desktop app or an unrestricted local development instance. Qt will
   otherwise abort before OpenSCAD reaches the CAD source.
2. Create or update one `DESIGN.md` at the root of the model package. Record the accepted brief,
   interfaces, pass/fail measures and comparison preset. Give each named concept a section for why
   it exists, source revisions, test results and feedback. Choose shared parameters
   for mating geometry, name dimensions in millimetres, and document the origin, assembly transform
   and intended print face for each part. Use simple reusable 2D profiles and extrusions where they
   describe the design clearly. Keep engineering dimensions and decorative choices adjustable
   independently when feasible. Derive dimensions from the same source for interacting parts.
3. Expose the controls the owner needs in the model manifest. Give each a stable ID, readable
   label, validated range and default, with units where relevant. Declare plates, assets and
   assembly positions for each named design. Keep use-time inputs such as names, dimensions and
   colours in Production fields. Put glue-in clearance, rim adjustments and fitting tolerances
   in Development fields. Record reasons and measured fit feedback in the model's `DESIGN.md`. For compatible parameter changes, preserve existing IDs and defaults
   unless the user asks to change them. New models use the same dropdown, form and viewer.
4. Export each printable part with the pinned local OpenSCAD build and explicitly registered fonts
   when applicable. Validate inputs, capture renderer diagnostics, and keep generated files in the
   preview output area. For public work, use synthetic text and distributable assets with their
   notices. Keep references and customer data local. Preserve the MakerWorld path and its guards.

## Design for layer-based colour changes

When a design uses multiple print colours, ask which faces need contrast and whether the owner
wants a single filament change, a limit on changes, or separate pieces. Use the intended print
orientation to put colour regions on consecutive Z bands where the design permits. For raised
lettering or a motif, finish the base at one common Z and put only the contrasting features
above it. Keep the features supported and bonded to the solid below. For more complex models,
consider the build direction, stepped detail and the order of coloured features together.
Avoid two colours on a layer when the visual goal allows it. If a shared layer is essential,
compare the slicer's change count and purge estimate for that layout and for separately printed
pieces, and agree on any assembly and fit implications with the owner.

Record each change height from the print bed and the filament order in the design record.
Align boundaries to the actual layer schedule, including the first layer and any variable
layer heights. Verify that each layer in the sliced preview uses the intended colour and count
the changes before presenting a print workflow as verified. A mesh check can establish the
transition geometry. The chosen slicer profile determines the actual toolpath. For a geometry-only
STL, give the owner the change height and instructions for setting the filament change or pause
in their slicer. Keep these standalone instructions within the existing Models and print rules.

## Verify geometry and fit

For each draft, start with common comparison inputs and at least one meaningful boundary case. Add cases for
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

Offer complete first previews of the agreed concepts. Tell the owner which model, named designs
and revisions to open in Models. Compare like-for-like presets and camera angles where possible.
Ask what works in each named design and which parts or appearance details they want to carry
forward. For example, “Open Models and compare the Stepped and Arched drafts in Assembly. Which
looks better on your desk, and which gives your scissors more usable space?”

On follow-up, read `DESIGN.md` and identify the design and revision the feedback refers to. Change
the relevant CAD or parameters. Render again, verify changed dimensions and mating interfaces,
and compare with the previous review. Confirm each revised draft appears in the development app
when available. Record feedback, decisions, test results and open fit questions by design and
revision. Report editable source, STL links, the comparison preset and physical checks still due.

Keep each revision in Development while iterating. When the owner approves a verified revision,
ask the owner to select that preview and click **Move to Production**. SnapSTL pins the reviewed
CAD and development settings locally. Record the approval, revision and fit results in
`DESIGN.md`. Later fit or CAD changes begin when the owner moves the design back to Development.
A production batch pilot or public release is its own decision.
