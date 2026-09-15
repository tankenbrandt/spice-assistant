#!/usr/bin/env python3
"""Declarative circuit specs: measure any deck, not just the benchmark's.

`speccheck.py` hard-codes one Python function per benchmark circuit, which is
fine for a fixed test set and useless for your own amplifier. This module takes
the same measurement vocabulary those functions are written in -- run an
analysis, pull a quantity out of the resulting vectors, compare it to a limit
-- and makes it declarative, so a spec is a file rather than a patch:

    circuit: sallen_key
    analyses:
      ac: {command: ac dec 200 10 1meg, vectors: [vdb(out)]}
    measurements:
      - name: f3db
        analysis: ac
        extract: {kind: crossing, of: vdb(out), level: first - 3, direction: falling}
        target: 1000
        tol: 5%

The same file drives both layers: `speccheck` uses it for the nominal verdict
and `robustness` uses it for Monte Carlo, sensitivity and worst-case, so a
circuit is described once and signed off end to end.

Specs load from YAML (if PyYAML is installed) or JSON. Everything here is pure
ngspice -- no API calls.
"""

import ast
import json
import math
import operator
import re
from dataclasses import dataclass, field
from pathlib import Path

import speccheck

# --------------------------------------------------------------- expressions

# Level and derived-value expressions come out of a spec file, so they are
# evaluated through an AST whitelist rather than bare eval(): names must be
# bound by the caller and only arithmetic and the functions below are allowed.
_FUNCS = {
    "abs": abs, "sqrt": math.sqrt, "log10": math.log10, "log": math.log,
    "exp": math.exp, "min": min, "max": max, "pow": pow,
}
_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class SpecError(ValueError):
    """A spec file is malformed, or asks for something that cannot be done."""


def safe_eval(expr, names: dict) -> float:
    """Evaluate an arithmetic expression over `names`.

    A plain number passes straight through, so `level: 0.5` and
    `level: "max / sqrt(2)"` are both valid spellings of a level.
    """
    if isinstance(expr, (int, float)) and not isinstance(expr, bool):
        return float(expr)
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError as exc:
        raise SpecError(f"cannot parse expression {expr!r}: {exc}") from exc

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return float(node.value)
            raise SpecError(f"only numbers are allowed as constants, got {node.value!r}")
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise SpecError(
                    f"unknown name {node.id!r} in {expr!r}; "
                    f"available: {', '.join(sorted(names))}")
            return float(names[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            try:
                return _BINOPS[type(node.op)](walk(node.left), walk(node.right))
            except ZeroDivisionError as exc:
                raise SpecError(
                    f"division by zero evaluating {expr!r}; a measured value "
                    "it depends on is 0 (is the circuit doing anything?)"
                ) from exc
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
            return _UNARYOPS[type(node.op)](walk(node.operand))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
                raise SpecError(f"function not allowed in {expr!r}")
            if node.keywords:
                raise SpecError("keyword arguments are not allowed in expressions")
            try:
                return float(_FUNCS[node.func.id](*[walk(a) for a in node.args]))
            except SpecError:
                raise
            except (ValueError, ZeroDivisionError, OverflowError) as exc:
                raise SpecError(
                    f"{node.func.id}() is undefined for the measured value "
                    f"in {expr!r}: {exc}") from exc
        raise SpecError(f"expression element not allowed in {expr!r}: "
                        f"{type(node).__name__}")

    return float(walk(tree))


# ------------------------------------------------------------------- limits

_PCT_RE = re.compile(r"^\s*([-+]?[\d.eE+-]+)\s*%\s*$")


def _as_fraction(tol) -> float:
    """Accept 5%, 0.05 or '5 %' as the same tolerance."""
    if isinstance(tol, (int, float)):
        return float(tol)
    m = _PCT_RE.match(str(tol))
    if not m:
        raise SpecError(f"cannot read tolerance {tol!r}; use e.g. 5% or 0.05")
    return float(m.group(1)) / 100.0


@dataclass
class Limits:
    """The acceptance band for one measurement."""
    target: float | None = None
    lsl: float | None = None
    usl: float | None = None
    units: str = ""

    def ok(self, v: float) -> bool:
        if self.lsl is not None and v < self.lsl:
            return False
        if self.usl is not None and v > self.usl:
            return False
        return True

    @property
    def bounded(self) -> bool:
        return self.lsl is not None or self.usl is not None

    def describe(self) -> str:
        u = f" {self.units}" if self.units else ""
        if self.target and self.lsl is not None and self.usl is not None:
            tol = max(abs(self.usl - self.target), abs(self.target - self.lsl))
            return f"{self.target:g}{u} +/-{tol / abs(self.target):.1%}"
        if self.lsl is not None and self.usl is not None:
            return f"{self.lsl:g} .. {self.usl:g}{u}"
        if self.usl is not None:
            return f"<= {self.usl:g}{u}"
        if self.lsl is not None:
            return f">= {self.lsl:g}{u}"
        return "(unbounded)"

    def deviation(self, v: float) -> str:
        """Human-readable distance from target, for the measured column."""
        if self.target:
            return f" ({(v - self.target) / abs(self.target):+.1%})"
        return ""


def limits_from(d: dict, units: str) -> Limits:
    """Build limits from any of: target+tol, target+abs_tol, min/max.

    An `informational: true` measurement needs no band -- it is taken and
    reported, but does not decide pass or fail. That is what intermediate
    quantities are: a band edge exists so Q can be derived from it, not
    because anyone specified it.
    """
    target = d.get("target")
    lsl, usl = d.get("min"), d.get("max")
    if target is not None:
        target = float(target)
        if "tol" in d:
            span = abs(target) * _as_fraction(d["tol"])
        elif "abs_tol" in d:
            span = abs(float(d["abs_tol"]))
        else:
            span = None
        if span is not None:
            lsl = target - span if lsl is None else float(lsl)
            usl = target + span if usl is None else float(usl)
    lims = Limits(target=target,
                  lsl=None if lsl is None else float(lsl),
                  usl=None if usl is None else float(usl),
                  units=units)
    if not lims.bounded and not d.get("informational"):
        raise SpecError(
            "a measurement needs an acceptance band: give target+tol, "
            "target+abs_tol, or min/max -- or mark it 'informational: true' "
            "if it only exists to be referenced by another measurement")
    return lims


# -------------------------------------------------------------- spec objects

@dataclass
class Analysis:
    """One ngspice analysis and the vectors to capture from it."""
    name: str
    command: str
    vectors: list = field(default_factory=list)
    options: list = field(default_factory=list)

    def control_body(self, datafile: str = "meas.txt") -> str:
        lines = ["set wr_singlescale", *self.options, self.command]
        if self.vectors:
            lines.append(f"wrdata {datafile} " + " ".join(self.vectors))
        return "\n".join(lines) + "\n"


@dataclass
class Measurement:
    name: str
    limits: Limits
    label: str = ""
    units: str = ""
    analysis: str = ""
    extract: dict = field(default_factory=dict)
    expr: str = ""            # derived measurements only
    informational: bool = False   # measured and reported, but not a spec

    @property
    def derived(self) -> bool:
        return bool(self.expr)

    @property
    def title(self) -> str:
        return self.label or self.name


@dataclass
class CircuitSpec:
    name: str
    description: str = ""
    analyses: dict = field(default_factory=dict)
    measurements: list = field(default_factory=list)
    source: Path | None = None

    @property
    def specs(self) -> list:
        """Measurements that decide pass/fail, excluding informational ones."""
        return [m for m in self.measurements if not m.informational]

    def measurement(self, name: str) -> Measurement:
        for m in self.measurements:
            if m.name == name:
                return m
        raise SpecError(f"no measurement named {name!r}")


# -------------------------------------------------------------------- loading

def _load_mapping(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError as exc:                              # pragma: no cover
        raise SpecError(
            f"{path.name} is YAML but PyYAML is not installed. "
            "Run: pip install pyyaml -- or write the spec as .json."
        ) from exc
    return yaml.safe_load(text)


def parse(data: dict, source: Path | None = None) -> CircuitSpec:
    """Build a CircuitSpec from an already-loaded mapping."""
    if not isinstance(data, dict):
        raise SpecError("a spec file must be a mapping at the top level")
    if "measurements" not in data:
        raise SpecError("spec has no 'measurements' section")

    analyses = {}
    for name, a in (data.get("analyses") or {}).items():
        if isinstance(a, str):
            a = {"command": a}
        if "command" not in a:
            raise SpecError(f"analysis {name!r} has no 'command'")
        vectors = a.get("vectors") or []
        if isinstance(vectors, str):
            vectors = [vectors]
        options = a.get("options") or []
        if isinstance(options, str):
            options = [options]
        analyses[name] = Analysis(name=name, command=str(a["command"]),
                                  vectors=list(vectors), options=list(options))

    measurements = []
    for raw in data["measurements"]:
        if "name" not in raw:
            raise SpecError("every measurement needs a 'name'")
        name = str(raw["name"])
        units = str(raw.get("units", ""))
        extract = raw.get("extract") or {}
        if isinstance(extract, str):
            extract = {"kind": extract}
        expr = str(raw.get("expr", ""))
        if not expr and not extract:
            raise SpecError(f"measurement {name!r} needs either 'extract' or 'expr'")
        if expr and extract:
            raise SpecError(f"measurement {name!r} has both 'extract' and 'expr'")

        analysis = str(raw.get("analysis", ""))
        if not expr:
            if not analysis:
                if len(analyses) == 1:
                    analysis = next(iter(analyses))
                else:
                    raise SpecError(
                        f"measurement {name!r} must name an analysis "
                        f"(choices: {', '.join(sorted(analyses)) or 'none defined'})")
            if analysis not in analyses:
                raise SpecError(
                    f"measurement {name!r} refers to unknown analysis {analysis!r}")
            kind = extract.get("kind")
            if kind not in EXTRACTORS:
                raise SpecError(
                    f"measurement {name!r} uses unknown extractor {kind!r}; "
                    f"available: {', '.join(sorted(EXTRACTORS))}")

        measurements.append(Measurement(
            name=name, label=str(raw.get("label", "")), units=units,
            analysis=analysis, extract=dict(extract), expr=expr,
            informational=bool(raw.get("informational", False)),
            limits=limits_from(raw, units)))

    names = [m.name for m in measurements]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SpecError(f"duplicate measurement names: {', '.join(sorted(dupes))}")

    return CircuitSpec(
        name=str(data.get("circuit") or (source.stem if source else "circuit")),
        description=str(data.get("description", "")),
        analyses=analyses, measurements=measurements, source=source)


def load(path) -> CircuitSpec:
    """Load a spec from a .yaml/.yml/.json file."""
    path = Path(path)
    if not path.exists():
        raise SpecError(f"spec file not found: {path}")
    return parse(_load_mapping(path), source=path)


# ----------------------------------------------------------------- extractors
#
# Each extractor receives (xs, ys, cfg, ctx) and returns a float. `xs` is the
# analysis sweep variable (frequency or time), `ys` the selected vector, and
# `ctx` carries the other vectors from the same analysis plus every
# measurement already resolved -- so a spec can say `at: f_peak` and read a
# curve at a frequency an earlier measurement found.

def _window(xs, ys, cfg):
    """Restrict to the tail of the sweep, e.g. 'last 20%' for the
    steady-state part of a transient."""
    spec = cfg.get("window")
    if not spec:
        return xs, ys
    m = re.match(r"(?i)^\s*last\s+([\d.]+)\s*%\s*$", str(spec))
    if not m:
        raise SpecError(f"cannot read window {spec!r}; use e.g. 'last 20%'")
    frac = 1.0 - float(m.group(1)) / 100.0
    x0 = xs[0] + frac * (xs[-1] - xs[0])
    pairs = [(x, y) for x, y in zip(xs, ys) if x >= x0]
    if not pairs:
        return xs, ys
    return [p[0] for p in pairs], [p[1] for p in pairs]


@dataclass
class Context:
    """What an extractor can see besides its own curve."""
    vectors: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)


def _stats(ys, ctx=None):
    """Names available inside a level/position expression.

    The curve's own statistics shadow earlier measurements, so `max` always
    means this curve's maximum however the rest of the spec is named.
    """
    names = dict(ctx.values) if ctx else {}
    names.update({"first": ys[0], "last": ys[-1], "max": max(ys), "min": min(ys),
                  "mean": sum(ys) / len(ys), "peak": max(ys)})
    return names


def x_at_peak(xs, ys, cfg, ctx):
    xs, ys = _window(xs, ys, cfg)
    return xs[ys.index(max(ys))]


def e_max(xs, ys, cfg, ctx):
    return max(_window(xs, ys, cfg)[1])


def e_min(xs, ys, cfg, ctx):
    return min(_window(xs, ys, cfg)[1])


def e_mean(xs, ys, cfg, ctx):
    w = _window(xs, ys, cfg)[1]
    return sum(w) / len(w)


def e_ptp(xs, ys, cfg, ctx):
    w = _window(xs, ys, cfg)[1]
    return max(w) - min(w)


def e_crossing(xs, ys, cfg, ctx):
    """First crossing of a level, which may be relative to the curve itself.

    'first - 3' is the -3 dB point of a low-pass (flat at DC); 'max - 3' the
    same for a band-pass or high-pass; 'max / sqrt(2)' the half-power point of
    a linear magnitude.
    """
    xs, ys = _window(xs, ys, cfg)
    search = str(cfg.get("search", "all")).lower()
    if search not in ("all", "before_peak", "after_peak"):
        raise SpecError(f"search must be all, before_peak or after_peak, got {search!r}")
    if search in ("before_peak", "after_peak"):
        i = ys.index(max(ys))
        xs, ys = (xs[:i + 1], ys[:i + 1]) if search == "before_peak" else (xs[i:], ys[i:])
    level = safe_eval(cfg.get("level", 0), _stats(ys, ctx))
    direction = str(cfg.get("direction", "falling")).lower()
    if direction not in ("rising", "falling"):
        raise SpecError(f"direction must be rising or falling, got {direction!r}")
    hit = speccheck.crossing(xs, ys, level, rising=(direction == "rising"))
    if hit is None:
        raise SpecError(
            f"no {direction} crossing of {level:.4g} found in "
            f"{cfg.get('of', 'the vector')}")
    return hit


def e_value_at(xs, ys, cfg, ctx):
    """Linearly interpolated value of the curve at a given sweep point."""
    if "at" not in cfg:
        raise SpecError("value_at needs an 'at' sweep position")
    at = safe_eval(cfg["at"], _stats(ys, ctx))
    if at <= xs[0]:
        return ys[0]
    if at >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= at <= xs[i + 1]:
            span = xs[i + 1] - xs[i]
            if span == 0:
                return ys[i]
            return ys[i] + (at - xs[i]) / span * (ys[i + 1] - ys[i])
    return ys[-1]


EXTRACTORS = {
    "max": e_max,
    "min": e_min,
    "mean": e_mean,
    "peak_to_peak": e_ptp,
    "crossing": e_crossing,
    "value_at": e_value_at,
    "at_peak": x_at_peak,
}


# ------------------------------------------------------------------ evaluation

def _run_analysis(netlist: str, analysis: Analysis):
    """Run one analysis and return (sweep axis, {vector name: values})."""
    if not analysis.vectors:
        raise SpecError(
            f"analysis {analysis.name!r} captures no vectors; add a "
            "'vectors:' list so there is something to measure")
    d, out = speccheck.run_measurement(netlist, analysis.control_body())
    try:
        xs, cols = speccheck.read_wrdata(d / "meas.txt", len(analysis.vectors))
    except RuntimeError as exc:
        raise SpecError(
            f"analysis {analysis.name!r} produced no usable data ({exc}). "
            f"ngspice said:\n{out.strip()[-600:]}") from exc
    return xs, dict(zip(analysis.vectors, cols))


def measure(spec: CircuitSpec, netlist: str) -> dict:
    """Run every measurement in the spec against `netlist`.

    Each analysis runs once even if several measurements read from it.
    Returns {name: float}; raises SpecError if a measurement cannot be taken.
    """
    needed = {m.analysis for m in spec.measurements if not m.derived}
    captured = {n: _run_analysis(netlist, spec.analyses[n]) for n in sorted(needed)}

    # Declared order is resolution order, so a measurement may reference any
    # measurement above it -- both in `expr` and inside an extractor.
    values = {}
    for m in spec.measurements:
        if m.derived:
            values[m.name] = safe_eval(m.expr, dict(values))
            continue
        xs, vecs = captured[m.analysis]
        of = m.extract.get("of")
        if of is None:
            if len(vecs) != 1:
                raise SpecError(
                    f"measurement {m.name!r} must say which vector to use "
                    f"('of'), because analysis {m.analysis!r} captures {len(vecs)}")
            of = next(iter(vecs))
        if of not in vecs:
            raise SpecError(
                f"measurement {m.name!r} reads {of!r}, which analysis "
                f"{m.analysis!r} does not capture (it has: {', '.join(vecs)})")
        ctx = Context(vectors=vecs, values=dict(values))
        values[m.name] = float(
            EXTRACTORS[m.extract["kind"]](xs, vecs[of], m.extract, ctx))
    return values


def verdict(spec: CircuitSpec, netlist: str) -> dict:
    """Measure and judge, in the shape speccheck.check() returns.

    Never raises: a measurement failure becomes a failed result carrying the
    reason, so a batch run reports rather than aborts.
    """
    try:
        values = measure(spec, netlist)
    except Exception as exc:                # noqa: BLE001 - reported, not raised
        return {"pass": False, "target": describe(spec), "measured": None,
                "error": f"{type(exc).__name__}: {exc}"}

    subs, info = {}, {}
    for m in spec.measurements:
        v = values[m.name]
        u = f" {m.units}" if m.units else ""
        row = {
            "pass": m.limits.ok(v),
            "target": m.limits.describe(),
            "measured": f"{v:.4g}{u}{m.limits.deviation(v)}",
            "value": v,
        }
        (info if m.informational else subs)[m.title] = row
    return {
        "pass": all(r["pass"] for r in subs.values()),
        "target": describe(spec),
        "measured": "; ".join(f"{k}: {r['measured']}" for k, r in subs.items()),
        "error": None,
        "specs": subs,
        "informational": info,
        "values": values,
    }


def describe(spec: CircuitSpec) -> str:
    return "; ".join(f"{m.title} = {m.limits.describe()}" for m in spec.specs)
