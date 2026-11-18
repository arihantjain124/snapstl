// SnapSTL name desk organizer — original design (snapstl-v1), written for SnapSTL.
//
// Two parts, printed separately and glued:
//   part = "base"  compartment box, upright: X = length, Y = depth, Z = height, front face at -Y.
//                  The front face has a shallow recess in the shape of the name piece (glue guide).
//   part = "name"  the name with an outline around it, flat on the bed: X = length, Y = letter height,
//                  Z = name_t (4 mm): a backing plate with the letters raised `relief` mm above it.
//   part = "assembled"  both together, for pictures only (never printed).
//
// The base is sized from the name piece (textmetrics), so the two always match:
//   base length = name length + 2 × margin, base height = name height + 2 × margin (at least min_base_h,
//   the name centred). Long names shrink the text so the name piece stays ≤ max_plate_w.
// SnapSTL then scales both with Scaling Rules v1.1 (Base X = Name X, Base Z = Name Y).
//
// Run:  openscad -o base.stl -D 'name="Maalu"' -D 'compartments=5' -D 'part="base"' organizer.scad
// Fonts: set OPENSCAD_FONT_PATH to the folder holding the font files.

/* [Name] */
name = "Maalu";
font = "Chewy";          // Chewy | Luckiest Guy | Titan One | DynaPuff | Cherry Bomb One | Akaya Kanadaka
font_size = 60;          // 30–120: text size in mm (before SnapSTL's scaling)
thickness = 50;          // 30–120: letter weight; 50 = the font as drawn, higher = bolder
outline = 6;             // 4–15: border around the letters in mm
rotate_text = "no";      // "yes" tilts the name by tilt_deg
compartments = 5;        // 1–10 (SnapSTL takes this from the scaling table)
part = "assembled";      // base | name | assembled

/* [Fixed shape] */
name_t = 4;              // name piece thickness (letters' top)
relief = 1.6;            // letters stand this far above the backing plate
margin = 6;              // base reaches this far past the name piece on every side
depth = 50;              // base depth (Y)
wall = 2.4;              // outer walls
floor_t = 2.4;           // base floor
divider = 2;             // compartment dividers
divider_drop = 8;        // dividers stop this far below the top edge
corner_r = 4;            // rounded vertical corners of the base
recess = 0.6;            // depth of the glue-guide recess in the front face
bridge = 3;              // closes small gaps so the name piece is one solid part
tilt_deg = 6;
max_plate_w = 228;       // long names: text shrinks so the name piece is at most this long
min_base_h = 86;         // the base is never lower than this (keeps scaling close to 1:1)
min_aspect = 1.9;        // short names: letter spacing widens until the name piece is this many times
max_spacing = 1.6;       //   longer than high (up to max_spacing), so scaling doesn't stretch the letters

$fa = 4;
$fs = 0.4;

assert(len(name) > 0, "name is empty");
assert(compartments >= 1 && compartments <= 10, "compartments must be 1–10");

// Text size: font_size, or smaller when the name piece would be longer than max_plate_w.
tm0 = textmetrics(name, size = font_size, font = font, halign = "center", valign = "baseline");
fs0 = font_size * 0.03 * (thickness - 50) / 50;      // stroke growth at full size (scales with size)
w0 = tm0.size.x + 2 * (max(fs0, 0) + outline);
fs = w0 <= max_plate_w ? font_size
     : font_size * (max_plate_w - 2 * outline) / (tm0.size.x + 2 * max(fs0, 0));
weight = (thickness - 50) / 50 * fs * 0.03;          // mm added (or removed) around each stroke
tm1 = textmetrics(name, size = fs, font = font, halign = "center", valign = "baseline");
// Short names: spread the letters (spacing) rather than letting SnapSTL's scaling stretch them.
h1 = tm1.size.y + 2 * (max(weight, 0) + outline);
need_w = min_aspect * h1 - 2 * (max(weight, 0) + outline);
spacing = (len(name) < 2 || tm1.size.x >= need_w) ? 1
          : min(max_spacing, 1 + (need_w - tm1.size.x) / (tm1.advance.x * (len(name) - 1) / len(name)));
tm = textmetrics(name, size = fs, font = font, spacing = spacing, halign = "center", valign = "baseline");
tilt = rotate_text == "yes" ? tilt_deg : 0;

// Unrotated plate box (text box grown by weight + outline), then its bounds after the tilt.
grow = max(weight, 0) + outline;
bx0 = tm.position.x - grow;  bx1 = tm.position.x + tm.size.x + grow;
by0 = tm.position.y - grow;  by1 = tm.position.y + tm.size.y + grow;
corners = [for (p = [[bx0, by0], [bx1, by0], [bx1, by1], [bx0, by1]])
           [p.x * cos(tilt) - p.y * sin(tilt), p.x * sin(tilt) + p.y * cos(tilt)]];
px0 = min([for (c = corners) c.x]);  px1 = max([for (c = corners) c.x]);
py0 = min([for (c = corners) c.y]);  py1 = max([for (c = corners) c.y]);
plate_w = px1 - px0;
plate_h = py1 - py0;

base_l = plate_w + 2 * margin;
base_h = max(plate_h + 2 * margin, min_base_h);

module letters2d() {
    rotate(tilt)
        if (weight >= 0) offset(r = weight) text(name, size = fs, font = font, spacing = spacing, halign = "center", valign = "baseline");
        else offset(delta = weight) text(name, size = fs, font = font, spacing = spacing, halign = "center", valign = "baseline");
}

// Name piece outline, centred on the origin.
module plate2d() {
    translate([-(px0 + px1) / 2, -(py0 + py1) / 2])
        offset(r = -bridge) offset(r = outline + bridge) { letters2d(); spine2d(); }
}

// A bar through the lower half of the letters, inside the text box: keeps the backing plate one
// piece however far apart the letters are (it only shows as part of the outline between letters).
module spine2d() {
    rotate(tilt) translate([tm.position.x + fs * 0.1, fs * 0.12]) square([max(tm.size.x - fs * 0.2, 0.1), fs * 0.22]);
}

// Letters only, in the same place as plate2d().
module letters_centred2d() {
    translate([-(px0 + px1) / 2, -(py0 + py1) / 2]) letters2d();
}

// Backing plate (name_t - relief) with the letters raised to name_t, so they read in one colour.
module name_piece() {
    translate([plate_w / 2, plate_h / 2, 0]) {
        linear_extrude(name_t - relief) plate2d();
        linear_extrude(name_t) letters_centred2d();
    }
}

module rounded_rect(l, d, r) {
    offset(r = r) offset(delta = -r) square([l, d]);
}

module base() {
    inner_l = base_l - 2 * wall;
    cell = (inner_l - (compartments - 1) * divider) / compartments;
    difference() {
        linear_extrude(base_h) rounded_rect(base_l, depth, corner_r);
        // hollow
        translate([0, 0, floor_t])
            linear_extrude(base_h) translate([wall, wall]) rounded_rect(inner_l, depth - 2 * wall, max(corner_r - wall, 0.5));
        // glue guide on the front face (-Y)
        translate([base_l / 2, recess, base_h / 2]) rotate([90, 0, 0]) linear_extrude(recess + 0.01) plate2d();
    }
    // dividers
    if (compartments > 1) for (i = [1 : compartments - 1])
        translate([wall + i * cell + (i - 1) * divider, wall - 0.01, 0])
            cube([divider, depth - 2 * wall + 0.02, base_h - divider_drop]);
}

if (part == "base") base();
else if (part == "name") name_piece();
else {  // assembled: name standing in its recess
    color("white") base();
    color("deeppink") translate([margin, recess - 0.01, (base_h - plate_h) / 2]) rotate([90, 0, 0]) name_piece();
}

echo(str("SNAPSTL text_size=", fs, " spacing=", spacing, " plate_w=", plate_w, " plate_h=", plate_h, " base=", base_l, "x", depth, "x", base_h));
