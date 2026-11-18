// Print coordinates in mm. The bezel back, backing plate and inside face of the stand rest on the bed.
use <fonts/AkayaKanadaka-Regular.ttf>
use <fonts/CherryBombOne-Regular.ttf>
use <fonts/Chewy-Regular.ttf>
use <fonts/DynaPuff[wdth,wght].ttf>
use <fonts/LuckiestGuy-Regular.ttf>
use <fonts/TitanOne-Regular.ttf>

part = "frame";
personalize = true;
text = "Good Times";
font = "Chewy";
text_position = "Bottom";
max_letter_height = 9;
photo_clearance = 0.3;
pin_clearance = 0.2;

photo = [101.6, 152.4];
border = 17;
frame_size = photo + [2 * border, 2 * border];
frame_depth = 7;
cover_inset = [2, 4];
cover_size = frame_size - 2 * cover_inset;
cover_depth = 3;
screw_offset = [5, 3.5];

hinge_height = 116;
hinge_depth = 9;
stand_width = 28;
side_gap = 0.35;
ear_width = 4.5;
axis_y = 5;
axis_z = 5;
knuckle_radius = 4.4;
stand_depth = 4;
open_angle = 30;
lean_angle = 17;
angle_difference = open_angle - lean_angle;
pivot_height = hinge_height * cos(lean_angle) - hinge_depth * sin(lean_angle);
stop_y = ((cover_depth - hinge_depth) + axis_z * cos(open_angle)) / sin(open_angle);

eps = 0.02;
$fn = 64;

module rounded_rectangle(size, radius) {
    hull()
        for (x = [radius, size.x - radius], y = [radius, size.y - radius])
            translate([x, y]) circle(r = radius);
}

module rebate() {
    pocket = photo + [2 * photo_clearance, 2 * photo_clearance];
    reveal = photo - [7, 7];
    seat_depth = 0.8;
    bevel_top = seat_depth + 3.5 + photo_clearance;
    // The 45-degree bevel closes the rear pocket toward the front window.
    translate([frame_size.x / 2, frame_size.y / 2, 0]) {
        translate([0, 0, -eps])
            linear_extrude(height = seat_depth + 2 * eps) square(pocket, center = true);
        hull() {
            translate([0, 0, seat_depth])
                linear_extrude(height = eps) square(pocket, center = true);
            translate([0, 0, bevel_top])
                linear_extrude(height = eps) square(reveal, center = true);
        }
        translate([0, 0, bevel_top])
            linear_extrude(height = frame_depth - bevel_top + eps) square(reveal, center = true);
    }
}

module screw_pattern(size, diameter, depth) {
    for (x = [screw_offset.x, size.x - screw_offset.x],
         y = [screw_offset.y, size.y - screw_offset.y])
        translate([x, y, -eps]) cylinder(d = diameter, h = depth + eps);
}

module inscription() {
    sample = 10;
    ink = textmetrics(text, size = sample, font = font);
    assert(ink.size.x > 0 && ink.size.y > 0, "Enter text with visible letters or symbols.");
    factor = min((frame_size.x - 20) / ink.size.x, max_letter_height / ink.size.y);
    baseline_y = text_position == "Top" ? frame_size.y - 9 : 9;
    translate([frame_size.x / 2, baseline_y, frame_depth - eps])
        linear_extrude(height = 0.65 + eps)
            scale([factor, factor])
                translate(-ink.position - ink.size / 2)
                    text(text, size = sample, font = font);
}

module frame() {
    difference() {
        linear_extrude(height = frame_depth) rounded_rectangle(frame_size, 5);
        rebate();
        translate(cover_inset) screw_pattern(cover_size, 2.65, 4);
    }
    if (personalize) inscription();
}

// Extrude a side profile in Y/Z along the hinge axis (X).
module side_profile(x, y, width) {
    translate([x, y, 0])
        rotate([90, 0, 90]) linear_extrude(height = width) children();
}

module pin_socket(z) {
    r = 1.5 + pin_clearance;
    translate([0, z]) union() {
        circle(r = r);
        roof = r / sqrt(2);
        polygon([[-roof, roof], [roof, roof], [0, 2 * roof]]);
    }
}

module knuckle(base_z, pin_z, base_low, base_high) {
    difference() {
        hull() {
            translate([base_low, base_z]) square([base_high - base_low, eps]);
            translate([0, pin_z]) circle(r = knuckle_radius);
        }
        pin_socket(pin_z);
    }
}

module backing() {
    difference() {
        linear_extrude(height = cover_depth) rounded_rectangle(cover_size, 3);
        screw_pattern(cover_size, 3.4, cover_depth + eps);
    }
    hinge_y = frame_size.y - cover_inset.y - hinge_height;
    left_ear = cover_size.x / 2 - stand_width / 2 - side_gap - ear_width;
    right_ear = cover_size.x / 2 + stand_width / 2 + side_gap;
    for (x = [left_ear, right_ear])
        side_profile(x, hinge_y, ear_width)
            knuckle(cover_depth - eps, hinge_depth, -6, 7);
}

function foot_y(z) = (pivot_height + (z - axis_z) * sin(angle_difference)) / cos(angle_difference);

module stand() {
    side_profile(0, axis_y, stand_width) {
        knuckle(0, axis_z, stop_y, 9);
        // The stop corner on the bed face meets the back at 30 degrees.
        // The sloped end matches the desk in the intended 17-degree lean.
        polygon([[6, 0], [foot_y(0), 0],
                 [foot_y(stand_depth), stand_depth], [6, stand_depth]]);
    }
}

if (part == "frame") frame();
else if (part == "backing") backing();
else if (part == "stand") stand();
else assert(false, "Unknown picture frame part");
