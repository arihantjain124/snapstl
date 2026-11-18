# Models in SnapSTL

Implementation plan · 2026-11-18

Add **Models** to the SnapSTL sidebar. The first entry is **Name Desk Organizer**, marked In
development. The same selector, parameter form, 3D viewer and export controls serve each new model.
An agent working in the development checkout authors OpenSCAD and a small model manifest. SnapSTL
discovers those files while it is running. The owner can change parameters and inspect each new
revision in the app without a service restart or an app release.

## What the owner sees

1. Open **Models** and pick a design. Above the viewer, show the design name, development status
   and its controls. Each control has a plain-language label, a default, a unit when relevant and a
   valid range or set of choices. Use the same widgets for every design.
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

While **Models** is open, periodically refresh the catalog's revision information and refresh it
on entering the tab or returning focus. Add discovered designs to the selector immediately. When
the selected design changes on disk, revalidate its current form values and request one updated
preview if those values still apply. Keep edits and compatible values across revisions. Highlight
renamed, removed or invalid inputs and wait for correction. Discard stale render results using the
selected model, source revision and submitted parameters. Use manual **Generate preview** for
form changes so typing a name doesn't start a render for every keystroke.

## Files an agent adds or edits

Use the proposed development root `models/<model-id>/` in the development checkout. The Models
server reads one explicitly configured local checkout, selected when launching the development
instance. Model IDs remain stable across revisions. The initial catalog scans direct children for
`model.json`. Each package contains:

| File | Purpose |
|---|---|
| `model.json` | Schema version, stable ID, title and maturity, entry point, ordered parameter definitions, and an ordered list of preview plates/parts. |
| `model.scad` | OpenSCAD entry point with deterministic part selection. |
| Assets or includes | Local fonts and supporting CAD, declared so a change invalidates the current preview. Include asset provenance and license notices. |
| `DESIGN.md` | User-approved brief, interfaces, geometry checks, decisions and revision notes. |

Start with a small versioned manifest. Control types are text, integer, number, checkbox and choice.
Each definition has a parameter ID, display label and typed default. Add units, bounds, step and
length or choice constraints as appropriate. Part definitions declare a stable ID, label, SCAD
selector, display colour and optional rigid transform into assembled coordinates. In the first
version, one printable part is one preview plate. Future grouped previews can use a list of
positioned parts per plate without a different viewer. Use production rules in the organizer
adapter for derived values such as compartment count and finished size. Display derived values
read-only and keep Scaling v1.1 in its existing module.

The catalog revision is a digest of a validated manifest, CAD source and declared assets.
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
- Use a dedicated localhost API namespace such as `/api/model-studio` so the existing downloaded
  model pictures keep their route. Provide catalog metadata, preview submission, job status and
  validated mesh retrieval by server-issued ID. Reuse the app's access controls. Keep the routes
  outside the logistics and helper allowlists. Preview submission follows the current shared-records
  write gate unless the owner explicitly chooses a sandbox-preview exception. The standalone CAD
  preview remains usable without MakerWorld or shared records.
- Execute the pinned OpenSCAD build locally using an argument list and schema-validated values.
  Discover only trusted local model packages. Resolve entry points and declared dependencies
  within the package, reject escaping paths and symlinks, and run with an isolated input snapshot
  and output directory. Keep CAD source and execution local. Apply a render timeout and bounds
  on text, parts, job concurrency and artifact sizes. Stream progress states via job polling and
  return sanitized diagnostics. Keep preview outputs separate from batch outputs and the model
  download cache.
- Validate the exported mesh with existing SnapSTL mesh utilities. Return plate labels, bounding
  boxes, parts and URLs for binary geometry, with assembly transforms and validation results.
  Cache by model revision, normalized parameters and engine build. Generate in a development
  instance such as the documented temporary home on port 8320, with synthetic input and an
  independent output directory. Use SnapSTL's existing test configuration for shared records
  in that isolated instance. Model iteration needs no access to production data or printers.

## Build and review

| Milestone | Ready when |
|---|---|
| Catalog and common viewer | A tiny synthetic fixture renders from manifest to form to all preview plates. A second fixture appears in the dropdown while the app is running. Changing its schema and SCAD refreshes the selected form and preview without a restart. |
| Organizer draft | A Chewy example renders two printable parts and an assembled view. Short, long and dotted names, all scaling bands and compartment counts, and invalid input exercise the first model. The owner reviews shape and appearance in the app. |
| Fit and platform verification | Exported geometry meets the agreed dimensions and clearance. Local wall thickness, seating and print orientation are checked. Coupons and full pairs verify physical fit. Browser and native macOS/Windows checks cover orbit, zoom, stale renders, errors and STL export. |
| Separate production pilot | A small manually selected batch uses approved organizer revisions through the existing scaling and plate checks after owner review. |

Acceptance checks include a broken manifest, unsupported parameter value, missing font or OpenSCAD,
timeout, malformed mesh, rapidly switching models, concurrent source edits and preserving the last
good view on failure. Prove that the preview uses the same meshes the owner downloads.

The [parametric model pipeline](parametric-model-pipeline.md) contains the organizer measurements
and release considerations. The [model-building skill](../.agents/skills/snapstl-models/SKILL.md)
guides the conversation and CAD iterations. Open the development checkout and invoke
`$snapstl-models` in Codex or `/snapstl-models` in Claude Code. A chat without access to that
checkout can develop the brief and parameters, with local CAD execution handled in a workspace
that has the toolchain. [Codex project skills](https://developers.openai.com/api/reference/cli)
· [Claude Code project skills](https://code.claude.com/docs/en/skills)
