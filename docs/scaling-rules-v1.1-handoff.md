# Snap Magicians --- 3MF Scaling & Batch Generation Handoff

**Document purpose:** Carry the decisions and workflow from the previous
session into a fresh ChatGPT session.\
**Project:** Personalized name organizers / separate name pieces,
prepared as 3MF files for Bambu Lab printing.\
**Rules version:** Scaling Rules v1.1 (latest agreed version in this
chat).\
**Status:** Scaling rules are agreed. The latest combined Hot Pink file
was generated and structurally checked, but still needs visual
inspection and slicing in Bambu Studio before being treated as
production-verified.

------------------------------------------------------------------------

## 1. Project goal

Create a repeatable workflow that takes individual customer
name-organizer 3MF input files, scales each organizer base and its
matching separate name piece, and combines three orders into a
multi-plate 3MF project for faster batch printing in Bambu Studio.

The product is personalized, but customers pay the same price. The goal
is therefore a consistent experience: each organizer should look
balanced, substantial, functional, and well-finished regardless of the
number of letters in the name.

The workflow should avoid manually opening one file, importing two more,
arranging, resizing, and preparing the plate for every batch.

## 2. Input files and model pairing

-   The user supplies customer-specific 3MF files, in the style of the
    previously supplied examples.
-   Each order has an organizer **Base** and a separate matching
    **Name** piece.
-   Keep each name paired with its own base throughout scaling and plate
    arrangement.
-   The scaling calculation must be based on the original, unscaled
    geometry---not on a previously scaled output.
-   Before processing, inspect the input 3MF and confirm that it
    actually contains the expected base and name meshes. Do not assume a
    file is valid merely because it has a `.3mf` extension.
-   If a file is missing a model, contains only one of the two parts, or
    has unexpected geometry, stop and report the issue rather than
    silently generating an incomplete batch.

**Important:** The exact internal structure of future input 3MF files
may vary. Inspect the actual files supplied in each batch and identify
the correct Base and Name objects before applying transforms. Do not
blindly assume object IDs or mesh paths are identical across files.

## 3. Final agreed scaling rules --- v1.1

### 3.1 Target dimensions by number of letters

These are target ranges/guidelines, not rigid dimensions. Choose a
suitable value within the range after checking the original geometry and
visual proportions.

  -----------------------------------------------------------------------
  Name length           Target organizer length   Target organizer height
                                            (X)                       (Z)
  ------------------- ------------------------- -------------------------
  3 letters                         190--200 mm                     90 mm

  4 letters                         195--210 mm                 89--90 mm

  5 letters                         200--220 mm                 88--90 mm

  6--7 letters                      215--230 mm                 86--89 mm

  8--10 letters                     225--240 mm                 85--88 mm

  11+ letters                    Maximum 240 mm                 85--88 mm
  -----------------------------------------------------------------------

Hard/priority constraints: - Finished organizer height target must
remain within **85--90 mm**. - For short names (3, 4, or 5 letters), use
a length of **190+ mm** where appropriate and keep height toward the
larger end of the range. This compensates for the shorter name without
making it look excessively stretched. - Longer names should generally
receive more length, with height toward the lower end of the allowed
range. - Do not force an exact length if it creates poor proportions or
damages the geometry. - The original geometry, letter widths,
readability, and overall appearance take priority over blindly following
letter count. - The earlier rule of keeping length around 20--24 cm and
height around 8--9 cm has been refined by this v1.1 table.

### 3.2 Axis relationships between Base and Name

These relationships were explicitly tested by the user and are important
for the separate pieces to fit when glued:

  Axis / scale   Organizer Base               Separate Name
  -------------- ---------------------------- ---------------------------------
  X scale        Dynamically calculated       **Must match Base X scale**
  Y scale        **100% --- do not change**   **Must match Base Z scale**
  Z scale        Dynamically calculated       **50% --- fixed for all names**

The critical rules are:

-   **Base X scale = Name X scale**
-   **Base Z scale = Name Y scale**
-   **Base Y scale = 100%**
-   **Name Z scale = 50%**

Do not confuse the axis labels: the user specifically decided that Base
Z must match Name Y, not Name Z. Name Z is always reduced to 50%; this
controls letter thickness/height in that axis and is not the
fit-matching axis.

### 3.3 Scaling calculations

Use the original Base dimensions to calculate the scale factors:

``` text
Base X scale (%) = (Target organizer length / Original Base X length) × 100

Base Z scale (%) = (Target organizer height / Original Base Z height) × 100

Base Y scale (%) = 100%

Name X scale (%) = Base X scale (%)

Name Y scale (%) = Base Z scale (%)

Name Z scale (%) = 50%
```

Apply the calculated percentages to the correct axes of the
corresponding objects. Check the actual coordinate orientation of each
input model before applying transforms; do not assume that the model's
visual "height" is always the same world axis without inspecting its
geometry.

### 3.4 Example from the latest Hot Pink batch

The following targets were used for the three earlier files:

  Name          Letters   Target length   Target height
  ----------- --------- --------------- ---------------
  Ayra                4          200 mm           90 mm
  Manvi               5          210 mm           89 mm
  Aishwarya           9          235 mm           86 mm

The reported scale percentages for that batch were:

  Name          Base X = Name X   Base Y   Base Z = Name Y   Name Z
  ----------- ----------------- -------- ----------------- --------
  Aishwarya              65.10%     100%            87.53%      50%
  Ayra                  116.56%     100%            93.51%      50%
  Manvi                 101.65%     100%           118.95%      50%

These percentages are specific to those original input meshes.
Recalculate them for every new input file; do not reuse them for other
names.

------------------------------------------------------------------------

## 4. Batch 3MF generation and plate layout

### Intended batch structure

-   Process **three customer orders** in one batch.
-   Each order contributes two separate parts: one Base and its matching
    Name.
-   The intended multi-plate arrangement is:
    -   **Plate 1:** the three organizer Bases.
    -   **Plate 2:** the three matching Name pieces.
-   Preserve clear pairing in object names/metadata (for example,
    `Ayra - Base` and `Ayra - Name`) so the pieces can be identified
    after import.
-   Keep each part fully inside the build area, with no collisions or
    unintended overlap.
-   Do not omit any of the six parts. A valid three-order batch should
    visibly contain all three bases and all three names.

### Required output checks

Before returning a generated file:

1.  Confirm all three input orders were read successfully.
2.  Confirm each order has both a Base and a Name mesh.
3.  Confirm all six expected parts are present in the output project.
4.  Confirm every Name is paired with the correct Base.
5.  Confirm all object/component references and mesh paths inside the
    3MF package resolve.
6.  Confirm transforms apply the v1.1 scaling rules.
7.  Confirm the parts are positioned within the printer build area and
    do not overlap.
8.  Confirm the project opens and displays correctly in Bambu Studio.
9.  Inspect both plates and slice them before printing.

A ZIP/package-level integrity check or valid object references do
**not** prove that the project will render or slice correctly in Bambu
Studio. Visual verification in the slicer remains necessary.

### Previous generation issues to avoid

Earlier attempts encountered these problems: - A sample 3MF was empty
and contained no visible models. - Another output showed only one name
when the batch was expected to contain all names. - A sample 3MF
download was unavailable. - The latest Hot Pink batch was generated with
six mesh references and structural checks, but Bambu Studio visual
rendering/slicing was not confirmed in the previous session.

Therefore, do not declare a batch complete solely because a file was
written successfully. Reopen/inspect the generated 3MF and verify all
six parts and both plates.

------------------------------------------------------------------------

## 5. Geometry, fit, and quality checks

The user wants a consistent product experience, not merely matching
numerical dimensions. Check:

-   Organizer length and height against the target table.
-   Base and Name scale relationships exactly as defined above.
-   Glue-contact/alignment surfaces remain compatible.
-   Name pieces remain readable and visually balanced.
-   Compartments remain usable.
-   Wall thickness and narrow features remain printable after scaling.
-   No excessive stretching or compression of individual letters.
-   The model fits the printer build area.
-   The parts are not accidentally buried, off-plate, or hidden in the
    project.

If a name's original geometry makes the target dimensions unsuitable,
flag it and explain the conflict instead of silently violating the fit
rules.

## 6. Bambu Lab / AMS context

The user prints with Bambu Lab printers and prepares files in Bambu
Studio. The earlier automation work involved a print-version selection
with options `AMS` and `NO AMS`; selecting `NO AMS` manually worked
after the automation had failed to detect the option in a Material UI
dropdown. This is background from the earlier print workflow, not a
scaling rule.

For the 3MF model-generation task: - Do not change print profiles,
filament assignments, or AMS settings unless the user asks. - Preserve
the supplied project settings where possible. - The user previously
indicated that the combined output is intended for a production
workflow, so the final file should be ready for inspection/slicing
rather than being only a conceptual example.

## 7. Expected workflow for each new batch

1.  Receive the three customer 3MF input files (or the number of files
    specified by the user).
2.  Inspect each file and identify its Base and Name objects and
    original dimensions.
3.  Count the letters in each name.
4.  Select target length and height using the v1.1 table, considering
    original geometry and visual balance.
5.  Calculate Base X and Base Z percentages from original Base geometry.
6.  Apply:
    -   Base X = calculated X
    -   Base Y = 100%
    -   Base Z = calculated height scale
    -   Name X = Base X
    -   Name Y = Base Z
    -   Name Z = 50%
7.  Keep each Base and Name paired.
8.  Build the multi-plate 3MF: three Bases on Plate 1 and three Names on
    Plate 2.
9.  Validate the package, mesh references, object count, transforms,
    dimensions, plate bounds, and object positions.
10. Open the result in Bambu Studio and verify that all models are
    visible and correctly placed.
11. Return the combined 3MF and a scaling report showing original
    dimensions, target dimensions, and applied percentages.
12. Clearly disclose anything not verified (especially slicer rendering
    and slicing).

## 8. Output/report format

For every batch, provide: - One combined 3MF project. - One CSV or
Markdown scaling report.

The report should include, for each name: - Name and letter count. -
Input filename. - Original Base X/Y/Z dimensions. - Target organizer
length and height. - Applied Base X, Base Y, Base Z percentages. -
Applied Name X, Name Y, Name Z percentages. - Any geometry/fit
warning. - Validation status (package integrity, six-part completeness,
plate bounds, and Bambu Studio visual/slice verification).

## 9. Important distinction: agreed rules vs. items to inspect

**Agreed and binding:** - v1.1 target dimension table. - Short 3--5
letter names should be at least about 190 mm long where appropriate and
use the larger height range. - Finished target height 85--90 mm. - Base
X = Name X. - Base Z = Name Y. - Base Y stays at 100%. - Name Z stays at
50%. - Three Bases on Plate 1 and their three matching Names on Plate 2
for a three-order batch.

**Must be inspected per input / not assumed:** - Actual object IDs and
mesh paths. - Coordinate orientation and original dimensions. - Whether
each file contains both expected parts. - Exact placement coordinates
and clearances. - Whether the final file opens, renders, and slices
correctly in Bambu Studio. - Any printer-specific build-volume or
profile details not supplied with the current batch.

------------------------------------------------------------------------

## 10. Fresh-session starting instruction

When continuing this project in a new session, use this document as the
project specification. Ask the user to upload the current customer 3MF
input files if they are not attached in that session. Inspect the actual
files, apply Scaling Rules v1.1 exactly, generate the combined
multi-plate 3MF and scaling report, and verify that all expected models
are present. Do not invent missing input geometry or claim Bambu Studio
validation unless it has actually been performed.
