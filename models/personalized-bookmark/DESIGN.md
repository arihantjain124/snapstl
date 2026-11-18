# Personalized Bookmark

Review the four drafts under **Models → Personalized Bookmark**. The brief is a thin,
one-piece bookmark with a customizable single-line inscription and separate base and letter
colours. A common layer boundary allows one filament change. The comparison preset reads
`Chapter One` in Chewy at a maximum letter height of 7 mm, with a 120 × 28 × 1.0 mm base,
0.6 mm raised lettering, a blue-green base and warm cream lettering.

## Drafts

| Design | Revision | Geometry |
| --- | --- | --- |
| Classic Round | `bec16bff60609498` | Four 4 mm-radius corners on a rounded rectangle. |
| Ribbon Tail | `9c768b654fbdfc1d` | A notched lower end inspired by a ribbon. |
| Open Arch | `e6ae395484815786` | Domed top and through-window with a 3.5 mm bridge for an optional ribbon. |
| Faceted Tab | `4211bc9551132568` | Angled shoulders and a flat top. |

## Parameters and print geometry

The length runs along Y, with the face from (0, 0) to (width, length). The flat back rests on
Z = 0. Each draft is one plate with one fused solid and an identity assembly transform. Text
runs along the length. OpenSCAD measures the glyph outlines and scales the full inscription
proportionally to fit the available space. Shorter text is easier to read on the smallest
bookmark. Chewy is bundled with its Apache 2.0 license.

| Control | Default and range |
| --- | --- |
| Inscription | `Chapter One`, up to 28 characters on one line |
| Length | 120 mm, 85–160 mm |
| Width | 28 mm, 22–40 mm |
| Base thickness | 1.0 mm, 0.8–1.6 mm in 0.2 mm steps in Models |
| Maximum letter height | 7 mm, 4–12 mm |
| Base colour and Letter colour | Blue-green and cream by default |
| Raised letter height | 0.6 mm, 0.4–0.8 mm in Development |

The base ends at its configured thickness. The letter outlines continue above that plane and
join the base with a small overlap inside the solid. Download the STL for a two-colour job and
set the filament change or pause before the first letter-only layer. At a first layer height
and fixed layer height of 0.20 mm, the default geometry puts the base on layers 1–5 and the
lettering on layers 6–8. Read the resulting layer preview for the actual printer profile.
Matching preview colours permit the single-slot Models print workflow.

## Checks and open review

OpenSCAD 2026.10.11 generated the default palette preset and each draft's size extremes.
The small test used `Élodie, just one more page!` with a 22 × 85 × 0.8 mm base and 0.4 mm
relief. The large test used a dotted `i` with a 40 × 160 × 1.6 mm base and 0.8 mm relief.
Each STL passed the closed, positive-volume mesh check, had one connected body and rested flat
on Z = 0. The base and raised-letter faces met across the intended colour plane. Open Arch
retained its window, and the other drafts had uninterrupted floors. The macOS browser preview
displayed both independently chosen colours and the 1.0 mm transition.

**Open**. Compare silhouettes and check legibility, flex and page thickness with a physical
sample. Check toolpath colour and change count with the chosen slicer profile before printing.
All four drafts are in Development and await owner review.
