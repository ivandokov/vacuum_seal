# Vacuum seal gasket, seamless TPU print

## Goal

Print a vacuum seal gasket for a pneumatic speargun in TPU (Filaflex 82A) with no seam at all. The gasket must be one continuous extrusion line, because a start/stop seam on the bore or the outer wall breaks the water seal. A reference part exists (the user has it physically, photos were removed from the project) that was printed this way: 3 visible walls at the small top opening and about 6 to 8 walls at the base. The reference was almost certainly printed with a 0.4 mm nozzle. The user started with a 0.6 mm nozzle for strength, compared both, and now always uses the 0.4 mm nozzle for its finish. Wall count is not important as long as no layer drops to a single wall.

## Files

- `Seal.stl`: the CAD model (binary STL, axis Z through the origin, flange at z 0). The user re exports it as the design evolves; the generator always reads the current file. Third version (2026-09-24): 13.1 mm tall, flange r 5.15 to 8.10 mm up to about z 4.5, then a uniform 1.30 mm wall curving inward to the 6 mm bore (r 3.0, outer r 4.30) at the top. With the 0.4 nozzle that is 7 rings at the flange and 3 rings of 0.43 mm on the wall. Earlier versions: 13.5 mm tall with a 1.25 mm wall, then thickened to 2.0 to 2.4 mm.
- `template.4mm.gcode.3mf`: BambuStudio 02.08 slice of the part. X1 Carbon, 0.4 nozzle, Filaflex 82A profile (246 C, first layer 240 C, bed 35 C textured plate), part center on the plate 128, 128. This file has seams (retraction plus wipe on every loop) and is only used as a template for start and end gcode, temperatures, fan schedule and bed position. The 0.6 nozzle template and output were removed once the user settled on the 0.4 nozzle.
- `Output.4mm.gcode.3mf` (0.4 nozzle, 0.08 mm layers, 252 C): generated seamless file, ready to print. Only the 3mf is written; add `--plain-gcode` to also get a plain .gcode for inspection (or read it with `unzip -p <file> Metadata/plate_1.gcode`). Regenerated whenever the STL or options change.
- `generate.py`: generator. Pure Python 3, no dependencies (numpy and matplotlib are not installed on this machine).

## How the generator works

```
python3 generate.py --stl Seal.stl --template template.4mm.gcode.3mf --output Output.4mm.gcode.3mf --layer-height 0.08 --first-layer-height 0.16 --temp 252
```

Add `--count 3` (or any number) for several copies on the plate; they are printed one whole object after another on a grid around the template position, never layer by layer, so each object keeps the continuous path. Between objects there is one retract, a lift, a travel and an unretract.

It reads the 2D profile from the STL (half plane intersection), and from the template 3mf takes the machine start and end gcode, nozzle size, temperatures, per layer fan commands and the bed position (part center 128, 128 in the 0.4 template). Layer height comes from the template unless overridden, and is fitted so the last layer lands exactly on the model top. It writes a new plate_1.gcode, updates the md5 (the printer checks it) and slice_info prediction and weight, then repacks the 3mf. The template can be reused as long as part height and bed position are unchanged.

Toolpath per layer, rings numbered from the outside in:
- Rings are concentric circles, all printed with the extruder running. Flat rings close with a flow scarf: flow ramps up over the first `--scarf` mm (default 2) and ramps down over the same arc at the end while overlapping it. Then a short radial jog inside the wall to the next ring at reduced flow (`--jog-flow`, default 0.3).
- One visible ring is a continuous helix with no closing point at all. Default is the bore (`--helix bore`), the dynamic sealing surface on the shaft. `--helix outer` moves the helix to the outer surface instead. Only one ring per layer can be a helix.
- The helix segment of each layer spans one turn plus `--rotate` degrees (default 137.5, golden angle) while climbing one layer height, so its pitch is layer height / 1.382 and its departure point, the flat ring closures and the internal connector all rotate around the part every layer. Nothing stacks into a vertical line, so no leak channel can form. The connector column at a fixed angle in the first print was visible as a seam and is why this rotation was added.
- The bore bead volume is computed from a tracked surface height per angle (BoreSurface), so the helix start over the flat first layer, the fine pitch, and the flat last layer over the helix are all filled exactly and the rim ends flat.
- Return to the next layer's first ring is a radial move through the wall interior, one layer above the rings it crosses.
- No retraction, no travel, no z hop, no wipe anywhere. Z never decreases. Verified by parsing the output (a checker script is in the session history; it counts non extruding moves, negative E and z decreases between the machine start and end markers).

Ring count per layer: wall thickness divided by nominal line width (1.03 x nozzle), width clamped between 0.83 and 1.35 x nozzle. Current STL (third version) with the 0.4 nozzle: 7 rings at the flange, 3 rings of 0.43 mm on the 1.30 mm wall. Never 1.

Speeds: per layer from a minimum layer time, clamped between a minimum and maximum speed. Defaults below.

### Script flags

Each flag with its default and meaning:

- `--stl PATH`: binary STL of the part, revolve axis Z through the origin, flange at z 0. Required.
- `--template PATH`: BambuStudio sliced .gcode.3mf for the target printer, nozzle and filament. Supplies start and end gcode, nozzle size, temperatures, fan schedule and bed position. Required.
- `--output PATH`: output .gcode.3mf. Required. Naming convention here: `Output.<nozzle>mm.gcode.3mf`.
- `--plain-gcode`: also write a plain .gcode next to the 3mf, for inspection. Off by default.
- `--layer-height MM`: layer height. Default is the template's. Fitted so the last layer lands exactly on the model top. Used: 0.08 (0.10 was the practical floor on the 0.6 nozzle).
- `--first-layer-height MM`: first layer height. Default is the template's. Used: 0.16.
- `--line-width MM`: nominal line width. Default 1.03 x nozzle. Ring count per layer is wall thickness divided by this, then width is clamped between 0.83 and 1.35 x nozzle.
- `--helix {bore,outer}`: which visible surface is the continuous helix with no closure. Default `bore` (the shaft contact surface). The other surface gets scattered, flow ramped closures.
- `--rotate DEG`: how far the helix overshoots one turn each layer. Default 137.5 (golden angle). Rotates every ring closure and the internal connector around the part so nothing stacks into a line. Also sets the helix pitch: layer height / (1 + rotate/360).
- `--scarf MM`: length over which a flat ring ramps flow up at its start and down at its end while overlapping the start. Default 2.0. Raise to 4 if closures are visible.
- `--jog-flow FACTOR`: flow factor on the short radial connections inside the wall. Default 0.3. Lower to 0.15 to put less material at the connector corners.
- `--min-layer-time S`: target minimum time per layer, sets the layer speed. Default 10.
- `--max-speed MM/S`: speed ceiling. Default 25.
- `--min-speed MM/S`: speed floor. Default 6. The thin neck runs at the floor.
- `--first-layer-speed MM/S`: speed ceiling for the first layer. Default 18.
- `--accel MM/S2`: print acceleration (M204). Default 1500.
- `--temp C`: nozzle temperature from layer 2 on. Default is the template's (246 for Filaflex 82A). Used: 252 on the 0.4 nozzle file after the user saw small imperfections that looked like micro clogs or moisture. Profile range is 200 to 265.
- `--first-layer-temp C`: first layer nozzle temperature, replaces the template's initial layer temp (240) in the machine start gcode. Default is `--temp` if given, else the template's.
- `--count N`: number of copies on the plate. Default 1. Printed sequentially, one whole object at a time, on a grid (columns = ceil(sqrt(N))) centered on the template position. Progress (M73) and the header layer count cover all objects. Fan off for the first two layers and fan on at layer 3 are repeated per object. The script refuses layouts that do not fit the bed.
- `--spacing MM`: center to center distance between copies. Default 70. The X1C hotend shroud clearance radius from the printer profile is 68 mm for objects lower than 34 mm, so do not go below about 70 for this 16 mm wide part.
- `--object-lift MM`: height above the finished object for the travel to the next one. Default 3.
- `--retract-at-start MM`: retract and unretract around the single approach move before the first layer. Default 2.5, matches the TPU profile.
- `--start-overhead S`: seconds the machine start gcode takes, only used for the progress display (M73). Default 497.


Extrusion model matches BambuStudio exactly: bead area = h (w - h) + pi (h/2)^2, relative E (M83).

## Design decisions worth remembering

- Only one ring per layer can be a pure helix. Two helices would need a descent of one layer height somewhere in the interior, and a descending closed loop collides with its own start bead at closure. Leaving the loop open moves the defect to a groove on a visible surface. Alternating ring order between layers fails for the same reason. Hence: flat rings plus one helix, same ring order every layer, one crossing per layer.
- Archimedean multi turn spirals were rejected: the outer surface would get a step of one line width at the start angle.
- Templates are nozzle specific (metadata, purge line). A new nozzle needs a fresh BambuStudio slice as template.

## Print history

1. First print, 0.3 mm layers, fixed connector angle, flat outer ring with a seam gap: layer lines far too visible compared with the reference, a visible vertical seam line on the outer cone and a mark in the bore at the fixed angle. Geometry change (thicker wall) was as intended.
2. 0.12 mm layers (first 0.2), rotating closures, scarf closures, bore helix.
3. 0.6 nozzle, 0.10 mm layers (first 0.2), same scheme. User still saw larger layer lines than the reference and asked for finer layers. 0.10 is the practical floor for the 0.6 nozzle.
4. 0.4 nozzle, 0.08 mm layers (first 0.16), 168 layers on the second STL, about 39 min. The user liked this finish and settled on the 0.4 nozzle.
5. Current: third STL (13.1 mm, 1.30 mm wall), 0.4 nozzle, 0.08 mm layers, 252 C, 163 layers, 7 rings at the flange, 3 on the wall, about 38 min. Not yet printed. Bug fixed at this point: template end of print commands (fan off, M1003 S0) were copied into the middle of finer layer files; the 0.12 and first 0.10 files had the fan switch off partway. Regenerated files are correct.

The user checks the reference part with a magnifying glass and reports zero seam on it. With a multi wall part exactly one surface can be a mathematically perfect helix; the other has scattered, flow ramped closures. If the outer surface turns out to matter more for sealing, regenerate with `--helix outer`.

## Status

`Output.4mm.gcode.3mf` is the current file to print (0.4 nozzle). Preview by dragging the 3mf into BambuStudio. If the first layer inner rings lift, lower `--first-layer-speed`. If the thin tower looks molten, raise `--min-layer-time`. If closures are still visible, try `--scarf 4` or `--jog-flow 0.15`.

## Conventions

- Bambu X1C prints .gcode.3mf. The md5 file inside must match plate_1.gcode.
- Never mention Claude in PR descriptions or comments. No dashes as punctuation in docs.
