#!/usr/bin/env python3
"""spice-assistant: natural language -> ngspice netlist -> simulate -> self-heal.

Generates an ngspice netlist from a plain-English circuit description using the
Anthropic API, runs it through ngspice in batch mode, and if the simulation
fails, feeds the error back to the model for a corrected netlist. Retries are
capped at 4 total attempts.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import anthropic

PROJECT_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path | None = None) -> None:
    """Minimal stdlib-only .env loader (KEY=value, optional quotes).

    Real environment variables always win, so an exported key overrides the
    file. Lives here rather than in the benchmark so that every entry point
    -- CLI, benchmark, study -- picks the key up the same way.
    """
    path = path or PROJECT_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


MODEL = "claude-sonnet-5"
MAX_ATTEMPTS = 4

SYSTEM_PROMPT = (
    "You are an expert analog circuit designer who writes ngspice netlists. "
    "Given a natural-language circuit description, output ONLY a single valid "
    "ngspice netlist and nothing else. Rules:\n"
    "- No explanation, no commentary, no markdown, no ``` code fences.\n"
    "- The first line must be a title/comment line (ngspice treats the first "
    "line of a deck as a comment).\n"
    "- Put ALL analysis and output commands inside a single .control block, "
    "using interactive command forms (op, dc, ac, tran, print), matching the "
    "analyses the description implies. Do NOT use standalone dot-analysis "
    "lines (.op, .ac, .tran, .dc) anywhere in the deck, do NOT call 'run', "
    "and never use 'plot' (batch mode has no plotting).\n"
    "- End the deck with a .end line.\n"
    "- Use only standard ngspice syntax that runs in batch mode (ngspice -b)."
)


def strip_fences(text: str) -> str:
    """Remove any Markdown code fences the model may have added despite instructions."""
    text = text.strip()
    # Strip a leading ```lang fence and a trailing ``` fence if present.
    text = re.sub(r"^```[^\n]*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    # Also drop any stray fence-only lines just in case.
    lines = [ln for ln in text.splitlines() if ln.strip() != "```"]
    return "\n".join(lines).strip() + "\n"


# Cumulative API usage for this process (benchmark reads this for cost reporting).
USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def _message_text(description_messages, system=SYSTEM_PROMPT) -> str:
    """Single Anthropic call that returns concatenated text output."""
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=system,
        messages=description_messages,
    )
    USAGE["calls"] += 1
    USAGE["input_tokens"] += resp.usage.input_tokens
    USAGE["output_tokens"] += resp.usage.output_tokens
    return "".join(block.text for block in resp.content if block.type == "text")


def generate_netlist(description: str) -> str:
    """Ask the model for a netlist implementing `description`."""
    raw = _message_text(
        [{"role": "user", "content": f"Circuit description:\n{description}"}]
    )
    return strip_fences(raw)


def fix_netlist(netlist_text: str, error_output: str) -> str:
    """Ask the model to correct a netlist given the ngspice error output."""
    prompt = (
        "The following ngspice netlist failed to simulate correctly.\n\n"
        "=== NETLIST ===\n"
        f"{netlist_text}\n"
        "=== NGSPICE OUTPUT (stdout+stderr) ===\n"
        f"{error_output}\n"
        "=== END ===\n\n"
        "Return a corrected ngspice netlist that fixes the problem. "
        "Output ONLY the netlist, no explanation and no code fences."
    )
    raw = _message_text([{"role": "user", "content": prompt}])
    return strip_fences(raw)


def fix_netlist_spec(netlist_text: str, spec_target: str, measured: str) -> str:
    """Ask the model to redesign a netlist that simulates cleanly but misses
    its electrical spec, using the actual measured value as feedback.

    NOTE: this is an API call (billed). The benchmark only uses it behind the
    opt-in --spec-repair flag.
    """
    prompt = (
        "The following ngspice netlist simulates cleanly, but FAILS its "
        "electrical specification.\n\n"
        "=== NETLIST ===\n"
        f"{netlist_text}\n"
        "=== SPEC TARGET ===\n"
        f"{spec_target}\n"
        "=== MEASURED VALUE (from actual simulation) ===\n"
        f"{measured}\n"
        "=== END ===\n\n"
        "Adjust the design (component values, bias, duty cycle, model "
        "parameters, topology if necessary) so the circuit MEETS the spec. "
        "Keep the same node/source names required by the harness. "
        "Output ONLY the corrected netlist, no explanation and no code fences."
    )
    raw = _message_text([{"role": "user", "content": prompt}])
    return strip_fences(raw)


def find_ngspice() -> str:
    """Locate the ngspice executable.

    Order: NGSPICE_PATH env var -> ngspice_con/ngspice on PATH -> bundled copy
    under tools/Spice64/bin (downloaded during setup on Windows).
    """
    env = os.environ.get("NGSPICE_PATH")
    if env and Path(env).exists():
        return env

    for name in ("ngspice_con", "ngspice"):
        found = shutil.which(name)
        if found:
            return found

    bundled_dir = Path(__file__).resolve().parent / "tools" / "Spice64" / "bin"
    for name in ("ngspice_con.exe", "ngspice.exe", "ngspice_con", "ngspice"):
        cand = bundled_dir / name
        if cand.exists():
            return str(cand)

    raise FileNotFoundError(
        "Could not find ngspice. Install it and put it on PATH, set NGSPICE_PATH "
        "to the executable, or place it under tools/Spice64/bin."
    )


# ngspice frequently exits 0 even when the deck has problems, so also scan the
# combined output for the error signatures it prints in batch mode.
_ERROR_RE = re.compile(
    r"(?im)^\s*(error|fatal)\b"
    r"|aborted"
    r"|simulation interrupted"
    r"|there aren't any circuits loaded"
    r"|can't find"
    r"|cannot find"
    r"|unknown (?:subckt|parameter|device)"
)


def run_ngspice(netlist_text: str) -> tuple[bool, str]:
    """Write the netlist to a temp .cir file and run `ngspice -b` on it.

    Returns (success, combined_stdout_and_stderr).
    """
    exe = find_ngspice()
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".cir", delete=False, encoding="ascii", errors="replace"
    )
    try:
        tmp.write(netlist_text)
        tmp.close()
        proc = subprocess.run(
            [exe, "-b", tmp.name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        success = proc.returncode == 0 and not _ERROR_RE.search(combined)
        return success, combined
    except subprocess.TimeoutExpired:
        return False, "ngspice timed out after 60 seconds (possible non-converging simulation)."
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        prog = Path(sys.argv[0]).name
        print(f'Usage: python {prog} "<circuit description>"', file=sys.stderr)
        return 2

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY is not set. Export it, or put it in a "
            ".env file next to main.py as ANTHROPIC_API_KEY=sk-ant-...",
            file=sys.stderr,
        )
        return 2

    description = sys.argv[1]

    netlist = generate_netlist(description)
    success = False
    output = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"--- Attempt {attempt}/{MAX_ATTEMPTS}: running ngspice ---", file=sys.stderr)
        success, output = run_ngspice(netlist)
        if success:
            print(f"--- Simulation succeeded on attempt {attempt} ---", file=sys.stderr)
            break
        if attempt < MAX_ATTEMPTS:
            print("--- Simulation failed; asking the model to fix it ---", file=sys.stderr)
            netlist = fix_netlist(netlist, output)
        else:
            print("--- Out of attempts; giving up ---", file=sys.stderr)

    print("\n============================ FINAL NETLIST ============================")
    print(netlist.rstrip("\n"))
    print("\n========================= FULL NGSPICE OUTPUT ========================")
    print(output.rstrip("\n"))
    print("\n======================================================================")
    print(f"Result: {'SUCCESS' if success else 'FAILURE'} after up to {MAX_ATTEMPTS} attempts.")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
