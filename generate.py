#!/usr/bin/env python3
"""
Generate a seamless, single continuous extrusion toolpath for an axisymmetric
part (revolve body) and pack it into a BambuStudio .gcode.3mf.

The template .gcode.3mf (sliced in BambuStudio for the target printer, nozzle
and filament) supplies the machine start/end gcode, temperatures, fan schedule,
layer height, nozzle size and bed position. The STL supplies the 2D profile.

Toolpath design (per layer, rings numbered from the outside in):
  * every ring is a concentric circle, all printed with the extruder running
  * flat rings close on themselves with a small seam gap, then jog radially
    (inside the wall) to the next ring; the extruder never stops
  * the innermost ring (the bore, the sealing surface) is printed as a
    continuous helix climbing one layer height per turn, like vase mode, so
    it has no closing point at all
  * the return move to the outer ring of the next layer runs radially through
    the wall interior, one layer height above the rings it crosses
  * no retraction, no travel moves, no z hop, no wipe anywhere in the print
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import tempfile
import zipfile

# --------------------------------------------------------------------------- STL profile


def read_stl(path):
    with open(path, "rb") as f:
        head = f.read(80)
        n = struct.unpack("<I", f.read(4))[0]
        tris = []
        for _ in range(n):
            d = f.read(50)
            v = struct.unpack("<12f", d[:48])
            tris.append(((v[3], v[4], v[5]), (v[6], v[7], v[8]), (v[9], v[10], v[11])))
    return tris


def profile_segments(tris):
    """Intersect the mesh with the half plane y=0, x>0. Returns (r, z) segments."""
    segs = []
    for t in tris:
        pts = []
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            ya, yb = a[1], b[1]
            if ya == 0 and yb == 0:
                pts += [(a[0], a[2]), (b[0], b[2])]
            elif (ya < 0) != (yb < 0) or ya == 0 or yb == 0:
                if ya == yb:
                    continue
                s = -ya / (yb - ya)
                if 0 <= s <= 1:
                    pts.append((a[0] + s * (b[0] - a[0]), a[2] + s * (b[2] - a[2])))
        pts = [p for p in pts if p[0] > 1e-6]
        uniq = []
        for p in pts:
            if all(abs(p[0] - q[0]) > 1e-6 or abs(p[1] - q[1]) > 1e-6 for q in uniq):
                uniq.append(p)
        if len(uniq) >= 2:
            segs.append((uniq[0], uniq[1]))
    return segs


def wall_interval(segs, z):
    """Radial extent [r_in, r_out] of the solid at height z."""
    rs = []
    for (r0, z0), (r1, z1) in segs:
        if z0 == z1:
            continue
        if (z0 <= z < z1) or (z1 <= z < z0):
            s = (z - z0) / (z1 - z0)
            rs.append(r0 + s * (r1 - r0))
    if len(rs) < 2:
        return None
    return min(rs), max(rs)


# --------------------------------------------------------------------------- template


def read_template(path):
    tmp = tempfile.mkdtemp(prefix="seamless_")
    with zipfile.ZipFile(path) as z:
        z.extractall(tmp)
    gpath = os.path.join(tmp, "Metadata", "plate_1.gcode")
    with open(gpath, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    cfg = {}
    for ln in lines:
        m = re.match(r"^; ([a-z_0-9]+) = (.*)$", ln)
        if m:
            cfg[m.group(1)] = m.group(2)

    def idx(marker):
        for i, ln in enumerate(lines):
            if ln.startswith(marker):
                return i
        raise ValueError("marker not found: " + marker)

    exe_start = idx("; EXECUTABLE_BLOCK_START")
    first_layer = idx("; CHANGE_LAYER")
    end_start = idx("; MACHINE_END_GCODE_START")
    # preamble: machine start gcode + filament start gcode (G90, M83, ...)
    preamble = lines[exe_start:first_layer]
    # tail: from the machine end gcode to the end of file
    tail = lines[end_start - 1:] if lines[end_start - 1].startswith("; FEATURE") else lines[end_start:]
    header = lines[:exe_start]

    # per layer auxiliary commands (temperature, fans, powerloss) from the template
    per_layer = {}
    layer = 0
    for ln in lines[first_layer:end_start]:
        if ln.startswith("; close powerlost recovery") or ln.startswith("M1003 S0"):
            break  # end of print commands, not layer commands
        if ln.startswith("; CHANGE_LAYER"):
            layer += 1
        elif layer and re.match(r"^(M104|M106|M1003|M140)\b", ln):
            per_layer.setdefault(layer, []).append(ln)

    with open(os.path.join(tmp, "Metadata", "plate_1.json")) as f:
        plate = json.load(f)
    bb = plate["bbox_all"]
    center = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
    return dict(tmp=tmp, header=header, preamble=preamble, tail=tail, cfg=cfg,
                per_layer=per_layer, center=center, lines=lines)


# --------------------------------------------------------------------------- toolpath


class BoreSurface:
    """Height of the bore bead top as a function of angle, so the bore helix
    always extrudes exactly the volume needed to fill the gap beneath it."""

    def __init__(self, nbins=1440):
        self.n = nbins
        self.z = [0.0] * nbins

    def _bin(self, th):
        return int((th % (2 * math.pi)) / (2 * math.pi) * self.n) % self.n

    def height(self, th):
        return self.z[self._bin(th)]

    def set(self, th0, th1, z0, z1):
        n = max(1, int(abs(th1 - th0) / (2 * math.pi) * self.n) + 1)
        for i in range(n + 1):
            s = i / n
            self.z[self._bin(th0 + (th1 - th0) * s)] = z0 + (z1 - z0) * s


def bead_area(w, h):
    """Extrusion cross section used by BambuStudio: rectangle with round ends."""
    h = min(h, w)
    return h * (w - h) + math.pi * (h / 2) ** 2


class Path:
    def __init__(self, cx, cy, fil_d):
        self.cx, self.cy = cx, cy
        self.fil_area = math.pi * (fil_d / 2) ** 2
        self.out = []
        self.x = self.y = self.z = None
        self.e_total = 0.0
        self.length = 0.0

    def comment(self, s):
        self.out.append("; " + s)

    def raw(self, s):
        self.out.append(s)

    def move_to(self, x, y, z=None):
        self.x, self.y = x, y
        if z is not None:
            self.z = z

    def extrude_to(self, x, y, z, area):
        d = math.dist((self.x, self.y, self.z), (x, y, z))
        if d < 1e-6:
            return
        e = d * area / self.fil_area
        self.e_total += e
        self.length += d
        zs = "" if abs(z - self.z) < 1e-9 else " Z%.3f" % z
        self.out.append("G1 X%.3f Y%.3f%s E%.5f" % (x, y, zs, e))
        self.x, self.y, self.z = x, y, z

    def polar(self, r, th):
        return self.cx + r * math.cos(th), self.cy + r * math.sin(th)

    def steps_for(self, r, span, tol=0.004, max_deg=4.0):
        d = 2 * math.acos(max(-1.0, 1 - tol / r))
        d = min(d, math.radians(max_deg))
        return max(8, int(math.ceil(abs(span) / d)))

    def ring(self, r, th0, span, z0, z1, area_fn):
        """Arc at radius r from th0 over `span` radians, z from z0 to z1.
        area_fn(arc_from_start, th_mid, z_mid, th_prev, z_prev) gives the bead area."""
        n = self.steps_for(r, span)
        th_prev, z_prev = th0, z0
        for i in range(1, n + 1):
            s = i / n
            th = th0 + span * s
            z = z0 + (z1 - z0) * s
            sm = s - 0.5 / n
            a = area_fn(r * span * sm, th0 + span * sm, z0 + (z1 - z0) * sm, th_prev, z_prev, th, z)
            x, y = self.polar(r, th)
            self.extrude_to(x, y, z, a)
            th_prev, z_prev = th, z

    def jog(self, r_to, th_to, z_to, area, rise_within=None):
        """Radial connection inside the wall. Optionally rises within the first
        `rise_within` mm of radial travel, then continues level."""
        x1, y1 = self.polar(r_to, th_to)
        if rise_within is not None and z_to > self.z + 1e-9:
            d = math.dist((self.x, self.y), (x1, y1))
            if d > rise_within:
                s = rise_within / d
                xm, ym = self.x + (x1 - self.x) * s, self.y + (y1 - self.y) * s
                self.extrude_to(xm, ym, z_to, area)
        self.extrude_to(x1, y1, z_to, area)


def choose_rings(t, w_nom, w_min, w_max):
    k = max(1, int(round(t / w_nom)))
    w = t / k
    if w > w_max:
        k += 1
        w = t / k
    if w < w_min and k > 1:
        k -= 1
        w = t / k
    return k, w


def build(args, tpl, segs):
    cfg = tpl["cfg"]
    nozzle = float(cfg.get("nozzle_diameter", "0.4").split(",")[0])
    h = args.layer_height or float(cfg.get("layer_height", "0.2"))
    h0 = args.first_layer_height or float(cfg.get("initial_layer_print_height", h))
    fil_d = float(cfg.get("filament_diameter", "1.75").split(",")[0])
    density = float(cfg.get("filament_density", "1.24").split(",")[0])
    ef = float(cfg.get("elefant_foot_compensation", "0"))
    w_nom = args.line_width or round(nozzle * 1.03, 2)
    w_min, w_max = 0.83 * nozzle, 1.35 * nozzle
    cx, cy = tpl["center"]
    two_pi = 2 * math.pi
    rot = math.radians(args.rotate)
    scarf = args.scarf

    zmax = max(max(a[1], b[1]) for a, b in segs)
    # fit the layer height so the last layer lands exactly on the model top
    n_above = max(1, int(round((zmax - h0) / h)))
    h = (zmax - h0) / n_above
    layer_tops = [round(h0 + i * h, 4) for i in range(n_above + 1)]
    N = len(layer_tops)

    layers = []
    for n, zt in enumerate(layer_tops, start=1):
        lh = h0 if n == 1 else h
        iv = wall_interval(segs, zt - lh / 2)
        if iv is None:
            raise SystemExit("no wall at z=%.3f" % (zt - lh / 2))
        ri, ro = iv
        if n == 1 and ef > 0:
            ro -= ef
        k, w = choose_rings(ro - ri, w_nom, w_min, w_max)
        radii = [ro - w / 2 - i * w for i in range(k)]
        layers.append(dict(n=n, z=zt, h=lh, k=k, w=w, radii=radii))

    # temperature overrides: the template's first layer temp appears in the machine start
    # gcode (M104/M109), the print temp is set by an M104 at the start of layer 2
    t0_tpl = int(float(cfg.get("nozzle_temperature_initial_layer", "0").split(",")[0]))
    t_tpl = int(float(cfg.get("nozzle_temperature", "0").split(",")[0]))
    t_first = args.first_layer_temp or args.temp
    if t_first and t0_tpl:
        tpl["preamble"] = [re.sub(r"^(M10[49] S)%d\b" % t0_tpl, r"\g<1>%d" % t_first, ln) for ln in tpl["preamble"]]
    if args.temp and t_tpl:
        # the purge line in the machine start gcode also heats to the print temperature
        tpl["preamble"] = [re.sub(r"^(M10[49] S)%d\b" % t_tpl, r"\g<1>%d" % args.temp, ln) for ln in tpl["preamble"]]
        for n_, cmds in tpl["per_layer"].items():
            tpl["per_layer"][n_] = [re.sub(r"^(M104 S)%d\b" % t_tpl, r"\g<1>%d" % args.temp, ln) for ln in cmds]
    temps = (t_first or t0_tpl, args.temp or t_tpl)

    # ---- object layout: sequential printing, one whole object at a time
    count = max(1, args.count)
    cols = int(math.ceil(math.sqrt(count)))
    rows = int(math.ceil(count / cols))
    r_max = max(max(a[0], b[0]) for a, b in segs)
    centers = []
    for i in range(count):
        c, rw = i % cols, i // cols
        centers.append((cx + (c - (cols - 1) / 2) * args.spacing, cy + (rw - (rows - 1) / 2) * args.spacing))
    bed = float(cfg.get("printable_area", "0x0,256x0,256x256,0x256").split(",")[2].split("x")[0]) if "printable_area" in cfg else 256.0
    for (ox, oy) in centers:
        if ox - r_max < 2 or oy - r_max < 2 or ox + r_max > bed - 2 or oy + r_max > bed - 2:
            raise SystemExit("object at (%.1f, %.1f) does not fit on the %.0f mm bed, lower --spacing or --count" % (ox, oy, bed))

    p = Path(cx, cy, fil_d)

    def layer_length(L):
        return sum(two_pi * r for r in L["radii"]) + scarf * (L["k"] - 1) + L["radii"][-1] * rot + L["k"] * L["w"]

    def layer_speed(n, length):
        v = length / args.min_layer_time
        vmax = args.first_layer_speed if n == 1 else args.max_speed
        return max(args.min_speed, min(vmax, v))

    times = [layer_length(L) / layer_speed(L["n"], layer_length(L)) for L in layers]
    model_time = sum(times) * count
    total_time = model_time + args.start_overhead
    elapsed = args.start_overhead
    total_layers = N * count

    for oi, (ox, oy) in enumerate(centers):
        p.cx, p.cy = ox, oy
        surface = BoreSurface()
        L0 = layers[0]
        ang = 0.0
        x0, y0 = p.polar(L0["radii"][0] if args.helix == "bore" else L0["radii"][-1], ang)
        p.comment("OBJECT %d of %d at (%.2f, %.2f)" % (oi + 1, count, ox, oy))
        if oi == 0:
            # approach the first point from wherever the start gcode left the nozzle
            p.raw("G1 E-%.1f F2700" % args.retract_at_start)
            p.raw("G1 X%.3f Y%.3f F30000" % (x0, y0))
            p.raw("M204 S%d" % args.accel)
            p.raw("G1 Z%.3f" % L0["z"])
            p.raw("G1 E%.1f F2700" % args.retract_at_start)
        else:
            # finished object: retract, lift clear of it, travel, come down, unretract
            p.raw("G1 E-%.1f F2700" % args.retract_at_start)
            p.raw("G1 Z%.3f F3000" % (layer_tops[-1] + args.object_lift))
            if temps[0] != temps[1]:
                p.raw("M104 S%d ; first layer temperature for the next object" % temps[0])
            p.raw("G1 X%.3f Y%.3f F30000" % (x0, y0))
            p.raw("G1 Z%.3f F3000" % L0["z"])
            p.raw("G1 E%.1f F2700" % args.retract_at_start)
        p.move_to(x0, y0, L0["z"])

        for li, L in enumerate(layers):
            n, zt, k, w, radii, lh = L["n"], L["z"], L["k"], L["w"], L["radii"], L["h"]
            is_last = li == N - 1
            gn = oi * N + n  # global layer number for the progress display
            p.comment("CHANGE_LAYER")
            p.comment("Z_HEIGHT: %g" % zt)
            p.comment("LAYER_HEIGHT: %g" % lh)
            p.comment("layer num/total_layer_count: %d/%d" % (gn, total_layers))
            p.comment("update layer progress")
            p.raw("M73 L%d" % gn)
            p.raw("M991 S0 P%d ;notify layer change" % (gn - 1))
            p.raw("M73 P%d R%d" % (int(100 * elapsed / total_time), int(math.ceil((total_time - elapsed) / 60))))
            for cmd in tpl["per_layer"].get(n, []):
                p.raw(cmd)  # fan off for the first layers, print temperature, fan on: per object
            v = layer_speed(n, layer_length(L))
            p.comment("rings: %d, line width: %.3f, speed: %.1f mm/s, start angle %.0f deg" % (k, w, v, math.degrees(ang % two_pi)))
            p.raw("G1 F%d" % int(round(v * 60)))
            elapsed += times[li]

            full = bead_area(w, lh)
            jog_area = full * args.jog_flow

            def flat_area(r, ls):
                total = two_pi * r + ls

                def fn(arc, th, z, th_prev, z_prev, th_new, z_new):
                    m = 1.0
                    if ls > 0:
                        if arc < ls:
                            m = arc / ls
                        elif arc > total - ls:
                            m = (total - arc) / ls
                    return full * max(0.0, min(1.0, m))
                return fn

            order = radii if args.helix == "bore" else list(reversed(radii))
            for i, r in enumerate(order):
                inner = i == k - 1  # last ring in the order gets the helix
                p.comment("FEATURE: " + ("Outer wall" if (i == 0 or inner) else "Inner wall"))
                p.comment("LINE_WIDTH: %.3f" % w)
                ls = min(scarf, math.pi * r)
                if not inner:
                    # flat ring, flow ramps up at the start and down over the overlap at the end
                    span = two_pi + ls / r
                    p.ring(r, ang, span, zt, zt, flat_area(r, ls))
                    ang = (ang + span) % two_pi
                    p.jog(order[i + 1], ang, zt, jog_area)
                else:
                    if n == 1 or is_last:
                        # flat bore ring, volume from the actual gap below (first layer: bed,
                        # last layer: the helix below, so the rim ends flat)
                        span = two_pi + ls / r
                        z0, z1 = zt, zt
                    else:
                        # bore helix: climbs one layer height over a bit more than one turn,
                        # so the departure point rotates around the part every layer
                        span = two_pi + rot
                        z0, z1 = zt, layers[li + 1]["z"]

                    def bore_area(arc, th, z, th_prev, z_prev, th_new, z_new, _flat=flat_area(r, ls) if (n == 1 or is_last) else None):
                        gap = z - surface.height(th)
                        gap = max(0.0, min(gap, 2.5 * lh))
                        a = bead_area(w, gap) if gap > 0.02 * lh else 0.0
                        if _flat is not None:
                            a *= _flat(arc, th, z, th_prev, z_prev, th_new, z_new) / full
                        surface.set(th_prev, th_new, z_prev, z_new)
                        return a

                    p.ring(r, ang, span, z0, z1, bore_area)
                    ang = (ang + span) % two_pi

            if not is_last:
                Ln = layers[li + 1]
                p.comment("FEATURE: Inner wall")
                first_next = Ln["radii"][0] if args.helix == "bore" else Ln["radii"][-1]
                p.jog(first_next, ang, Ln["z"], jog_area, rise_within=w)

    p.raw("; close powerlost recovery")
    p.raw("M1003 S0")
    volume = p.e_total * p.fil_area
    stats = dict(layers=total_layers, length_mm=p.e_total, volume_mm3=volume, weight_g=volume / 1000 * density,
                 path_len=p.length, model_time=model_time, total_time=total_time, zmax=layer_tops[-1],
                 nozzle=nozzle, h=h, h0=h0, w_nom=w_nom, bore_pitch=h / (1 + rot / two_pi), temps=temps,
                 count=count, centers=centers)
    return p.out, stats, layers


def fmt_time(s):
    s = int(round(s))
    if s >= 3600:
        return "%dh %dm %ds" % (s // 3600, (s % 3600) // 60, s % 60)
    return "%dm %ds" % (s // 60, s % 60)


def write_outputs(args, tpl, body, stats):
    header = list(tpl["header"])
    repl = {
        "; model printing time:": "; model printing time: %s; total estimated time: %s" % (
            fmt_time(stats["model_time"]), fmt_time(stats["total_time"])),
        "; total layer number:": "; total layer number: %d" % stats["layers"],
        "; total filament length [mm] :": "; total filament length [mm] : %.2f" % stats["length_mm"],
        "; total filament volume [cm^3] :": "; total filament volume [cm^3] : %.2f" % stats["volume_mm3"],
        "; total filament weight [g] :": "; total filament weight [g] : %.2f" % stats["weight_g"],
        "; max_z_height:": "; max_z_height: %.2f" % stats["zmax"],
    }
    for i, ln in enumerate(header):
        for key, val in repl.items():
            if ln.startswith(key):
                header[i] = val
    # note in the config block so the file is identifiable
    for i, ln in enumerate(header):
        if ln.startswith("; CONFIG_BLOCK_START"):
            header.insert(i + 1, "; seamless_toolpath = 1 (generated by generate.py, single continuous extrusion)")
            break

    preamble = list(tpl["preamble"])
    tail = list(tpl["tail"])
    # fans off + spaghetti detector off as in the template before the end gcode
    pre_tail = ["M106 S0", "M106 P2 S0", "M981 S0 P20000 ; close spaghetti detector"]
    gcode = "\n".join(header + preamble + body + pre_tail + tail)
    if not gcode.endswith("\n"):
        gcode += "\n"

    tmp = tpl["tmp"]
    gpath = os.path.join(tmp, "Metadata", "plate_1.gcode")
    with open(gpath, "w", encoding="utf-8") as f:
        f.write(gcode)
    md5 = hashlib.md5(gcode.encode("utf-8")).hexdigest().upper()
    with open(os.path.join(tmp, "Metadata", "plate_1.gcode.md5"), "w") as f:
        f.write(md5)
    # slice info: prediction and weight
    sp = os.path.join(tmp, "Metadata", "slice_info.config")
    with open(sp) as f:
        si = f.read()
    si = re.sub(r'key="prediction" value="\d+"', 'key="prediction" value="%d"' % int(stats["total_time"]), si)
    si = re.sub(r'key="weight" value="[0-9.]+"', 'key="weight" value="%.2f"' % stats["weight_g"], si)
    with open(sp, "w") as f:
        f.write(si)

    out3mf = args.output
    outgcode = None
    if args.plain_gcode:
        outgcode = re.sub(r"\.gcode\.3mf$", ".gcode", out3mf)
        if outgcode == out3mf:
            outgcode = out3mf + ".gcode"
        with open(outgcode, "w", encoding="utf-8") as f:
            f.write(gcode)
    # repack, keeping the original entry order
    with zipfile.ZipFile(args.template) as zin:
        names = zin.namelist()
    with zipfile.ZipFile(out3mf, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in names:
            zout.write(os.path.join(tmp, name), name)
    shutil.rmtree(tmp, ignore_errors=True)
    return out3mf, outgcode


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stl", required=True)
    ap.add_argument("--template", required=True, help="BambuStudio sliced .gcode.3mf used as template")
    ap.add_argument("--output", required=True, help="output .gcode.3mf")
    ap.add_argument("--plain-gcode", action="store_true", help="also write a plain .gcode next to the 3mf, for inspection")
    ap.add_argument("--line-width", type=float, default=None, help="nominal line width, default 1.03 x nozzle")
    ap.add_argument("--layer-height", type=float, default=None, help="override template layer height (mm)")
    ap.add_argument("--first-layer-height", type=float, default=None, help="override template first layer height (mm)")
    ap.add_argument("--helix", choices=["bore", "outer"], default="bore", help="which visible surface is printed as the continuous helix (the other closes each layer with a scattered, flow ramped closure)")
    ap.add_argument("--rotate", type=float, default=137.5, help="degrees the bore helix overshoots each layer; rotates all ring closures and the internal connector around the part")
    ap.add_argument("--scarf", type=float, default=2.0, help="mm over which flat ring closures ramp flow down while overlapping the ramped up start")
    ap.add_argument("--max-speed", type=float, default=25.0, help="mm/s")
    ap.add_argument("--min-speed", type=float, default=6.0, help="mm/s")
    ap.add_argument("--first-layer-speed", type=float, default=18.0, help="mm/s")
    ap.add_argument("--min-layer-time", type=float, default=10.0, help="seconds")
    ap.add_argument("--accel", type=int, default=1500)
    ap.add_argument("--temp", type=int, default=None, help="nozzle temperature from layer 2 on (C), default from the template")
    ap.add_argument("--first-layer-temp", type=int, default=None, help="first layer nozzle temperature (C), default --temp if given, else the template's")
    ap.add_argument("--jog-flow", type=float, default=0.3, help="flow factor on internal ring to ring connections")
    ap.add_argument("--count", type=int, default=1, help="number of copies on the plate, printed one whole object after another (never by layer), on a grid around the template position")
    ap.add_argument("--spacing", type=float, default=70.0, help="center to center distance between copies in mm; must clear the hotend shroud of finished objects (X1C clearance radius is 68 mm at less than 34 mm height)")
    ap.add_argument("--object-lift", type=float, default=3.0, help="mm above the finished object height for the travel to the next object")
    ap.add_argument("--retract-at-start", type=float, default=2.5)
    ap.add_argument("--start-overhead", type=float, default=497.0, help="seconds spent in machine start gcode (for progress display)")
    args = ap.parse_args()

    tpl = read_template(args.template)
    segs = profile_segments(read_stl(args.stl))
    body, stats, layers = build(args, tpl, segs)
    out3mf, outgcode = write_outputs(args, tpl, body, stats)

    print("nozzle %.2f  layer %.3f (first %.2f)  nominal width %.2f  bore helix pitch %.3f" % (stats["nozzle"], stats["h"], stats["h0"], stats["w_nom"], stats["bore_pitch"]))
    print("nozzle temperature: first layer %d C, then %d C" % stats["temps"])
    if stats["count"] > 1:
        print("objects: %d, printed one after another at " % stats["count"] + ", ".join("(%.0f, %.0f)" % c for c in stats["centers"]))
    print("layers %d  path %.0f mm  filament %.0f mm  %.2f g" % (stats["layers"], stats["path_len"], stats["length_mm"], stats["weight_g"]))
    print("model time %s, total with start gcode %s" % (fmt_time(stats["model_time"]), fmt_time(stats["total_time"])))
    prev = None
    for L in layers:
        key = (L["k"], round(L["w"], 2))
        if key != prev:
            print("  from z=%5.2f: %d rings x %.3f mm" % (L["z"], L["k"], L["w"]))
            prev = key
    print("wrote", out3mf)
    if outgcode:
        print("wrote", outgcode)


if __name__ == "__main__":
    main()
