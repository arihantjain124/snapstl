// All dimensions are in millimetres. The flat back prints at Z = 0.
use <fonts/Chewy-Regular.ttf>

part = "bookmark";
text = "Chapter One";
length = 120;
width = 28;
base_thickness = 1;
letter_height = 7;
letter_relief = 0.6;

font_name = "Chewy";
$fn = 64;

module rounded_body(radius) {
    hull()
        for (x = [radius, width - radius], y = [radius, length - radius])
            translate([x, y]) circle(r = radius);
}

module arch_cutout() {
    bridge = 3.5;
    radius = width / 2 - bridge;
    center_y = length - width / 2;
    hull() {
        translate([width / 2, center_y]) circle(r = radius);
        translate([bridge, center_y - 6]) square([2 * radius, 1]);
    }
}

module silhouette(style) {
    if (style == "classic-round") {
        rounded_body(4);
    } else if (style == "ribbon-tail") {
        difference() {
            rounded_body(2);
            polygon([[width * 0.16, -1], [width * 0.84, -1], [width / 2, 9]]);
        }
    } else if (style == "open-arch") {
        difference() {
            hull() {
                for (x = [3, width - 3]) translate([x, 3]) circle(r = 3);
                translate([width / 2, length - width / 2]) circle(d = width);
            }
            arch_cutout();
        }
    } else if (style == "faceted-tab") {
        polygon([[3, 0], [width - 3, 0], [width, 3], [width, length - 13],
                 [width - 7, length], [7, length], [0, length - 13], [0, 3]]);
    } else {
        assert(false, "Unknown bookmark style");
    }
}

module inscription(style) {
    // Measure the actual glyphs so descenders and accents stay inside the
    // inscription area. Turn the line along the length of the bookmark.
    sample_size = 10;
    ink = textmetrics(text, size = sample_size, font = font_name);
    low = style == "ribbon-tail" ? 13 : 5;
    high = style == "open-arch" ? length - width / 2 - 10 : length - 7;
    assert(ink.size.x > 0 && ink.size.y > 0, "Enter text with visible letters or symbols.");
    fit = min((high - low) / ink.size.x, letter_height / ink.size.y);
    center = ink.position + ink.size / 2;

    // A small overlap joins each glyph to the base. At Z = base_thickness
    // the broad face ends and only the inscription continues upward.
    overlap = 0.04;
    translate([width / 2, (low + high) / 2, base_thickness - overlap])
        linear_extrude(height = letter_relief + overlap)
            rotate(90)
                scale([fit, fit])
                    translate(-center)
                        text(text, size = sample_size, font = font_name);
}

module bookmark(style) {
    assert(part == "bookmark", "Unknown bookmark part");
    union() {
        linear_extrude(height = base_thickness) silhouette(style);
        inscription(style);
    }
}
