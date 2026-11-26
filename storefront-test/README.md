# Storefront test

A test of the new snapmagicians.in custom order page: a customer types a name, picks the name colour and
sees the real Letter Contour organizer in 3D, rendered by SnapSTL's Models renderer (`model_studio`).

* `render_server.py` serves `index.html` and a small public API in front of `model_studio.preview`.
  Customers set only the text and the name colour (SnapSTL's filament colours, `farm.COLOR_HEX`);
  every other parameter stays at the design's own values. It never exposes the SnapSTL portal or its data.
  Add to bag writes a test order to `test-orders.jsonl` (what SnapSTL would receive).
* `run.sh` runs it on this Mac (`./run.sh --lan` to open it on a phone on the same Wi-Fi).
  Needs the OpenSCAD development build with textmetrics.
* The internet test runs on an AWS Lightsail server (Ubuntu, `openscad-nightly`, Caddy with a password);
  it clones this branch and runs `render_server.py` the same way.
