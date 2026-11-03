#!/usr/bin/env python3
"""The test-set block diagram: which instrument connects to which node.

Step 6 of the course's ten-step method asks for a "cartoon" of the test set:
instruments as blocks around a simplified circuit, so the test plan can refer
to it. Everything needed is already in the deck and the spec:

* the deck's sources say what drives the circuit -- a DC supply per rail, a
  function generator per sine/pulse source, switches for stepped logic inputs;
* the spec's vectors say what gets measured -- each becomes a scope channel
  (or a DMM reading, for a DC-only spec).

    python blockdiagram.py labs/ee347_exp4/fig4_5.cir --spec labs/ee347_exp4/fig4_5.yaml -o test_set.png

Pure drawing -- no simulation, no API calls.
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import specs as specfile

SCOPE_CHANNELS = 4

# Muted, print-friendly: these end up in a lab report, often printed.
INK = "#1d1d1b"
MUTED = "#6b6a65"
LINE = "#3c3b37"
FILL = {"supply": "#e8eef6", "signal": "#eef4ea", "logic": "#f6f0e4",
        "dut": "#ffffff", "scope": "#f1ecf5", "dmm": "#f1ecf5"}

_VEC_RE = re.compile(r"(?i)^\s*(?:v|vdb|vp|vm|vr|vi)\(([^,)]+)(?:,\s*([^)]+))?\)\s*$")


@dataclass
class TestSet:
    title: str
    supplies: list = field(default_factory=list)     # (name, node, volts)
    generators: list = field(default_factory=list)   # (name, node, text)
    logic: list = field(default_factory=list)        # (name, node, levels text)
    probes: list = field(default_factory=list)       # node names, in channel order
    dmm_only: bool = False

    @property
    def channels(self) -> list:
        return self.probes[:SCOPE_CHANNELS]

    @property
    def overflow(self) -> list:
        return self.probes[SCOPE_CHANNELS:]


def _probed_nodes(spec) -> list:
    nodes = []
    for a in spec.analyses.values():
        for v in a.vectors:
            m = _VEC_RE.match(v)
            if not m:
                continue
            for n in (m.group(1), m.group(2)):
                if n and n.strip() != "0" and n.strip() not in nodes:
                    nodes.append(n.strip())
    return nodes


def from_deck(spec, deck: str, title: str = "") -> TestSet:
    """Instruments from the deck's sources, probe points from the spec."""
    from labreport import sources      # the same source reading the test plan uses
    ts = TestSet(title=title or spec.name)
    for s in sources(deck):
        node = s["nodes"][0] if s["nodes"][0] != "0" else s["nodes"][1]
        if s["kind"] == "dc":
            ts.supplies.append((s["name"], node, s["value"]))
        elif s["kind"] == "signal":
            ts.generators.append((s["name"], node, s["text"].split(":", 1)[-1].strip()))
        elif s["kind"] == "ac":
            ts.generators.append((s["name"], node, "sine, swept frequency"))
        elif s["kind"] == "logic":
            ts.logic.append((s["name"], node, s["text"]))
    rails = {n for _, n, _ in ts.supplies}
    gens = [n for _, n, _ in ts.generators]
    switched = {n for _, n, _ in ts.logic}
    probed = [n for n in _probed_nodes(spec) if n not in rails]
    # Channel priority: the generator's output (gain and timing are read
    # against it, so it is probed even when the spec never names it), then the
    # circuit's own nodes, and switched logic inputs last -- a switch is a DC
    # level you already know, the first thing to give up for a channel.
    ts.probes = (gens + [n for n in probed if n not in gens and n not in switched]
                 + [n for n in probed if n in switched])
    kinds = {a.command.split()[0].lower() for a in spec.analyses.values()}
    ts.dmm_only = kinds <= {"op", "dc"}
    return ts


# ------------------------------------------------------------------ drawing

def _box(ax, x, y, w, h, title, lines, kind):
    from matplotlib.patches import FancyBboxPatch
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                fc=FILL[kind], ec=LINE, lw=1.2))
    ax.text(x + w / 2, y + h - 0.22, title, ha="center", va="top", fontsize=9.5,
            fontweight="bold", color=INK)
    for i, ln in enumerate(lines):
        ax.text(x + w / 2, y + h - 0.55 - i * 0.27, ln, ha="center", va="top",
                fontsize=8, color=MUTED)


def _wire(ax, pts, label=None, label_at=0, color=LINE):
    xs, ys = zip(*pts, strict=True)
    ax.plot(xs, ys, color=color, lw=1.3, solid_capstyle="round")
    ax.plot(xs[-1], ys[-1], "o", color=color, ms=3.5)
    if label:
        (x0, y0), (x1, y1) = pts[label_at], pts[label_at + 1]
        ax.text((x0 + x1) / 2, (y0 + y1) / 2 + 0.07, label, ha="center", va="bottom",
                fontsize=7.5, color=INK)


def draw(ts: TestSet, path: Path) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # One box per generator; all switched logic inputs share one box.
    left = [("gen", g) for g in ts.generators] + ([("logic", ts.logic)] if ts.logic else [])
    rows = max(len(left), len(ts.channels), 2)
    H = max(5.2, 2.4 + rows * 0.95)
    fig, ax = plt.subplots(figsize=(8.6, H * 0.78))
    ax.set_xlim(0, 11)
    ax.set_ylim(-0.9, H)
    ax.axis("off")

    # Circuit under test, centre.
    dx, dw = 4.1, 2.8
    dy, dh = 0.6, H - 2.4
    # A handout's part title is often longer than the box is wide: wrap it.
    import textwrap
    _box(ax, dx, dy, dw, dh, "Circuit under test", textwrap.wrap(ts.title, 30)[:4], "dut")

    # DC supply across the top, one lead per rail into the top of the circuit.
    if ts.supplies:
        sw = 3.4
        sx = dx + dw / 2 - sw / 2
        _box(ax, sx, H - 1.25, sw, 1.0, "DC power supply",
             ["  ".join(f"{n} = {v:g} V" for n, _, v in ts.supplies)], "supply")
        for i, (_, node, _) in enumerate(ts.supplies):
            x = dx + dw * (i + 1) / (len(ts.supplies) + 1)
            _wire(ax, [(x, H - 1.25), (x, dy + dh)])
            ax.text(x + 0.06, dy + dh + 0.12, node.upper(), fontsize=7.5, color=INK)

    # Sources down the left, each wired to its input node. Boxes differ in
    # height (the switch box grows per input), so stack them by height with
    # equal gaps rather than spacing their centres evenly.
    heights = [0.84 if kind == "gen" else 0.95 + 0.34 * len(item) for kind, item in left]
    gap = max(0.2, (dh - sum(heights)) / (len(left) + 1)) if left else 0
    top = dy + dh
    for (kind, item), bh in zip(left, heights, strict=True):
        top -= gap
        bottom = top - bh
        if kind == "gen":
            _, node, text = item
            text = text if len(text) <= 30 else text[:29] + "…"
            _box(ax, 0.2, bottom, 2.9, bh, "Function generator", [text], "signal")
            _wire(ax, [(3.1, bottom + bh / 2), (dx, bottom + bh / 2)], label=node.upper())
        else:
            _box(ax, 0.2, bottom, 2.9, bh, "Logic inputs (switches)",
                 ["0 V or VDD per table row"], "logic")
            for j, (_, node, _) in enumerate(item):
                yy = top - 0.95 - j * 0.34
                _wire(ax, [(3.1, yy), (dx, yy)], label=node.upper())
        top = bottom

    # Scope (or DMM) down the right, one channel per probed node.
    inst = "Digital multimeter" if ts.dmm_only else "Oscilloscope (DC-coupled)"
    sx = 7.9
    _box(ax, sx, dy, 2.9, dh, inst, [], "dmm" if ts.dmm_only else "scope")
    chans = ts.channels
    cgap = dh / (len(chans) + 1) if chans else 0
    for i, node in enumerate(chans):
        y = dy + dh - cgap * (i + 1)
        label = node.upper()
        ch = "DMM" if ts.dmm_only else f"CH{i + 1}"
        _wire(ax, [(dx + dw, y), (sx, y)], label=label)
        ax.text(sx + 0.15, y, ch, va="center", fontsize=8.5, color=INK, fontweight="bold")
    if ts.overflow:
        ax.text(sx + 1.45, dy + 0.18, "move a probe to read " + ", ".join(
            n.upper() for n in ts.overflow), ha="center", fontsize=7.2, color=MUTED)

    # Common ground: every instrument returns to the circuit's ground.
    gy = -0.45
    ax.plot([0.6, 10.4], [gy, gy], color=LINE, lw=1.6)
    for x in ([1.65] if left else []) + [dx + dw / 2, sx + 1.45]:
        ax.plot([x, x], [gy, dy if x != 1.65 else dy + 0.0], color=LINE, lw=1.0, ls=(0, (2, 2)))
    ax.text(10.4, gy - 0.12, "common ground", ha="right", va="top", fontsize=7.5, color=MUTED)

    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("deck")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("-o", "--out", default="test_set.png")
    a = ap.parse_args(argv)
    ts = from_deck(specfile.load(a.spec), Path(a.deck).read_text(encoding="utf-8"), a.title)
    print(f"wrote {draw(ts, Path(a.out))}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
