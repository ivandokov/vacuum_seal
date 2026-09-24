# Vacuum seal gasket, seamless TPU print

A pneumatic speargun vacuum seal gasket printed in TPU as one continuous extrusion line. No retraction, no travel moves, no z hop and no seam anywhere on the sealing surfaces. The bore is printed as a continuous helix, the other walls close at a different angle on every layer.

## Files

- `Vacuum seal.stl`: the gasket, a revolve body with its axis on Z and the flange at z 0.
- `template.4mm.gcode.3mf`: a normal BambuStudio slice of the part for the X1 Carbon, 0.4 mm nozzle and Filaflex 82A. Only its start and end gcode, temperatures, fan schedule and bed position are used.
- `generate.py`: builds the seamless toolpath from the STL and packs it into a printable `.gcode.3mf` using the template. Pure Python 3, no dependencies.
- `Output.4mm.gcode.3mf`: the generated file, ready to print.

## Usage

```
python3 generate.py --stl "Vacuum seal.stl" --template template.4mm.gcode.3mf --output Output.4mm.gcode.3mf --layer-height 0.08 --first-layer-height 0.16 --temp 252
```

Add `--count 3` to print three copies one after another. Run `python3 generate.py --help` for all flags. `CLAUDE.md` documents every flag, the toolpath design and the print history in detail.
