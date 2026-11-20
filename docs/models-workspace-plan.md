# Models in SnapSTL

Implementation plan · 2026-11-18

The development branch implements catalog discovery, manifest controls, interactive plate and
assembly views, STL export, and the Letter Contour organizer draft. The selector supports
multiple named designs and can copy compatible inputs for fast switching. The current organizer
has one selectable design at the owner's direction. Moving a tested preview to Production pins
its source and development values on this computer. Moving it back reopens the working draft with
the last tuned fit values. Side-by-side comparison and browsing all historic revisions remain
follow-up work.

Add **Models** to the SnapSTL sidebar. The first entry is **Name Desk Organizer**, marked In
development. The same selector, parameter form, 3D viewer and export controls serve each new model.
An agent working in the development checkout authors OpenSCAD and a small model manifest. SnapSTL
discovers those files while it is running. The owner can change parameters and inspect each new
revision in the app without a service restart or an app release.

## What the owner sees

1. Open **Models** and pick a model. Under the model picker, show its named **Designs** with
   **In development** or **Developed** status. Pick a design and, when available, an earlier
   revision. The selected design's controls sit above the viewer. Each control has a plain-language
   label, a default, a unit when relevant and a valid range or set of choices.
2. Edit values and click **Generate preview**. Keep the last successful view visible while rendering
   and identify its revision and stale status. Show errors next to the affected field or above the
   viewer. **Reset defaults** reloads the selected design's defaults.
3. Inspect each preview plate in 3D. Show all plates as a list of labelled viewports or cards with
   one larger active view. Include orbit, zoom, pan and fit-to-view. Use a bed grid, actual dimensions
   and distinct part colours. For multipart designs, offer an **Assembly** view if the manifest
   supplies transforms. The organizer shows an upright base plate and a flat name plate. The
   assembled view shows how the insert sits, with convenient front and angled views.
4. Read the selected part's dimensions and geometry findings. Download the corresponding STL
   when validation passes. Preview plates represent the print orientation of the design's parts.
   Production batch layouts, slicing and printing remain explicit workflows in their existing pages.

### Compare drafts and approve designs

Keep a model request under a single model entry. Start with the number of concepts requested by
the owner. If they leave the number open, use one when the direction is settled or propose two to
three when different shapes, construction approaches or visual styles could answer the brief.
Use distinct names that describe each concept. A different parameter value, font setting, size or
colour remains a configuration of the same design. Keep accessible named concepts and useful
reviewed revisions so feedback can refer to a specific design and revision.

Compare two to three concepts with the same camera and comparable input values, using side-by-side
views where there is room and fast switching otherwise. Preserve each concept's edits and carry
shared inputs across comparison when their IDs and ranges agree. Show each concept's geometry
checks, dimensions and status alongside its preview. Ask for feedback by concept name.

When the owner approves a verified revision, mark that design **Developed**. Several named designs
of the same model may reach Developed. A model with an approved design appears in **Developed**,
and any sibling drafts remain visible. Pin approval to the reviewed source revision and store
later edits as drafts so an approved version remains available. Developed records design approval.
Production use goes through the separate pilot.

### Using the model-building skill

Put a **Build a model with an agent** help panel next to the model picker. Explain how to open
the SnapSTL development checkout and paste a plain-language request into Codex or Claude Code.
Show `$snapstl-models` for Codex and `/snapstl-models` for Claude Code, with a short example in
the help text. The skill changes new or Development models.

> Design a desk organizer for pens and scissors with a glue-in name. Give me three clearly
> different concepts and ask me which measurements you need before you build. Tell me what
> to open in SnapSTL Models when the drafts are ready.

Tell the owner to return to **Models**, select the named model and compare the suggested drafts.
When the agent publishes another revision, the running preview app picks it up from the checkout.

While **Models** is open, periodically refresh the catalog's revision information and refresh it
on entering the tab or returning focus. Add discovered designs to the selector immediately. When
the selected design changes on disk, revalidate its current form values and request one updated
preview if those values still apply. Keep edits and compatible values across revisions. Highlight
renamed, removed or invalid inputs and wait for correction. Discard stale render results using the
selected model, design ID, source revision and submitted parameters. Use manual **Generate preview** for
form changes so typing a name doesn't start a render for every keystroke.

## Files an agent adds or edits

Use the proposed development root `models/<model-id>/` in the development checkout. The Models
server reads one explicitly configured local checkout, selected when launching the development
instance. Model IDs remain stable across revisions. The initial catalog scans direct children for
`model.json`. Each package contains:

| File | Purpose |
|---|---|
| `model.json` | Schema version, stable model ID and title, and named design entries with stable IDs, status, current revision and approved revision when applicable. Each design declares its entry point, parameters and preview plates. |
| OpenSCAD source | Separate named concept entry points with deterministic part selection. Shared dimensions and geometry helpers can live in common include files. |
| Assets or includes | Local fonts and supporting CAD, declared so a change invalidates the current preview. Include asset provenance and license notices. |
| `DESIGN.md` | One required file per model. Record the brief, interfaces, design names and rationale, like-for-like comparison presets, feedback and checks by design and revision, approved revisions and open questions. Read it before revising the model. |

Start with a small versioned manifest. Control types are text, integer, number, checkbox and choice.
Each definition has a parameter ID, display label and typed default. Add units, bounds, step and
length or choice constraints as appropriate. Part definitions declare a stable ID, label, SCAD
selector, display colour and optional rigid transform into assembled coordinates. In the first
version, one printable part is one preview plate. Future grouped previews can use a list of
positioned parts per plate without a different viewer. Use production rules in the organizer
adapter for derived values such as compartment count and finished size. Display derived values
read-only and keep Scaling v1.1 in its existing module.

The catalog revision is a digest of a validated manifest, CAD source and declared assets.
Store approved source and dependency snapshots and any revisions promised for review, so a later
edit to a shared include preserves earlier geometry.
The renderer snapshots that revision for each render. A malformed or partially saved package
shows a per-design error while other designs remain available. Update package files and publish the
manifest last when changing the contract. The app swaps in a new preview only after a complete
matching result succeeds. Adding a design or editing its geometry within this contract requires
only package changes. A genuinely new kind of control or renderer requires a versioned app change.

## App integration

- Add the sidebar entry and `t-models` to the single-page interface in
  `src/snapstl/static/index.html`. Prototype interactive mesh rendering with built-in WebGL
  in the browser and verify it inside both native app webviews. Shared code owns cameras, bed
  coordinates, loading and error states. Keep dimensions and an explanatory fallback usable if
  WebGL is unavailable. Evaluate any vendored viewer dependency separately with the owner.
- Use the dedicated localhost namespace `/api/model-studio` so the existing downloaded
  model pictures keep their route. Provide catalog metadata, preview submission and
  validated mesh retrieval by server-issued ID. Reuse the app's access controls. Keep the routes
  outside the logistics and helper allowlists. Preview submission follows the current shared-records
  write gate unless the owner explicitly chooses a sandbox-preview exception. The standalone CAD
  preview remains usable without MakerWorld or shared records.
- Execute the pinned OpenSCAD build locally using an argument list and schema-validated values.
  Discover only trusted local model packages. Resolve entry points and declared dependencies
  within the package, reject escaping paths and symlinks, and run with an isolated input snapshot
  and output directory. Keep CAD source and execution local. Apply a render timeout and bounds
  on text, parts, job concurrency and artifact sizes. The browser shows a rendering state while a
  bounded request runs. The server returns sanitized diagnostics. Keep preview outputs separate
  from batch outputs and the model download cache. A small memory cache retains recent previews.
- Validate the exported mesh with existing SnapSTL mesh utilities. Return plate labels, bounding
  boxes, parts and URLs for binary geometry, with assembly transforms and validation results.
  Cache by model ID, design ID, source revision, normalized parameters and engine build. Generate in a development
  instance such as the documented temporary home on port 8320, with synthetic input and an
  independent output directory. Use SnapSTL's existing test configuration for shared records
  in that isolated instance. Model iteration needs no access to production data or printers.

The implemented routes are `GET /api/model-studio/catalog`,
`POST /api/model-studio/preview` and `GET /api/model-studio/previews/{key}/{part}.stl`.
Use `POST /api/model-studio/approval` to promote a reviewed preview or reopen its design.
Models printing uses `GET /api/model-studio/printers` and `GET/POST /api/model-studio/print`.
Its verified 3MF sources live in the app's `model-prints/` folder. Standalone jobs use the
existing local print and slice queue with batch ID 0. Bambuddy receives a held job and the selected
loaded filament slot. Only the Start or Resume click releases it, subject to the normal bed-clear
rules. Models printing does not create an order or a batch.
The app reads `models/` in its checkout by default or the local directory in `SNAPSTL_MODELS_DIR`.
The manifest's design ID, source and declared assets determine its revision. The organizer's
specific options and measured results are in [DESIGN.md](../models/name-desk-organizer/DESIGN.md).

On this branch, moving **Name Desk Organizer → Letter Contour** to Production makes it the only built-in
source in **Batches → Queue download**. Moving it to Development withdraws the source from new jobs.
Each queued batch keeps its approved local snapshot and uses the existing manual build and fit checks.

## Build and review

Use `change/local-parametric-model` for all model packages and shared Models changes.
Review each design in Development. After the owner approves all models for production, release the
branch together by merging it into `main`. The **Move to Production** control records each local
approval. The Git merge is a separate release step.

| Milestone | Ready when |
|---|---|
| Catalog and common viewer | A tiny synthetic fixture renders from manifest to form to all preview plates. A second fixture appears in the dropdown while the app is running. Changing its schema and SCAD refreshes the selected form and preview without a restart. |
| Organizer draft | Letter Contour renders two printable parts and an assembled view with the six supported fonts. Short, long and dotted names, all scaling bands and compartment counts, and invalid input exercise the model. The owner reviews the visual match before approval and print trials. |
| Fit and platform verification | Exported geometry meets the agreed dimensions and clearance. Local wall thickness, seating and print orientation are checked. Coupons and full pairs verify physical fit. Browser and native macOS/Windows checks cover orbit, zoom, stale renders, errors and STL export. |
| Separate production pilot | A small manually selected batch uses approved organizer revisions through the existing scaling and plate checks after owner review. |

Acceptance checks include comparing named drafts, approving and reopening multiple developed
designs, keeping an approved revision stable across draft edits, a broken manifest, unsupported parameter value, missing font or OpenSCAD,
timeout, malformed mesh, rapidly switching models, concurrent source edits and preserving the last
good view on failure. Prove that the preview uses the same meshes the owner downloads.

The [parametric model pipeline](parametric-model-pipeline.md) contains the organizer measurements
and release considerations. The [model-building skill](../.agents/skills/snapstl-models/SKILL.md)
guides the conversation and CAD iterations. Open the development checkout and invoke
`$snapstl-models` in Codex or `/snapstl-models` in Claude Code. A chat without access to that
checkout can develop the brief and parameters, with local CAD execution handled in a workspace
that has the toolchain. [Codex project skills](https://developers.openai.com/api/reference/cli)
· [Claude Code project skills](https://code.claude.com/docs/en/skills)

The skill also supports adapting CADAM-exported OpenSCAD into a model package. The agent examines
the source and dependencies, maps adjustable parameters to the manifest, separates printable
parts, and checks multiple views before owner review. CADAM itself is a separate application.
