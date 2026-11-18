# Name Desk Organizer

## Letter Contour

**In development.** The owner reviewed the first preview and asked to keep Letter Contour only,
match MakerWorld design 2600234 closely, and bring its font and parameter options into Models.
The owner confirmed permission to reproduce the design. This local preview is for review on
the development branch.

The common comparison preset is the synthetic name `AaLiQ`, Chewy, font size 60 mm, thickness
50 mm, outline 6 mm, rotate off and 0.30 mm nominal recess clearance. It produces a 210 × 50 ×
89 mm base with five compartments and a 197.34 × 74.95 × 2 mm name insert.

### Geometry and assembly

- Use one flat font outline for the insert and its recess. The base's front and rear rim use the
  font outline expanded by the outline setting. A vertical continuation within the name's width
  supports the floor. Very narrow text gets wider shoulders to keep the compartments usable.
- The raw insert is 4 mm thick. The recess is 1 mm deep. Front and back walls measure 2.9 mm and
  3 mm respectively before the recess, with a 3 mm floor and 3 mm dividers before scaling.
- The base prints upright and the name flat. Export the name first and use its actual bounds to
  generate the base. For assembly, rotate the name so its print-plane Y becomes base Z and its
  thickness points towards the front. Place the back of the insert at the floor of the recess.
- Scaling v1.1 chooses compartments and base length/height by letter count. It applies the same
  X and height factors to both parts, keeps base depth and halves the insert thickness to 2 mm.
  For unusually compressed names, the recess grows enough to meet the digital clearance check.
- Model defaults follow current SnapSTL Product settings. The bundled Chewy, Luckiest Guy,
  Titan One, DynaPuff, Cherry Bomb One and Akaya Kanadaka fonts retain their own license notices.
  This preview accepts single-line A–Z text up to 15 letters. Thickness acts as base depth.
  Rotate text tilts the glyph profile 6° before creating both parts. The cached examples all used
  the same options, so the exact original behavior for non-default thickness and rotation is open.

### Verification and open checks

- Compared the generated unscaled meshes with six cached pairs that passed SnapSTL's watermark
  scan. Projected insert overlap was 99.98–99.99%. Rear base silhouette overlap was 97.57–100%.
  The largest base envelope difference was under 0.005 mm. The sample with the largest silhouette
  difference was also the reference base that failed the watertightness check.
- Local OpenSCAD 2026.10.11 exported every font, examples from all six scaling bands, an unusually
  wide 15-letter string, both rotation settings and both ends of the numeric parameter ranges.
  Every pair passed the solid, compartment, fit and written-STL dimension checks.
- In the browser, changing Chewy to DynaPuff regenerated the assembly from the two new STLs.
  The preview reports the current revision and whether it reflects the current inputs.
- **Open:** owner review, physical fit and glue-up, Windows and the native webviews, and direct
  comparison with MakerWorld exports for non-default settings. Print tests are needed before
  marking this design Developed or piloting it with production orders.
