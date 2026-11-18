# Picture Frame

## Folding Easel

The owner requested a 4 × 6 in frame with a folding stand, the six existing organizer fonts, and a choice of top or bottom lettering. This first Development draft uses portrait orientation, a rounded bezel, a rear-loaded paper photo and a three-part assembly. The assumed photo is 101.6 × 152.4 mm. Use a spacer sheet if a thin photo rattles in the rebate. All dimensions below are CAD targets for the first print.

| Interface | Draft dimensions |
|---|---|
| Bezel and opening | 135.6 × 186.4 × 7 mm bezel. The 94.6 × 145.4 mm opening covers 3.5 mm of the photo at each edge. |
| Photo pocket | 102.2 × 153.0 mm at default clearance, with 0.8 mm of straight depth and a 45° transition into the opening. A 3 mm backing plate closes the pocket. |
| Cover mounting | Four M3 × 6 mm pan-head screws through 3.4 mm holes into printed 2.65 mm pilot bores. Drive the screws by hand. |
| Stand hinge | An M3 × 45 mm bolt and M3 locknut join the three knuckles across a 37.7 mm span. Use a smooth shank across the hinge if available. Leave the nut loose enough for the stand to rotate. |
| Stand travel | A 0.35 mm side gap at each ear and 0.20 mm radial pin clearance at default. A built-in stop sets 30° of opening. The open foot matches a 17° rearward desk lean. |
| Lettering | The same six fonts bundled by the organizer, with license notices in this package. Up to 24 characters, centered on either border and resized to fit. The raised text is 0.65 mm high and can be turned off. |

### Review and assembly

In **Models → Picture Frame → Folding Easel**, generate the default **Good Times / Chewy / Bottom** preview. Rotate **Assembly** to see the open stand. Switch **Text position** to **Top** and generate again for a like-for-like comparison. The three individual plate views show the STL print orientations.

1. Put the stand knuckle between the cover ears. Align the holes and slide the M3 × 45 mm bolt through. Fit the locknut and check that the stand swings freely.
2. Place a paper photo face down inside the bezel from the back. Lay the backing over it with the hinge up and the stand directed toward the frame's bottom edge. Attach the cover using the four short screws.
3. Open the stand until it stops. Both the lower rear edge of the frame and the foot should touch the desk. Fold the leg back to stow it.

### First revision

The bezel prints rear face down with the lettering facing up. The cover prints photo side down and the stand prints with its inner flat face down. All plate minima are at the origin in exported STLs. In Assembly, the bezel remains fixed. The backing rotates 180° about X and translates into the rear recess plane. The stand rotates 210° about X and translates to align the three knuckles at X-axis pivot Y = 116 mm, Z = −9 mm. The static transform constants in the manifest follow the corresponding dimensions in the CAD source.

OpenSCAD 2026.10.11 exported all three closed, consistently wound, connected solids. Cross-sections verify the default photo recess and lip, aligned screw bores, pin and side clearances, closed position within the frame silhouette and interference-free motion through 30° in 1° steps. The open foot and frame edge lie on the same desk plane. The modeled center of mass falls between them. The six font choices rendered at both positions with a 24-character mixed-case sample and maximum fit settings. The unlettered frame rendered at the minimum clearances.

Physical fit remains open. Check screw bite, paper retention, leg swing and stability on a desk. Support and brim needs depend on the slicer and material. Review the prototype in Development before any Production approval.
