// SnapSTL organizer. Both parts are generated from the same text profile.
use <fonts/AkayaKanadaka-Regular.ttf>
use <fonts/CherryBombOne-Regular.ttf>
use <fonts/Chewy-Regular.ttf>
use <fonts/DynaPuff[wdth,wght].ttf>
use <fonts/LuckiestGuy-Regular.ttf>
use <fonts/TitanOne-Regular.ttf>

part = "name";
text = "AaLiQ";
font = "Chewy";
font_size = 60;
thickness = 50;
outline = 6;
rotate_text = "no";
clearance = 0.30;
minimum_rim = 0;
compartments = 5;
base_length = 210;
base_height = 89;

// SnapSTL measures the exported name before generating the base so rotated text and
// each font's actual contour share exactly the same coordinates.
label_left = undef;
label_bottom = undef;
label_width = undef;
label_height = undef;
floor = 3;
wall = 3;
front_wall = 2.9;
recess = 1;
insert = 4;
epsilon = 0.002;
$fa = 4;
$fs = 0.25;

assert(compartments >= 1 && compartments <= 10, "Choose 1–10 compartments");
assert(len(text) > 0 && clearance > 0 && outline > 0 && thickness > front_wall + wall);

module letters() {
    rotate(rotate_text == "yes" ? 6 : 0)
        text(text, size=font_size, font=font);
}

module outline_shape() {
    offset(r=outline) letters();
}

module bottom_rail(rail_left, rail_width, bottom) {
    nominal_height = label_height + 2*outline;
    // MakerWorld continues the floor up through gaps to 66% of the raw base height.
    // A development override can raise this further in finished millimetres above the floor.
    lip = max(0.66*nominal_height, floor + minimum_rim * nominal_height/base_height);
    if (rail_width > label_width + epsilon) {
        // Narrow names need shoulders to preserve useful compartments on both ends.
        translate([rail_left, bottom])
            offset(r=outline) offset(delta=-outline)
                square([rail_width, max(3*outline, 0.60*label_height + outline)]);
        translate([rail_left, bottom]) square([rail_width, lip]);
    } else {
        translate([rail_left, bottom]) square([rail_width, lip]);
    }
}

module silhouette(rail_left, rail_width, bottom, sweep) {
    union() {
        outline_shape();
        // Continue the contour down to a flat floor within the width of the name.
        intersection() {
            translate([label_left,bottom]) square([label_width, sweep]);
            minkowski() {
                outline_shape();
                translate([-epsilon/2,-sweep]) square([epsilon,sweep]);
            }
        }
        bottom_rail(rail_left, rail_width, bottom);
    }
}

module base() {
    assert(!is_undef(label_width) && !is_undef(label_height) && !is_undef(label_left)
           && !is_undef(label_bottom), "Export the name and pass its bounds before rendering the base.");
    bottom = label_bottom-outline;
    nominal_height = label_height+2*outline;
    rail_width = max(label_width, base_length*nominal_height/(base_height*1.5)-2*outline,
                     (compartments+1)*wall + compartments*4);
    rail_left = label_left-(rail_width-label_width)/2;
    cell = (rail_width-(compartments+1)*wall)/compartments;
    // Retain the measured allowance on ordinary names. Keep even unusually compressed
    // names above the assembly check's minimum clearance after Scaling v1.1.
    raw_clearance = max(clearance, 0.14*max((rail_width+2*outline)/base_length, nominal_height/base_height));
    difference() {
        translate([0,thickness,0]) rotate([90,0,0])
            linear_extrude(thickness) silhouette(rail_left,rail_width,bottom,nominal_height+1);
        for (i = [0:compartments-1])
            translate([rail_left+wall+i*(cell+wall), front_wall, bottom+floor])
                cube([cell,thickness-front_wall-wall,nominal_height+1]);
        translate([0,recess,0]) rotate([90,0,0]) linear_extrude(recess+0.05)
            offset(r=raw_clearance) letters();
    }
}

if (part == "base") base();
else if (part == "name") linear_extrude(insert) letters();
else assert(false, "Choose base or name");
