# Local parametric models

Research and implementation proposal · 2026-11-18

The first model is a desk organizer with open compartments, a base whose outline follows the name,
and a separately printed name that seats in a shallow recess. The immediate goal is to make new
pairs locally during MakerWorld outages. The longer-term goal is to turn a plain-language brief into
editable CAD, printable parts and previews using the same workflow for future models.

## Organizer reference survey

Measured the unscaled STL pairs in SnapSTL's configured Downloads folder with the app's mesh,
watermark, scaling and component helpers. Files were read directly because `cache.load()` also
updates the cache usage record. The survey used the current Chewy / 60 / 50 / 6 / rotate off
settings. Of 14 complete pairs, six passed the app's watermark scan and eight were excluded on
that scan. The six measured pairs span 4–7 letters and four or five compartments. Data below are
rounded mesh measurements in millimetres. The reference meshes and customer names stay local.

To repeat the survey, load each accepted STL pair with `trimesh` and normalize each part with
`meshes.to_origin`. Measure the envelopes with `meshes.dims`. Use coplanar triangle areas to find
the floor and recess planes, then intersect the base at several depths to measure the wall and
divider profiles. Count openings at the floor and use `extras._components` for the name. Run
`scaling.scale_pair` with the matching band to record the finished dimensions separately.

| Feature | Observed on the six pairs |
|---|---|
| Base envelope (unscaled) | X 149.61–252.31, Y 50.00, Z 74.82–95.75 |
| Flat name envelope (unscaled) | X 137.61–240.31, Y 62.82–83.75, Z 4.00. The name meshes are flat extrusions with 5–8 connected components, including separate dots where applicable. |
| Base profile | Overall X and Z are each about 12.00 mm larger than the corresponding name dimensions. The front and back top edges follow the text silhouette. The ends and the dividers follow the local height of the profile. |
| Floor and walls | 3.00 mm floor. Front inner wall plane at Y 2.90, reducing to about 1.90 mm of material behind the recess. Back inner wall plane at Y 47.00, leaving 3.00 mm. End walls are about 3.00 mm at the floor. |
| Compartments | Equal clear widths within each base. Clear depth about 44.10 mm. Clear width in this sample 30.67–44.47 mm. Dividers are 3.00 mm thick and reach the local top contour. |
| Front recess | 1.00 mm deep. Its X and Z envelopes are about 0.60 mm larger than the corresponding name dimensions. Its near edges are about 5.70 mm from the base's X and Z minima. This suggests about 0.30 mm nominal clearance per side before scaling. |
| Production output with current bands | Base 200–222 mm long, 50 mm deep and 87.5–90 mm high for these lengths. Name 183.96–211.44 mm long, 73.72–78.72 mm high and 2 mm thick. |

The recess clearance is an estimate from triangulated surfaces. Physical fit, glyph counters and small
islands need checking. Five of six reference bases and all six names loaded as watertight meshes.
The measured pairs do not establish geometry for the six-compartment band, extreme letter widths,
very short or long names, rotation, or other fonts. End-wall curvature, the exact offset behavior
around counters and the thickness control still need calibration if close visual matching is approved.
Where authorized, use synthetic controlled references and a physical sample with calipers to resolve
these details. Older notes include historical variants with name overhang, so treat the table above
as measurements of this sample and these settings.

## Toolchain and design

- Start with **OpenSCAD and Chewy**. OpenSCAD's source is editable text, and its command line can
  generate each part as binary STL. The installed OpenSCAD 2026.10.11 successfully generated a
  synthetic pair from a temporary copy of the untracked prototype, with explicit font registration
  using `use <…/Chewy-Regular.ttf>`. Both meshes were watertight. The executable hit a Qt CPU
  feature error under this Mac's agent sandbox and ran with approved execution outside it. Verify a
  pinned build with a font and `textmetrics()` smoke test on Mac and Windows. [OpenSCAD downloads](https://openscad.org/downloads.html)
  · [CLI reference](https://github.com/openscad/openscad/blob/master/doc/openscad.1.in)
  · [Qt issue](https://github.com/openscad/openscad/issues/6712)
- The untracked prototype establishes that local export works. It uses a rounded rectangular box,
  shorter dividers, a bridged insert with raised letters and a 0.6 mm recess. Use the measured design
  requirements to drive the geometry work. Calibrate one font before adding the other five. Record
  the CAD build and font checksum in each preview's manifest. [Chewy font and license](https://github.com/google/fonts/tree/main/apache/chewy)
- Generate one shared glyph profile. Use it for the flat insert, a recess with a clearance offset,
  and the organizer wall profile. Build the front/back walls and floor from explicit parameters.
  Cut equal-width compartment voids from the same solid so the divider tops follow the walls. Keep
  the base upright and the insert flat with both at Z = 0 in their respective print coordinates.
- Check publication rights before pursuing close visual reproduction of the third-party design.
  MakerWorld describes its commercial membership around selling physical prints. The terms for
  this particular model were unavailable during this survey. Confirm the creator's terms or written
  permission before publishing a close reproduction. An independently styled design that meets the
  functional measurements gives us a separate release path. Publish editable CAD for the design we
  have rights to release, with a license chosen by the owner and the font's own notice. CERN-OHL-P-2.0
  is a permissive candidate for original CAD. [MakerWorld commercial membership](https://blog.bambulab.com/empowering-our-creators-with-new-commercial-license-membership/)
  · [CERN license variants](https://ohwr.org/licences/)

## Build milestones

1. **Preview locally.** Pin an OpenSCAD build and font, then build the Chewy organizer in a
   standalone command. Use SnapSTL's letter-count bands as the default in production-sized previews.
   The command should accept explicit, validated parameters and export two STLs in millimetres,
   an assembled preview and a short geometry report. Keep generated examples under an ignored
   output folder. Verify the command can run again from the saved parameters.
2. **Prove print fit.** Cover all six scaling bands and representative glyph cases with
   synthetic names including narrow and wide glyphs, mixed case, descenders, counters and dotted
   letters. Check positive-volume watertight solids, component count, dimensions, pocket count,
   orientation, wall thickness and minimum feature width. Register the insert to the recess using
   the CAD assembly transform and compare the cut profile and clearance after scaling. SnapSTL's
   current fit check measures preservation of proportions, so add this seating check for the local
   model. Print clearance coupons and full pairs for four, five and six compartments, including
   short and long names. Compare photos, dry fit, glue-up, footprint, and slicer time/material with
   existing prints. Record chosen tolerances from those trials.
3. **Test the plate path.** Feed the validated pair into `scaling.scale_pair` and the existing
   grouping, packing and 3MF writer. Exercise the existing fit guards at both checkpoints and verify
   name-letter repair works with local components. Time a representative queue of synthetic jobs
   with the network off to establish throughput and failure handling. Confirm a fresh install can
   reproduce the result on Mac and Windows.
4. **Pilot in SnapSTL after review.** Add an explicit per-batch local model source for a small
   pilot. Snapshot source, model revision and font in the batch. Include those in the local cache
   identity, use source-specific metadata and publish both validated meshes as a pair. Generation
   failures should show which model failed and allow a manual retry. The existing MakerWorld path
   continues through the Helper and its license and watermark guards. Keep manual batch steps,
   shared order claims, Scaling v1.1, plate grouping and print start controls. Expand to backlog
   batches only after the pilot's prints pass inspection.

## Reusable workflow for new designs

Give each design a brief, editable source, typed parameter specification, example configurations
with synthetic values, and a revision log. The brief records use case, millimetre axes, part
orientations, fit interfaces, print constraints and open questions. Record each experiment's
inputs, measured and printed results, and the change made for the next revision.

Use the same sequence per design:

1. Turn the brief into validated parameters with units, defaults and ranges. Record assumptions
   in the preview manifest so the next request can adjust them.
2. Render each printable part locally and generate assembled front, top and angled views.
3. Run the shared mesh checks and the design's own interface checks. Return the STLs, views,
   editable source and parameters together. Flag any failed checks or untested print assumptions.
4. Iterate by changing parameters or adding a new CAD revision if the shape logic needs to change.
   Save the selected preset once physical testing confirms it.

Example request once the command exists:

> In SnapSTL, preview the organizer with the sample text AaLiQ in Chewy at production size. Use the
> standard settings and give me the base and name STLs, a front view with the name seated, and the
> finished dimensions. Then make a second preview with 0.15 mm more clearance per side.

The prompt describes the change. The local tool renders and checks it. Use synthetic names when
working from chat. Production parameters that contain customer names stay on the owner's computer.
