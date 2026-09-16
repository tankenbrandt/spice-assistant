#!/usr/bin/env python3
"""Run a deck in LTspice and read the result, alongside ngspice.

`speccheck` measures with ngspice. ngspice is free software; LTspice is what
most analog designers actually have open. Agreement between the two is a much
stronger claim than either one alone, so this module makes LTspice a second
measurement backend rather than just the netlister it was in `symlib`.

Two things stand between an ngspice deck and LTspice:

* **`.control` blocks.** ngspice takes its analysis commands from a control
  block; LTspice has never understood one and silently simulates nothing.
  `deck_for` strips the block and re-states the analysis as a directive.
* **Vector names.** A spec asks for `vm(out)`, `vdb(out)`, `vin#branch`.
  LTspice's raw file holds one complex `V(out)` per point and calls the source
  current `I(Vin)`. `vector` evaluates the ngspice spelling against that.

    python ltspice.py baseline/rc_lowpass.cir --analysis "ac dec 20 1 1meg" \
        --vectors vdb(out)

Everything degrades cleanly: with no LTspice installed, `available()` is False
and callers stay on ngspice.
"""

import argparse
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import symlib

# LTspice writes the operating point of an AC/tran run to a separate <name>.op.raw;
# the analysis data we want is always in <name>.raw.
_TIMEOUT = 120


def available(exe=None) -> bool:
    """True if an LTspice executable can be found."""
    return find_executable(exe) is not None


def find_executable(explicit=None):
    """Locate LTspice.exe. Delegates to symlib so there is one search path."""
    return symlib.find_executable(explicit)


# ----------------------------------------------------------------- the deck

_CONTROL = re.compile(r"^\s*\.control\b.*?^\s*\.endc\b[^\n]*\n?",
                      re.MULTILINE | re.DOTALL | re.IGNORECASE)


def strip_control(netlist: str) -> str:
    """Remove every `.control ... .endc` block.

    LTspice does not implement control blocks. Left in place it does not error
    -- it runs the deck with no analysis at all and writes an empty raw file,
    which is the confusing failure this avoids.
    """
    return _CONTROL.sub("", netlist)


def directive(command: str) -> str:
    """Turn an ngspice control-block command into an LTspice dot directive.

    `ac dec 20 10 10meg` -> `.ac dec 20 10 10meg`. A command that is already a
    directive is passed through, so callers may hand over either spelling.
    """
    cmd = command.strip()
    return cmd if cmd.startswith(".") else "." + cmd


class UnsupportedOption(Exception):
    """An ngspice analysis option with no LTspice equivalent.

    Raised rather than ignored: an option that silently does nothing produces
    a number that looks fine and is wrong. The differential-pair spec found
    this the hard way -- dropping its `alter` lines left both inputs driven in
    phase, so the "differential" gain was really the common-mode gain.
    """


# `alter @vinp[acmag]=0.5` -- ngspice's way of re-driving a source between
# analyses without editing the deck.
_ALTER = re.compile(
    r"^\s*alter\s+@(\w+)\s*\[\s*(acmag|acphase)\s*\]\s*=\s*(\S+)\s*$", re.IGNORECASE)

# The AC spec inside a source line: `AC`, optionally followed by magnitude
# and phase.
_ACSPEC = re.compile(r"\bac\b(\s+[-+0-9.eE]\S*)?(\s+[-+0-9.eE]\S*)?", re.IGNORECASE)


def _set_ac(netlist: str, source: str, mag=None, phase=None) -> str:
    """Rewrite one source's AC magnitude/phase in the deck text.

    LTspice states them positionally (`AC <mag> <phase>`), which is how it
    expresses what ngspice does with `alter`.
    """
    out, found = [], False
    for line in netlist.splitlines():
        parts = line.split()
        if not parts or parts[0].lower() != source.lower():
            out.append(line)
            continue
        found = True
        m = _ACSPEC.search(line)
        cur_mag = (m.group(1) or "").strip() if m else ""
        cur_phase = (m.group(2) or "").strip() if m else ""
        new_mag = cur_mag or "1" if mag is None else str(mag)
        new_phase = cur_phase or "0" if phase is None else str(phase)
        spec = f"AC {new_mag} {new_phase}"
        out.append(_ACSPEC.sub(spec, line, count=1) if m else line.rstrip() + " " + spec)
    if not found:
        raise UnsupportedOption(
            f"alter targets source {source!r}, which is not in the deck")
    return "\n".join(out)


def apply_options(netlist: str, options) -> str:
    """Fold an analysis's ngspice `options` into the deck text for LTspice."""
    for opt in options or []:
        opt = opt.strip()
        m = _ALTER.match(opt)
        if m:
            src, what, value = m.group(1), m.group(2).lower(), m.group(3)
            kw = {"mag" if what == "acmag" else "phase": value}
            netlist = _set_ac(netlist, src, **kw)
            continue
        # `set units=degrees` only tells ngspice how to print a phase; this
        # module always returns degrees, so honouring it is a no-op.
        if re.match(r"^set\s+units\s*=\s*degrees\s*$", opt, re.IGNORECASE):
            continue
        if re.match(r"^set\s+wr_singlescale\s*$", opt, re.IGNORECASE):
            continue
        # A bare `op` before an `ac` is an ngspice workaround: a standalone
        # `ac` there linearises around an ill-conditioned bias point. LTspice
        # always solves the operating point before an AC analysis, so the
        # workaround has no counterpart and nothing needs doing -- see the
        # note in specs_lib/ce_amp_bias_and_gain.yaml.
        if re.match(r"^op\s*$", opt, re.IGNORECASE):
            continue
        raise UnsupportedOption(
            f"no LTspice equivalent for analysis option {opt!r}; "
            "the LTspice run would silently measure something else")
    return netlist


def deck_for(netlist: str, command: str, options=None) -> str:
    """An LTspice-runnable deck: the circuit, one analysis directive, `.end`.

    The first line of a SPICE deck is its title and is never parsed as a
    component, so it is kept exactly where it is.
    """
    body = strip_control(netlist)
    body = apply_options(body, options)
    # Drop any existing .end so the directive lands before it, not after,
    # where LTspice would ignore it.
    body = re.sub(r"^\s*\.end\s*$", "", body, flags=re.MULTILINE | re.IGNORECASE)
    return body.rstrip() + "\n" + directive(command) + "\n.end\n"


# ------------------------------------------------------------ the raw file

@dataclass
class Raw:
    """One parsed LTspice ASCII raw file."""
    plotname: str = ""
    flags: list = field(default_factory=list)
    variables: list = field(default_factory=list)   # names, in file order
    points: list = field(default_factory=list)      # [[complex|float per var]]

    @property
    def complex_data(self) -> bool:
        return "complex" in self.flags

    def column(self, index: int) -> list:
        return [p[index] for p in self.points]

    def index_of(self, name: str):
        """Case-insensitive variable lookup; LTspice capitalises V() and I()."""
        low = name.lower()
        for i, v in enumerate(self.variables):
            if v.lower() == low:
                return i
        return None


def _number(token: str):
    """Parse one raw value: `1.0e+00` or the `real,imag` pair of a complex run."""
    token = token.strip()
    if "," in token:
        re_s, im_s = token.split(",", 1)
        return complex(float(re_s), float(im_s))
    return float(token)


def parse_raw(text: str) -> Raw:
    """Parse an LTspice ASCII raw file (`-ascii`).

    The format is a header of `Key: value` lines, a `Variables:` table, then
    `Values:` -- one block per point, the first line of which carries the point
    index before the first variable's value.
    """
    raw = Raw()
    lines = text.splitlines()
    i = 0
    nvars = npoints = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("plotname:"):
            raw.plotname = stripped.split(":", 1)[1].strip()
        elif low.startswith("flags:"):
            raw.flags = stripped.split(":", 1)[1].split()
        elif low.startswith("no. variables:"):
            nvars = int(stripped.split(":", 1)[1])
        elif low.startswith("no. points:"):
            npoints = int(stripped.split(":", 1)[1])
        elif low.startswith("variables:"):
            for j in range(nvars):
                parts = lines[i + 1 + j].split("\t")
                # index, name, type -- with leading empty field from the tab
                fields = [p for p in parts if p.strip()]
                raw.variables.append(fields[1].strip() if len(fields) > 1 else "")
            i += nvars
        elif low.startswith("values:"):
            i += 1
            break
        i += 1

    if not nvars:
        raise ValueError("raw file declares no variables")

    # Each point: the index, then one value per variable, tab separated and
    # spread over nvars lines.
    tokens = []
    for line in lines[i:]:
        if not line.strip():
            continue
        tokens.extend(t for t in line.split("\t") if t.strip())

    per_point = nvars + 1  # the leading point index
    total = len(tokens) // per_point
    if npoints and total < npoints:
        raise ValueError(
            f"raw file claims {npoints} points but holds {total}; "
            "the run was probably cut short")
    for p in range(total):
        chunk = tokens[p * per_point:(p + 1) * per_point]
        raw.points.append([_number(t) for t in chunk[1:]])
    return raw


# -------------------------------------------------------- ngspice vectors

# The accessor is longest-match: `db` before the single letters, or `vdb(out)`
# parses as v + d and then fails on the leftover `b`.
_VEC = re.compile(r"^(v|i)(db|m|p|r|i)?\(([^)]+)\)$", re.IGNORECASE)


def _as_complex(v) -> complex:
    return v if isinstance(v, complex) else complex(v, 0.0)


def vector(raw: Raw, name: str) -> list:
    """Evaluate an ngspice vector name against LTspice's raw data.

    Handles `v(out)`, the AC accessors `vm/vp/vdb/vr/vi(out)`, `i(r1)`, and
    ngspice's `vin#branch` spelling of a source current.
    """
    name = name.strip()

    # ngspice writes a source current as `vin#branch`; LTspice as `I(Vin)`.
    if "#branch" in name.lower():
        src = name.lower().split("#")[0]
        idx = raw.index_of(f"I({src})")
        if idx is None:
            raise KeyError(f"{name}: LTspice has no I({src}) "
                           f"(it has: {', '.join(raw.variables)})")
        return [_as_complex(v).real if not raw.complex_data else abs(_as_complex(v))
                for v in raw.column(idx)]

    m = _VEC.match(name)
    if not m:
        idx = raw.index_of(name)
        if idx is None:
            raise KeyError(f"{name}: not in the raw file "
                           f"(it has: {', '.join(raw.variables)})")
        return [_as_complex(v).real for v in raw.column(idx)]

    # An unmatched optional *group* is None, not "" -- a plain `v(out)` has no
    # accessor and would otherwise crash here rather than read as magnitude.
    kind, accessor, node = m.group(1).lower(), (m.group(2) or "").lower(), m.group(3)
    idx = raw.index_of(f"{kind.upper()}({node})")
    if idx is None:
        raise KeyError(f"{name}: LTspice has no {kind.upper()}({node}) "
                       f"(it has: {', '.join(raw.variables)})")
    col = [_as_complex(v) for v in raw.column(idx)]

    if accessor in ("", "r") and not raw.complex_data:
        return [c.real for c in col]
    if accessor == "" or accessor == "m":
        # Plain v(x) on a complex run is the magnitude, as ngspice's wrdata
        # reports it for an AC sweep.
        return [abs(c) for c in col]
    if accessor == "r":
        return [c.real for c in col]
    if accessor == "i":
        return [c.imag for c in col]
    if accessor == "p":
        return [math.degrees(math.atan2(c.imag, c.real)) for c in col]
    if accessor == "db":
        return [20.0 * math.log10(abs(c)) if abs(c) > 0 else -math.inf for c in col]
    raise KeyError(f"{name}: unsupported accessor {accessor!r}")


def sweep_axis(raw: Raw) -> list:
    """The x axis: frequency, time or the swept source, as a real list.

    LTspice marks compressed transient points by writing the time negative;
    the magnitude is the real value, so the axis is taken as an absolute.

    An operating point has no sweep variable at all -- its first column is the
    first node voltage. Returning that as the axis would quietly hand
    measurements a voltage where they expect time or frequency, so a single
    zero is returned for the one point instead.
    """
    if not raw.variables:
        raise ValueError("raw file has no variables")
    if raw.plotname.lower().startswith("operating point"):
        return [0.0] * len(raw.points)
    return [abs(_as_complex(v).real) for v in raw.column(0)]


# ------------------------------------------------------------------- runner

def run(netlist: str, command: str, exe=None, keep=None, options=None):
    """Simulate one analysis in LTspice; return its parsed raw file."""
    exe = find_executable(exe)
    if exe is None:
        raise RuntimeError("LTspice not found; set LTSPICE_EXE or pass --exe")

    tmp = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="ltspice-"))
    tmp.mkdir(parents=True, exist_ok=True)
    deck = tmp / "deck.net"
    deck.write_text(deck_for(netlist, command, options), encoding="utf-8")

    proc = subprocess.run(
        [str(exe), "-b", "-ascii", "-Run", str(deck)],
        capture_output=True, text=True, timeout=_TIMEOUT)

    rawfile = deck.with_suffix(".raw")
    if not rawfile.exists():
        log = deck.with_suffix(".log")
        detail = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        raise RuntimeError(
            "LTspice wrote no raw file.\n"
            f"exit={proc.returncode}\n{(detail or proc.stdout or proc.stderr)[-800:]}")
    try:
        return parse_raw(rawfile.read_text(encoding="utf-8", errors="replace"))
    finally:
        if keep is None:
            shutil.rmtree(tmp, ignore_errors=True)


def run_analysis(netlist: str, analysis, exe=None):
    """LTspice's answer to `specs._run_analysis`.

    Returns `(sweep axis, {vector name: values})` -- the same shape the ngspice
    path returns, so `specs.measure` can read either without changing.
    """
    raw = run(netlist, analysis.command, exe=exe,
              options=getattr(analysis, "options", None))
    return sweep_axis(raw), {v: vector(raw, v) for v in analysis.vectors}


# ---------------------------------------------------------------------- CLI

def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ltspice", description="Run an ngspice deck in LTspice and print vectors.")
    ap.add_argument("deck", help="netlist file")
    ap.add_argument("--analysis", required=True, help="e.g. 'ac dec 20 1 1meg'")
    ap.add_argument("--vectors", nargs="+", default=[], help="e.g. vdb(out)")
    ap.add_argument("--exe", help="path to LTspice.exe")
    ap.add_argument("-n", type=int, default=5, help="rows to print (0 = all)")
    args = ap.parse_args(argv)

    exe = find_executable(args.exe)
    if exe is None:
        print("LTspice not found. Set LTSPICE_EXE or pass --exe.", file=sys.stderr)
        return 2

    netlist = Path(args.deck).read_text(encoding="utf-8", errors="replace")
    raw = run(netlist, args.analysis, exe=exe)
    print(f"{raw.plotname}  flags={' '.join(raw.flags)}  "
          f"{len(raw.points)} points", file=sys.stderr)
    print(f"variables: {', '.join(raw.variables)}", file=sys.stderr)

    names = args.vectors or []
    xs = sweep_axis(raw)
    cols = {n: vector(raw, n) for n in names}
    rows = range(len(xs)) if args.n == 0 else range(min(args.n, len(xs)))
    if names:
        print("\t".join(["x", *names]))
        for i in rows:
            print("\t".join([f"{xs[i]:.6g}", *(f"{cols[n][i]:.6g}" for n in names)]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
