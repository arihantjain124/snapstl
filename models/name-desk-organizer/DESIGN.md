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
  This preview accepts single-line A–Z text up to 15 letters. MakerWorld labels Thickness as the
  container depth. **Rotate text → yes** shows a signed angle in degrees, from −30° to +30°.
  It defaults to 6° and rotates the shared glyph profile before creating either part.
  The exact original behavior for non-default rotation is open.

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

### AaLiq reference and revised rim

The owner supplied the watermark-free MakerWorld v2.34 3MF for `AaLiq` in NO AMS, with Chewy,
size 60, thickness 50, outline 6, five compartments and rotation off. Both meshes are closed.
The raw base measures 196.975 × 50 × 97.632 mm. Its five pockets are each about 35.6 mm wide
after Scaling v1.1. The apparent small pocket near L and i came from the local rim dropping to
about 45 mm above the floor. MakerWorld fills that gap to about 56 mm above the floor. The raw
flat section is 64.437 mm high, exactly 66% of the raw base height. The local model now applies
that floor to the contour while keeping pocket widths unchanged. The corrected rear silhouette
overlaps the exact reference by 99.79%, and all five measured minimum rim heights match to 0.1 mm.

Development fields hold glue-in clearance and an optional minimum retaining rim above the floor.
Production fields hold the name, font, size, thickness, outline, rotation and both preview colours.
Name line 2, two-line alignment and compartment derivation follow the single-line and Scaling
v1.1 rules. The reference file remains local. The design stays in Development until the owner
checks the print and deliberately moves the reviewed revision to Production.
