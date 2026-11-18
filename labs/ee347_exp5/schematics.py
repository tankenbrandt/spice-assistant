"""Draw the Exp. 5 schematics (gate level, with breadboard pin numbers).

    python labs/ee347_exp5/schematics.py

Writes fig5_1_schematic.png and fig5_3_schematic.png next to this file; the
lab file shows them as each part's schematic. ascgen can't draw these decks
(its only subcircuit symbol is an op-amp), so they're drawn here by hand,
like the handout's Figures 5.1 and 5.3. The pin numbers are not typed in:
they come from layout.place(), the same assignment as the report's wiring
list, so the two can't disagree.
"""

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Arc, Circle, Polygon  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
import layout  # noqa: E402

PHI, PHIB = r"$\phi$", r"$\overline{\phi}$"
LW, PIN, NODE = 1.6, 7.5, 10


class Sheet:
    def __init__(self, deck: str):
        self.lay = layout.place(deck)
        self.fig, self.ax = plt.subplots()
        self.ax.set_aspect("equal")
        self.ax.axis("off")

    # -- pin numbers from the layout: "m.n" for every pin of `refs` on `net`
    def pins(self, refs, net: str) -> str:
        found = []
        for ref in refs:
            pl, slot, _ = self.lay.where[ref]
            found += [f"{pl.number}.{p}" for p in slot.pins if pl.nets.get(p) == net]
        return ",".join(dict.fromkeys(found))

    def unit(self, ref: str) -> str:
        pl, slot, _ = self.lay.where[ref]
        return f"{pl.package.name} {slot.unit} (pkg {pl.number})"

    # -- drawing primitives
    def wire(self, *pts):
        xs, ys = zip(*pts, strict=True)
        self.ax.plot(xs, ys, color="black", lw=LW, solid_capstyle="round")

    def dot(self, x, y):
        self.ax.add_patch(Circle((x, y), 0.045, color="black", zorder=3))

    def text(self, x, y, s, size=PIN, **kw):
        kw.setdefault("ha", "center")
        kw.setdefault("va", "center")
        self.ax.text(x, y, s, fontsize=size, **kw)

    def node(self, x, y, s, **kw):
        self.text(x, y, s, size=NODE, weight="bold", color="#1f4e8c", **kw)

    def tg(self, cx, cy, ref, ctl, net_in, net_out, net_ctl):
        """CD4016 switch: the transmission-gate bowtie, control pin on top."""
        a, b, h = cx - 0.35, cx + 0.35, 0.22
        for tri in (((a, cy + h), (a, cy - h), (b, cy)), ((b, cy + h), (b, cy - h), (a, cy))):
            self.ax.add_patch(Polygon(tri, closed=True, fill=False, lw=LW))
        self.wire((cx, cy + 0.11), (cx, cy + 0.55))
        self.text(cx, cy + 0.7, ctl, size=NODE + 1)
        self.text(cx + 0.18, cy + 0.42, self.pins([ref], net_ctl), ha="left")
        self.text(a - 0.05, cy + 0.14, self.pins([ref], net_in), ha="right")
        self.text(b + 0.05, cy + 0.14, self.pins([ref], net_out), ha="left")
        self.text(cx, cy - 0.42, self.unit(ref), size=6.5, color="#555")
        return a, b

    def inv(self, x, cy, refs, net_in, net_out):
        """CD4007 inverter (one P, one N): triangle and bubble."""
        self.ax.add_patch(Polygon(((x, cy + 0.3), (x, cy - 0.3), (x + 0.55, cy)),
                                  closed=True, fill=False, lw=LW))
        self.ax.add_patch(Circle((x + 0.61, cy), 0.06, fill=False, lw=LW))
        self.text(x - 0.05, cy + 0.15, self.pins(refs, net_in), ha="right")
        self.text(x + 0.72, cy + 0.15, self.pins(refs, net_out), ha="left")
        pl = self.lay.where[refs[0]][0]
        self.text(x + 0.3, cy - 0.45, f"CD4007 (pkg {pl.number})", size=6.5, color="#555")
        return x + 0.67

    def nand(self, x, cy, ref, net_in, net_out, d=1):
        """7400 gate with both inputs tied (an inverter). d=-1 faces left."""
        body, r = 0.3, 0.27
        X = lambda dx: x + d * dx  # noqa: E731
        self.wire((X(body), cy + r), (X(0), cy + r), (X(0), cy - r), (X(body), cy - r))
        self.ax.add_patch(Arc((X(body), cy), 2 * r, 2 * r,
                              theta1=-90 if d > 0 else 90, theta2=90 if d > 0 else 270, lw=LW))
        self.ax.add_patch(Circle((X(body + r + 0.06), cy), 0.06, fill=False, lw=LW))
        out = X(body + r + 0.12)
        # the two inputs, tied together just outside the gate
        for dy in (0.13, -0.13):
            self.wire((X(0), cy + dy), (X(-0.2), cy + dy))
        self.wire((X(-0.2), cy + 0.13), (X(-0.2), cy - 0.13))
        self.dot(X(-0.2), cy)
        self.text(X(-0.08), cy + 0.36, self.pins([ref], net_in), ha="center")
        self.text(out, cy + 0.2, self.pins([ref], net_out),
                  ha="left" if d > 0 else "right")
        self.text(X(0.3), cy - 0.45, self.unit(ref), size=6.5, color="#555")
        return X(-0.2), out

    def save(self, path: Path, note: str):
        self.ax.autoscale_view()
        x0, x1 = self.ax.get_xlim()
        y0, _ = self.ax.get_ylim()
        self.text((x0 + x1) / 2, y0 - 0.15, note, size=7, color="#333", va="top")
        self.ax.margins(0.03)
        self.fig.set_size_inches((x1 - x0) * 1.05 + 0.6, 1.0 * (self.ax.get_ylim()[1] - y0) + 1.2)
        self.fig.savefig(path, dpi=200, bbox_inches="tight", facecolor="white")
        plt.close(self.fig)


NOTE = ("Pin numbers m.n = package m, pin n (the report's wiring list). Every chip: "
        "pin 14 to VDD = 5 V, pin 7 to ground. CD4007 inverters: P W/L 60/5, N 30/5.")


def stage(s: Sheet, x, y, tga, inva, tgb, invb, nets):
    """One Figure 5.1 stage from x: TG(phi), inverter, TG(phibar), inverter.
    nets = (in, x_a, y_a, x_b, out). Returns the output x."""
    n_in, xa, ya, xb, n_out = nets
    a, b = s.tg(x + 0.55, y, tga, PHI, n_in, xa, "phi")
    s.wire((x, y), (a, y))
    s.wire((b, y), (b + 0.9, y))
    s.text(b + 0.45, y - 0.18, xa, size=7, style="italic")
    o = s.inv(b + 0.9, y, inva, xa, ya)
    a2, b2 = s.tg(o + 1.35, y, tgb, PHIB, ya, xb, "phib")
    s.wire((o, y), (a2, y))
    s.text((o + a2) / 2, y - 0.18, ya, size=7, style="italic")
    s.wire((b2, y), (b2 + 0.9, y))
    s.text(b2 + 0.45, y - 0.18, xb, size=7, style="italic")
    return s.inv(b2 + 0.9, y, invb, xb, n_out)


def fig5_1():
    s = Sheet((HERE / "fig5_1.cir").read_text(encoding="utf-8"))
    s.node(-0.35, 0, "VIN")
    s.dot(0, 0)
    out = stage(s, 0, 0, "XTG1", ("M1P", "M1N"), "XTG2", ("M2P", "M2N"),
                ("in", "x1", "y1", "x2", "out"))
    s.wire((out, 0), (out + 0.6, 0))
    s.dot(out + 0.3, 0)
    s.node(out + 0.95, 0, "VOUT")
    # feedback inverter (Figure 5.3's third gate), facing left under the stage
    fy = -1.25
    mid = (out + 0.3) / 2
    tie, o = s.nand(mid + 0.4, fy, "XU1", "out", "in", d=-1)
    s.wire((out + 0.3, 0), (out + 0.3, fy), (tie, fy))
    s.wire((o, fy), (0, fy), (0, 0))
    s.text(0.55, 0.95, "Vphi: 0-5 V, 1 kHz", size=7, color="#555")
    s.text(3.82, 0.95, "Vphib: 5-0 V, 1 kHz (complement)", size=7, color="#555")
    s.save(HERE / "fig5_1_schematic.png", NOTE)


def fig5_3():
    s = Sheet((HERE / "fig5_3.cir").read_text(encoding="utf-8"))
    # clock buffers (top left): gen -> U(A) -> phi -> U(B) -> phibar
    cy = 1.9
    s.node(-0.1, cy, "pulse gen\n0-5 V, 1 kHz", ha="right")
    s.wire((0, cy), (0.3, cy))
    tie, o = s.nand(0.5, cy, "XU1", "gen", "phi")
    s.wire((0.3, cy), (tie, cy))
    s.wire((o, cy), (o + 0.5, cy))
    s.dot(o + 0.3, cy)
    s.wire((o + 0.3, cy), (o + 0.3, cy + 0.45))
    s.text(o + 0.3, cy + 0.6, PHI, size=NODE + 1)
    tie2, o2 = s.nand(o + 0.7, cy, "XU2", "phi", "phib")
    s.wire((o + 0.5, cy), (tie2, cy))
    s.wire((o2, cy), (o2 + 0.4, cy))
    s.text(o2 + 0.55, cy, PHIB, size=NODE + 1)
    # the two stages
    s.node(-0.55, 0, "(1) VIN")
    s.dot(0, 0)
    mid = stage(s, 0, 0, "XTG1", ("M1P", "M1N"), "XTG2", ("M2P", "M2N"),
                ("in", "x1", "y1", "x2", "n2"))
    s.wire((mid, 0), (mid + 1.0, 0))
    s.node(mid + 0.5, -0.3, "(2)")
    out = stage(s, mid + 1.0, 0, "XTG3", ("M3P", "M3N"), "XTG4", ("M4P", "M4N"),
                ("n2", "x3", "y3", "x4", "out"))
    s.wire((out, 0), (out + 0.6, 0))
    s.dot(out + 0.3, 0)
    s.node(out + 1.15, 0, "(3) VOUT")
    fy = -1.25
    tie, o = s.nand(out / 2 + 0.4, fy, "XU3", "out", "in", d=-1)
    s.wire((out + 0.3, 0), (out + 0.3, fy), (tie, fy))
    s.wire((o, fy), (0, fy), (0, 0))
    s.save(HERE / "fig5_3_schematic.png",
           NOTE + f" {PHI} and {PHIB} are the clock-buffer outputs at top left.")


if __name__ == "__main__":
    fig5_1()
    fig5_3()
    print("wrote", HERE / "fig5_1_schematic.png", HERE / "fig5_3_schematic.png")
