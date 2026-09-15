#!/usr/bin/env python3
"""Plot what the spec measured: ngspice vectors -> standalone SVG / HTML.

`speccheck` prints a number; this draws the curve that number came from, with
the acceptance band on it. That is the difference between "gain = 107.3, FAIL"
and seeing the response sitting a factor of two above where it should be.

    python plot.py baseline/ce_bjt_amp.cir --spec specs_lib/ce_bjt_amp.yaml --open
    python plot.py deck.cir --analysis "ac dec 200 10 1meg" --vectors vdb(out)
    python plot.py deck.cir --spec s.yaml --format svg --theme dark

Reads the analyses straight out of the spec file, so the plot shows exactly
the sweep the verdict was computed from -- not a second opinion.

Vectors are grouped into panels by unit (volts, dB, degrees, amps). A Bode plot
is therefore two stacked panels rather than one chart with two y-scales: a
second scale on the same axes makes any crossing point an artefact of where
the scales were pinned. Pure ngspice -- no API calls.
"""

import argparse
import html
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------- palette
#
# Categorical slots 1-3 of a validated palette, in both modes. Three is the
# cap that clears the all-pairs colour-vision gates; a fourth vector folds
# into a further panel rather than inventing a hue. Every series is also
# direct-labelled, which is what lets aqua be used on the light surface
# despite sitting under 3:1 against it.
THEMES = {
    "light": {
        "surface": "#fcfcfb", "plane": "#f9f9f7",
        "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "axis": "#c3c2b7",
        "series": ["#2a78d6", "#eb6834", "#1baf7a"],
        "good": "#0ca30c", "critical": "#d03b3b",
        "band": "rgba(12,163,12,0.07)",
    },
    "dark": {
        "surface": "#1a1a19", "plane": "#0d0d0d",
        "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835",
        "series": ["#3987e5", "#d95926", "#199e70"],
        "good": "#0ca30c", "critical": "#d03b3b",
        "band": "rgba(12,163,12,0.10)",
    },
}

def css_theme() -> dict:
    """The theme dict, with every colour replaced by the CSS variable holding
    it. A standalone .svg has no stylesheet and keeps literal hex; an inline
    SVG uses this, so the panels switch with the page rather than staying on
    whichever theme rendered them."""
    keys = [k for k, v in THEMES["light"].items() if isinstance(v, str)]
    out = {k: f"var(--{k})" for k in keys}
    out["series"] = [f"var(--series-{i + 1})"
                     for i in range(len(THEMES["light"]["series"]))]
    return out


def theme_css(theme: dict) -> str:
    """Emit one theme as custom-property declarations."""
    parts = [f"--{k}:{v};" for k, v in theme.items() if isinstance(v, str)]
    parts += [f"--series-{i + 1}:{c};" for i, c in enumerate(theme["series"])]
    return "".join(parts)


# ------------------------------------------------------------------ units

_UNITS = [
    ("db", "dB", "dB"),
    ("vp", "phase", "deg"),
    ("vm", "volts", "V"),
    ("vr", "volts", "V"),
    ("vi", "volts", "V"),
    ("v", "volts", "V"),
    ("i", "current", "A"),
]


def unit_family(vector: str) -> tuple[str, str]:
    """Classify a vector name into (family, unit symbol).

    `vdb(out)` is dB, `vp(out)` is degrees, `v(out)` and `vm(out)` are volts.
    Vectors from different families never share an axis.
    """
    name = vector.strip().lower()
    head = name.split("(")[0] if "(" in name else name
    for prefix, family, unit in _UNITS:
        if head == prefix:
            return family, unit
    if head.startswith("vdb"):
        return "dB", "dB"
    if "#branch" in name:
        return "current", "A"
    return "other", ""


_SI = [(1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
       (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")]


def eng(v: float, sig: int = 3) -> str:
    """Engineering notation, the way an EE reads an axis: 1k, 4.7u, 250m."""
    if v == 0:
        return "0"
    if not math.isfinite(v):
        return "-"
    a = abs(v)
    for scale, suffix in _SI:
        if a >= scale:
            x = v / scale
            s = f"{x:.{max(0, sig - 1 - int(math.floor(math.log10(abs(x)))))}f}"
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s + suffix
    return f"{v:.{sig}g}"


# ------------------------------------------------------------------- ticks

def linear_ticks(lo: float, hi: float, want: int = 6) -> list[float]:
    """Ticks on a 1/2/5 x 10^n lattice covering [lo, hi]."""
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, want)
    mag = 10 ** math.floor(math.log10(raw))
    step = next((m * mag for m in (1, 2, 5, 10) if raw <= m * mag), 10 * mag)
    first = math.ceil(lo / step) * step
    out, t = [], first
    while t <= hi + step * 1e-9:
        out.append(0.0 if abs(t) < step * 1e-9 else t)
        t += step
    return out


def log_ticks(lo: float, hi: float) -> list[float]:
    """Decade ticks, with 2/5 subdivisions when the span is narrow."""
    if lo <= 0 or hi <= lo:
        return [lo] if lo > 0 else []
    decades = math.log10(hi) - math.log10(lo)
    mults = (1,) if decades > 4 else (1, 2, 5)
    out = []
    e = math.floor(math.log10(lo))
    while 10 ** e <= hi * 1.0000001:
        for m in mults:
            v = m * 10 ** e
            if lo * 0.9999 <= v <= hi * 1.0001:
                out.append(v)
        e += 1
    return out or [lo, hi]


# ------------------------------------------------------------------ models

@dataclass
class Series:
    name: str
    xs: list
    ys: list


@dataclass
class Marker:
    """A measured quantity drawn onto the panel it was taken from."""
    label: str
    axis: str                 # "x" or "y"
    value: float
    unit: str = ""
    lsl: float | None = None
    usl: float | None = None
    ok: bool = True


@dataclass
class Panel:
    title: str
    unit: str
    series: list = field(default_factory=list)
    markers: list = field(default_factory=list)
    xlabel: str = ""
    xlog: bool = False


# How a measurement relates to the curve it came from:
#   x-position  -- a frequency or time the measurement located  -> vertical rule
#   y-level     -- a value it read off the curve                -> horizontal rule
#   span        -- a magnitude, not a level                     -> no rule at all
#
# A ripple of 8 mV is not "the curve at 8 mV"; drawing it as a horizontal line
# puts a rule near zero that reads as a DC level and means nothing. Spans stay
# in the verdict table, where a magnitude belongs.
_X_EXTRACTORS = {"crossing", "at_peak"}
_SPAN_EXTRACTORS = {"peak_to_peak"}


def panels_from(captured: dict, spec=None, values: dict | None = None) -> list:
    """Build panels from {analysis name: (xs, {vector: ys})}.

    One panel per (analysis, unit family), so magnitude and phase from the same
    sweep become two stacked panels instead of two y-scales on one.
    """
    out = []
    for aname, (xs, vecs) in captured.items():
        xlog = aname_is_swept_log(aname, spec)
        xlabel = "frequency (Hz)" if xlog else ("time (s)" if len(xs) > 1 else "")
        groups: dict = {}
        for vname, ys in vecs.items():
            family, unit = unit_family(vname)
            groups.setdefault((family, unit), []).append(Series(vname, xs, ys))
        for (family, unit), series in groups.items():
            title = f"{aname} - {family}" if len(groups) > 1 else aname
            out.append(Panel(title=title, unit=unit, series=series,
                             xlabel=xlabel, xlog=xlog))

    if spec is not None and values:
        _attach_markers(out, spec, values)
    return out


def aname_is_swept_log(aname: str, spec) -> bool:
    if spec is None:
        return False
    analysis = spec.analyses.get(aname)
    if analysis is None:
        return False
    cmd = analysis.command.lower()
    return cmd.startswith("ac") and (" dec " in cmd or " oct " in cmd)


def _attach_markers(panels: list, spec, values: dict) -> None:
    """Put each measurement on the panel whose curve it was read from."""
    for m in spec.specs:
        if m.derived or m.name not in values:
            continue
        if m.extract.get("kind") in _SPAN_EXTRACTORS:
            continue
        of = m.extract.get("of")
        target_panel = None
        for p in panels:
            if not p.title.startswith(m.analysis):
                continue
            if of is None or any(s.name == of for s in p.series):
                target_panel = p
                break
        if target_panel is None:
            continue
        axis = "x" if m.extract.get("kind") in _X_EXTRACTORS else "y"
        v = values[m.name]
        target_panel.markers.append(Marker(
            label=m.title, axis=axis, value=v, unit=m.units,
            lsl=m.limits.lsl, usl=m.limits.usl,
            ok=m.limits.ok(v) if m.limits.bounded else True))


# ------------------------------------------------------------------ scaling

class Scale:
    def __init__(self, lo, hi, px0, px1, log=False):
        self.log = log and lo > 0 and hi > lo
        if self.log:
            self.a, self.b = math.log10(lo), math.log10(hi)
        else:
            if hi <= lo:
                hi = lo + 1.0
            self.a, self.b = lo, hi
        self.px0, self.px1 = px0, px1

    def __call__(self, v: float) -> float:
        if self.log:
            if v <= 0:
                return self.px0
            v = math.log10(v)
        f = (v - self.a) / (self.b - self.a)
        return self.px0 + f * (self.px1 - self.px0)


def _declutter(labels: list, top: float, bottom: float, gap: float = 13.0) -> list:
    """Push overlapping right-gutter labels apart, keeping their order.

    One forward pass separates neighbours, then a backward pass pulls the
    stack off the bottom edge if the forward pass ran it over.
    """
    if not labels:
        return []
    out = sorted(labels, key=lambda t: t[0])
    ys = [t[0] for t in out]
    for i in range(1, len(ys)):
        if ys[i] - ys[i - 1] < gap:
            ys[i] = ys[i - 1] + gap
    overflow = ys[-1] - bottom
    if overflow > 0:
        for i in range(len(ys) - 1, -1, -1):
            ys[i] -= overflow
            if i and ys[i] - ys[i - 1] >= gap:
                break
    ys[0] = max(ys[0], top)
    return [(y, t[1], t[2], t[3]) for y, t in zip(ys, out, strict=True)]


def _pad(lo: float, hi: float, frac: float = 0.08):
    if hi == lo:
        d = abs(lo) * 0.1 or 1.0
        return lo - d, hi + d
    d = (hi - lo) * frac
    return lo - d, hi + d


# ------------------------------------------------------------------ drawing

W, H = 880, 310
PAD_L, PAD_R, PAD_T, PAD_B = 74, 132, 48, 46


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def render_panel(panel: Panel, c: dict, idx: int) -> tuple[str, dict]:
    """One panel as an <svg>. Returns (markup, hover metadata)."""
    xs_all = [x for s in panel.series for x in s.xs]
    ys_all = [y for s in panel.series for y in s.ys]
    if not xs_all or not ys_all:
        return "", {}

    xlo, xhi = min(xs_all), max(xs_all)
    ylo, yhi = min(ys_all), max(ys_all)
    # Keep the acceptance band on screen even when the curve is far outside it.
    for m in panel.markers:
        if m.axis == "y":
            for b in (m.value, m.lsl, m.usl):
                if b is not None and math.isfinite(b):
                    ylo, yhi = min(ylo, b), max(yhi, b)
    ylo, yhi = _pad(ylo, yhi)

    sx = Scale(xlo, xhi, PAD_L, W - PAD_R, log=panel.xlog)
    sy = Scale(ylo, yhi, H - PAD_B, PAD_T)

    # Right-gutter labels are collected rather than drawn inline, so they can
    # be pushed apart before rendering: two series ending at the same value
    # (a rectifier's input and output both at 0 V) would otherwise print on
    # top of each other and of the marker label.
    gutter: list = []

    o = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
         f'aria-label="{_esc(panel.title)}" class="panel" data-panel="{idx}">']
    o.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="{c["surface"]}"/>')

    # Acceptance band first, so the curve draws over it.
    for m in panel.markers:
        if m.axis == "y" and m.lsl is not None and m.usl is not None:
            y1, y2 = sy(m.usl), sy(m.lsl)
            o.append(f'<rect x="{PAD_L}" y="{min(y1, y2):.1f}" '
                     f'width="{W - PAD_R - PAD_L}" height="{abs(y2 - y1):.1f}" '
                     f'fill="{c["band"]}"/>')

    # Hairline grid, solid -- dashes are reserved for the spec limits below.
    xticks = log_ticks(xlo, xhi) if panel.xlog else linear_ticks(xlo, xhi)
    yticks = linear_ticks(ylo, yhi, 5)
    for t in xticks:
        x = sx(t)
        o.append(f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{H - PAD_B}" '
                 f'stroke="{c["grid"]}" stroke-width="1"/>')
        o.append(f'<text x="{x:.1f}" y="{H - PAD_B + 18}" text-anchor="middle" '
                 f'class="tick">{_esc(eng(t))}</text>')
    for t in yticks:
        y = sy(t)
        o.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                 f'stroke="{c["grid"]}" stroke-width="1"/>')
        o.append(f'<text x="{PAD_L - 10}" y="{y + 4:.1f}" text-anchor="end" '
                 f'class="tick">{_esc(eng(t))}</text>')

    o.append(f'<line x1="{PAD_L}" y1="{H - PAD_B}" x2="{W - PAD_R}" y2="{H - PAD_B}" '
             f'stroke="{c["axis"]}" stroke-width="1"/>')
    o.append(f'<line x1="{PAD_L}" y1="{PAD_T}" x2="{PAD_L}" y2="{H - PAD_B}" '
             f'stroke="{c["axis"]}" stroke-width="1"/>')

    if panel.xlabel:
        o.append(f'<text x="{(PAD_L + W - PAD_R) / 2:.0f}" y="{H - 8}" '
                 f'text-anchor="middle" class="axislabel">{_esc(panel.xlabel)}</text>')
    if panel.unit:
        o.append(f'<text x="14" y="{(PAD_T + H - PAD_B) / 2:.0f}" class="axislabel" '
                 f'transform="rotate(-90 14 {(PAD_T + H - PAD_B) / 2:.0f})" '
                 f'text-anchor="middle">{_esc(panel.unit)}</text>')

    # Spec limits: dashed on purpose -- here a dash really does mean threshold.
    for m in panel.markers:
        colour = c["good"] if m.ok else c["critical"]
        if m.axis == "y":
            for b in (m.lsl, m.usl):
                if b is None or not math.isfinite(b):
                    continue
                y = sy(b)
                o.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                         f'stroke="{colour}" stroke-width="1" stroke-dasharray="5 4"/>')
            y = sy(m.value)
            o.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                     f'stroke="{colour}" stroke-width="2"/>')
            unit = m.unit or panel.unit
            gutter.append((y, f'{m.label} {eng(m.value)}'
                              f'{" " + unit if unit else ""}', colour, "marker"))
        else:
            x = sx(m.value)
            o.append(f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{H - PAD_B}" '
                     f'stroke="{colour}" stroke-width="2"/>')
            o.append(f'<text x="{x + 6:.1f}" y="{PAD_T + 14}" class="marker" '
                     f'fill="{colour}">{_esc(m.label)} {_esc(eng(m.value))}'
                     f'{_esc(" " + m.unit if m.unit else "")}</text>')

    # Curves, then a direct label at each endpoint -- identity is never
    # carried by colour alone.
    meta = {"x": [], "series": []}
    for i, s in enumerate(panel.series):
        colour = c["series"][i % len(c["series"])]
        pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(s.xs, s.ys, strict=True))
        o.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" '
                 f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        gutter.append((sy(s.ys[-1]), s.name, colour, "serieslabel"))
        meta["series"].append({"name": s.name, "color": colour, "ys": s.ys})
    meta["x"] = panel.series[0].xs
    meta["unit"] = panel.unit
    meta["xlog"] = panel.xlog
    meta["geom"] = {"padL": PAD_L, "padR": PAD_R, "padT": PAD_T, "padB": PAD_B,
                    "w": W, "h": H}

    for y, text, colour, cls in _declutter(gutter, PAD_T, H - PAD_B):
        o.append(f'<text x="{W - PAD_R + 8}" y="{y + 4:.1f}" class="{cls}" '
                 f'fill="{colour}">{_esc(text)}</text>')

    # Legend: identity never rests on colour alone, even with direct labels.
    if len(panel.series) >= 2:
        lx = PAD_L
        for i, s_ in enumerate(panel.series):
            colour = c["series"][i % len(c["series"])]
            o.append(f'<line x1="{lx}" y1="{PAD_T - 12}" x2="{lx + 14}" '
                     f'y2="{PAD_T - 12}" stroke="{colour}" stroke-width="2"/>')
            o.append(f'<text x="{lx + 19}" y="{PAD_T - 8}" class="legendlabel">'
                     f'{_esc(s_.name)}</text>')
            lx += 26 + 7 * len(s_.name)

    # Crosshair layer: a vertical hairline that snaps to the nearest sample,
    # plus a dot per series. The reader aims at an x, never at a 2px curve.
    o.append(f'<g class="crosshair" style="display:none">'
             f'<line y1="{PAD_T}" y2="{H - PAD_B}" stroke="{c["ink2"]}" '
             f'stroke-width="1"/></g>')
    o.append(f'<rect class="hit" x="{PAD_L}" y="{PAD_T}" '
             f'width="{W - PAD_R - PAD_L}" height="{H - PAD_B - PAD_T}" '
             f'fill="transparent"/>')
    o.append("</svg>")
    return "\n".join(o), meta


def render_svg(panels: list, theme: str = "light") -> str:
    """All panels stacked into one standalone SVG.

    A bare .svg carries no stylesheet, so it is baked to one theme; "auto"
    means the light surface here.
    """
    c = THEMES["light" if theme == "auto" else theme]
    parts, total_h = [], 0
    for i, p in enumerate(panels):
        markup, _ = render_panel(p, c, i)
        if not markup:
            continue
        inner = markup.split(">", 1)[1].rsplit("</svg>", 1)[0]
        parts.append(f'<g transform="translate(0 {total_h})">{inner}'
                     f'<text x="{PAD_L}" y="18" class="paneltitle">'
                     f'{_esc(p.title)}</text></g>')
        total_h += H + 26
    style = (f'<style>text{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif}}'
             f'.tick{{font-size:11px;fill:{c["muted"]};font-variant-numeric:tabular-nums}}'
             f'.axislabel{{font-size:12px;fill:{c["ink2"]}}}'
             f'.paneltitle{{font-size:13px;font-weight:600;fill:{c["ink"]}}}'
             f'.serieslabel{{font-size:12px}}.marker{{font-size:11px;font-weight:600}}'
             f'.legendlabel{{font-size:11px;fill:{c["ink2"]}}}'
             f'</style>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {total_h}" '
            f'width="{W}" height="{total_h}">{style}'
            f'<rect width="{W}" height="{total_h}" fill="{c["surface"]}"/>'
            + "".join(parts) + "</svg>")


# -------------------------------------------------------------------- HTML

def render_html(panels: list, title: str, subtitle: str = "",
                theme: str = "auto", verdict: dict | None = None) -> str:
    """An interactive page: crosshair readout, legend, and a table view.

    theme="auto" follows the reader's OS setting; "light"/"dark" stamp the
    page so an explicit choice wins over it.
    """
    stamp = f' data-theme="{theme}"' if theme in ("light", "dark") else ""
    light, dark = THEMES["light"], THEMES["dark"]
    c = css_theme()
    bodies, metas = [], []
    for i, p in enumerate(panels):
        markup, meta = render_panel(p, c, i)
        if not markup:
            continue
        bodies.append(f'<section class="card"><h2>{_esc(p.title)}</h2>'
                      f'{markup}<div class="readout" hidden></div></section>')
        metas.append(meta)

    rows = ""
    if verdict and verdict.get("specs"):
        rows = "".join(
            f'<tr><td>{_esc(k)}</td><td>{_esc(v["target"])}</td>'
            f'<td class="num">{_esc(v["measured"])}</td>'
            f'<td class="{"ok" if v["pass"] else "bad"}">'
            f'{"PASS" if v["pass"] else "FAIL"}</td></tr>'
            for k, v in verdict["specs"].items())
        rows = (f'<section class="card"><h2>Specs</h2><table><thead><tr>'
                f'<th>measurement</th><th>limits</th><th>measured</th><th></th>'
                f'</tr></thead><tbody>{rows}</tbody></table></section>')

    series_light = ";".join(light["series"])
    series_dark = ";".join(dark["series"])
    return f"""<!doctype html>
<html lang="en"{stamp}>
<meta charset="utf-8">
<title>{_esc(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: light dark; {theme_css(light)} }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{ {theme_css(dark)} }}
  }}
  :root[data-theme="dark"] {{ {theme_css(dark)} }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 24px 16px 48px; background: var(--plane);
         color: var(--ink);
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  .wrap {{ max-width: 960px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: var(--ink2); font-size: 13px; margin: 0 0 20px; }}
  .card {{ background: var(--surface); border: 1px solid var(--grid);
          border-radius: 10px; padding: 16px; margin-bottom: 16px; }}
  h2 {{ font-size: 13px; font-weight: 600; margin: 0 0 10px; letter-spacing: .01em; }}
  svg text {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
  .tick {{ font-size: 11px; fill: var(--muted); font-variant-numeric: tabular-nums; }}
  .axislabel {{ font-size: 12px; fill: var(--ink2); }}
  .serieslabel {{ font-size: 12px; }}
  .legendlabel {{ font-size: 11px; fill: var(--ink2); }}
  .marker {{ font-size: 11px; font-weight: 600; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 6px; }}
  .chip {{ display: inline-flex; align-items: center; gap: 6px;
          font-size: 12px; color: var(--ink2); }}
  .key {{ width: 14px; height: 2px; border-radius: 1px; }}
  .readout {{ margin-top: 8px; font-size: 12px; color: var(--ink2);
             font-variant-numeric: tabular-nums; min-height: 1.5em; }}
  .readout b {{ color: var(--ink); font-weight: 600; }}
  .readout .key {{ display: inline-block; margin-right: 5px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ text-align: left; padding: 7px 10px;
            border-bottom: 1px solid var(--grid); }}
  th {{ color: var(--muted); font-weight: 500; font-size: 12px; }}
  .num {{ font-variant-numeric: tabular-nums; }}
  .ok {{ color: var(--good); font-weight: 600; }}
  .bad {{ color: var(--critical); font-weight: 600; }}
  @media (max-width: 640px) {{ body {{ padding: 16px 12px 32px; }} }}
</style>
<div class="wrap">
  <h1>{_esc(title)}</h1>
  <p class="sub">{_esc(subtitle)}</p>
  {"".join(bodies)}
  {rows}
</div>
<script>
const META = {_json(metas)};
const SERIES = {{light: "{series_light}".split(";"), dark: "{series_dark}".split(";")}};
function eng(v) {{
  if (v === 0) return "0";
  if (!isFinite(v)) return "-";
  const u = [[1e12,"T"],[1e9,"G"],[1e6,"M"],[1e3,"k"],[1,""],
             [1e-3,"m"],[1e-6,"u"],[1e-9,"n"],[1e-12,"p"]];
  const a = Math.abs(v);
  for (const [s, suf] of u) if (a >= s) {{
    let x = (v / s).toPrecision(4).replace(/\\.?0+$/, "");
    return x + suf;
  }}
  return v.toExponential(2);
}}
document.querySelectorAll("svg.panel").forEach(svg => {{
  const meta = META[+svg.dataset.panel];
  if (!meta) return;
  const g = meta.geom, xs = meta.x;
  const hair = svg.querySelector(".crosshair");
  const line = hair.querySelector("line");
  const card = svg.closest(".card");
  const out = card.querySelector(".readout");
  const dots = [];
  meta.series.forEach(s => {{
    const d = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    d.setAttribute("r", 4);
    d.setAttribute("fill", s.color);
    d.setAttribute("stroke", getComputedStyle(document.body).backgroundColor);
    d.setAttribute("stroke-width", 2);
    hair.appendChild(d);
    dots.push(d);
  }});
  const toData = px => {{
    const f = (px - g.padL) / (g.w - g.padR - g.padL);
    if (meta.xlog) {{
      const a = Math.log10(xs[0]), b = Math.log10(xs[xs.length - 1]);
      return Math.pow(10, a + f * (b - a));
    }}
    return xs[0] + f * (xs[xs.length - 1] - xs[0]);
  }};
  const toPx = v => {{
    let a = xs[0], b = xs[xs.length - 1], x = v;
    if (meta.xlog) {{ a = Math.log10(a); b = Math.log10(b); x = Math.log10(v); }}
    return g.padL + (x - a) / (b - a) * (g.w - g.padR - g.padL);
  }};
  const yScale = () => {{
    let lo = Infinity, hi = -Infinity;
    meta.series.forEach(s => s.ys.forEach(y => {{
      if (y < lo) lo = y; if (y > hi) hi = y;
    }}));
    return {{lo, hi}};
  }};
  function move(evt) {{
    const r = svg.getBoundingClientRect();
    const px = (evt.clientX - r.left) / r.width * g.w;
    if (px < g.padL || px > g.w - g.padR) return hide();
    const want = toData(px);
    let i = 0, best = Infinity;
    for (let k = 0; k < xs.length; k++) {{
      const d = Math.abs(xs[k] - want);
      if (d < best) {{ best = d; i = k; }}
    }}
    const x = toPx(xs[i]);
    hair.style.display = "";
    line.setAttribute("x1", x); line.setAttribute("x2", x);
    // The readout lists every series at that x -- no need to land on a curve.
    out.hidden = false;
    out.textContent = "";
    const head = document.createElement("b");
    head.textContent = eng(xs[i]) + (meta.xlog ? " Hz" : " s");
    out.appendChild(head);
    meta.series.forEach((s, j) => {{
      const yv = s.ys[i];
      const sp = document.createElement("span");
      sp.style.marginLeft = "14px";
      const key = document.createElement("span");
      key.className = "key";
      key.style.background = s.color;
      key.style.width = "14px"; key.style.height = "2px";
      sp.appendChild(key);
      const b = document.createElement("b");
      b.textContent = eng(yv) + (meta.unit ? " " + meta.unit : "");
      sp.appendChild(b);
      sp.appendChild(document.createTextNode(" " + s.name));
      out.appendChild(sp);
      const {{lo, hi}} = yScale();
      const pad = (hi - lo) * 0.08 || 1;
      const y = (g.h - g.padB) - (yv - (lo - pad)) / ((hi + pad) - (lo - pad))
                * (g.h - g.padB - g.padT);
      dots[j].setAttribute("cx", x);
      dots[j].setAttribute("cy", y);
    }});
  }}
  function hide() {{ hair.style.display = "none"; out.hidden = true; }}
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerleave", hide);
}});
</script>
"""


def _json(obj) -> str:
    import json
    return json.dumps(obj)


# --------------------------------------------------------------------- CLI

def capture(netlist: str, analyses: dict) -> dict:
    """Run each analysis once and collect its vectors."""
    import specs as specfile
    out = {}
    for name, analysis in analyses.items():
        out[name] = specfile._run_analysis(netlist, analysis)
    return out


def _cli(argv=None) -> int:
    import specs as specfile

    ap = argparse.ArgumentParser(
        prog="plot", description="Plot ngspice vectors, with the spec band on them.")
    ap.add_argument("deck", help="netlist file")
    ap.add_argument("--spec", help="spec file; its analyses are what get plotted")
    ap.add_argument("--analysis", help="ad-hoc ngspice analysis, e.g. 'tran 10u 5m'")
    ap.add_argument("--vectors", nargs="+", default=[],
                    help="vectors to capture for --analysis")
    ap.add_argument("-o", "--out", help="output path")
    ap.add_argument("--format", choices=("html", "svg"), default="html")
    ap.add_argument("--theme", choices=("auto", "light", "dark"), default="auto",
                    help="auto follows the reader's OS setting (HTML only)")
    ap.add_argument("--open", action="store_true", help="open in the default browser")
    args = ap.parse_args(argv)

    path = Path(args.deck)
    if not path.exists():
        print(f"not found: {path}", file=sys.stderr)
        return 2
    netlist = path.read_text(encoding="utf-8")

    spec = values = verdict = None
    if args.spec:
        spec = specfile.load(args.spec)
        analyses = spec.analyses
    elif args.analysis:
        if not args.vectors:
            print("--analysis needs --vectors", file=sys.stderr)
            return 2
        analyses = {"analysis": specfile.Analysis(
            name="analysis", command=args.analysis, vectors=list(args.vectors))}
    else:
        print("give either --spec or --analysis/--vectors", file=sys.stderr)
        return 2

    try:
        captured = capture(netlist, analyses)
    except specfile.SpecError as exc:
        print(f"could not measure {path}: {exc}", file=sys.stderr)
        return 1

    if spec is not None:
        verdict = specfile.verdict(spec, netlist)
        values = verdict.get("values")

    panels = panels_from(captured, spec, values)
    if not panels:
        print("nothing to plot", file=sys.stderr)
        return 1

    if args.format == "svg":
        body = render_svg(panels, args.theme)
    else:
        sub = path.name
        if verdict and not verdict.get("error"):
            sub += f"  -  {'PASS' if verdict['pass'] else 'FAIL'}: {verdict['measured']}"
        body = render_html(panels, spec.name if spec else path.stem, sub,
                           args.theme, verdict)

    out = Path(args.out) if args.out else path.with_suffix("." + args.format)
    out.write_text(body, encoding="utf-8")
    print(f"wrote {out}  ({out.stat().st_size / 1024:.1f} KB, {len(panels)} panel(s))")

    if args.open:
        import webbrowser
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
