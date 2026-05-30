"""
CI isolation gate — enforces the hard separation between the paper-trading
engine (`labs/`) and the real-money live stack (`live/`).

Run locally or in CI:  python -m pytest tests/test_live_isolation.py -q
(also runnable bare:    python tests/test_live_isolation.py)

It FAILS the build if ANY of these leak:

  1. The literal `place_order` appears anywhere under `labs/`.
     The paper engine must NEVER reference an order-placement symbol.

  2. A *direct broker order SDK* call/import appears in the web/service/runner
     files (`labs/ui/live_routes.py`, `live/live_service.py`,
     `live/live_runner.py`). Direct SDK access (`import kiteconnect`,
     `from SmartApi ...`, `.placeOrder(`, `kite.place_order(`) is allowed
     ONLY under `live/brokers/*`. These files must route everything through
     the broker abstraction / chokepoint instead.

  3. `import live` (or `from live`) appears in
     `labs/engine/paper_executor.py`. The paper executor must stay blind to
     the live stack.

These are textual/static checks on purpose: they hold even if a file fails to
import, and they mirror the §11 grep gate described in the spec.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories whose own source files are scanned in rule 1.
LABS_DIR = REPO_ROOT / "labs"

# Files that must not contain a direct broker SDK order call (rule 2).
NO_DIRECT_SDK_FILES = [
    REPO_ROOT / "labs" / "ui" / "live_routes.py",
    REPO_ROOT / "live" / "live_service.py",
    REPO_ROOT / "live" / "live_runner.py",
]

# Only these paths are permitted to touch a broker order SDK directly.
BROKERS_DIR = REPO_ROOT / "live" / "brokers"

# Patterns that indicate a *direct* broker order SDK touch.
DIRECT_SDK_PATTERNS = [
    re.compile(r"\bimport\s+kiteconnect\b"),
    re.compile(r"\bfrom\s+kiteconnect\b"),
    re.compile(r"\bimport\s+SmartApi\b"),
    re.compile(r"\bfrom\s+SmartApi\b"),
    re.compile(r"\.placeOrder\s*\("),        # Angel SDK
    re.compile(r"\bkite\.place_order\s*\("),  # Zerodha SDK
]

# Rule-3 patterns.
IMPORT_LIVE_PATTERNS = [
    re.compile(r"^\s*import\s+live\b", re.MULTILINE),
    re.compile(r"^\s*from\s+live\b", re.MULTILINE),
]


def _py_files(root: Path):
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _strip_comments_and_strings(src: str) -> str:
    """Best-effort removal of line comments and string/docstring bodies so a
    pattern in prose (e.g. this test's own docstring style) does not trip the
    gate. Conservative: only strips obvious comments and triple-quoted blocks."""
    # Drop triple-quoted blocks (docstrings).
    src = re.sub(r'"""[\s\S]*?"""', '""', src)
    src = re.sub(r"'''[\s\S]*?'''", "''", src)
    # Drop full-line and trailing comments.
    src = re.sub(r"#.*", "", src)
    return src


# --------------------------------------------------------------------------- #
# Rule 1: no `place_order` anywhere under labs/
# --------------------------------------------------------------------------- #
def test_no_place_order_under_labs():
    offenders = []
    for f in _py_files(LABS_DIR):
        if "place_order" in _read(f):
            # report line numbers for actionable failure
            for i, line in enumerate(_read(f).splitlines(), 1):
                if "place_order" in line:
                    offenders.append(f"{f.relative_to(REPO_ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "`place_order` must never appear under labs/ (paper engine places no "
        "real orders):\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# Rule 2: no direct broker SDK order call in web/service/runner files
# --------------------------------------------------------------------------- #
def test_no_direct_broker_sdk_outside_brokers():
    offenders = []
    for f in NO_DIRECT_SDK_FILES:
        assert f.exists(), f"expected file missing: {f.relative_to(REPO_ROOT)}"
        # Defensive: these files must not live under live/brokers/.
        assert BROKERS_DIR not in f.parents, f"{f} unexpectedly under brokers/"
        code = _strip_comments_and_strings(_read(f))
        for pat in DIRECT_SDK_PATTERNS:
            for m in pat.finditer(code):
                offenders.append(
                    f"{f.relative_to(REPO_ROOT)}: matched {pat.pattern!r}"
                )
    assert not offenders, (
        "Direct broker order SDK access is allowed ONLY under live/brokers/*. "
        "Route through the broker abstraction instead:\n  "
        + "\n  ".join(offenders)
    )


def test_brokers_dir_is_the_only_sdk_holder():
    """Sanity: confirm the SDK truly is isolated — no direct SDK import/order
    call anywhere under live/ EXCEPT live/brokers/*."""
    offenders = []
    live_dir = REPO_ROOT / "live"
    for f in _py_files(live_dir):
        if BROKERS_DIR in f.parents or f.parent == BROKERS_DIR:
            continue
        code = _strip_comments_and_strings(_read(f))
        for pat in DIRECT_SDK_PATTERNS:
            if pat.search(code):
                offenders.append(
                    f"{f.relative_to(REPO_ROOT)}: matched {pat.pattern!r}"
                )
    assert not offenders, (
        "Broker SDK leaked outside live/brokers/*:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# Rule 3: paper_executor must not import the live stack
# --------------------------------------------------------------------------- #
def test_paper_executor_does_not_import_live():
    f = REPO_ROOT / "labs" / "engine" / "paper_executor.py"
    assert f.exists(), f"expected file missing: {f.relative_to(REPO_ROOT)}"
    code = _strip_comments_and_strings(_read(f))
    offenders = [pat.pattern for pat in IMPORT_LIVE_PATTERNS if pat.search(code)]
    assert not offenders, (
        "labs/engine/paper_executor.py must not import the live stack "
        f"(matched: {offenders})"
    )


if __name__ == "__main__":
    # Allow bare execution without pytest installed.
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures.append((name, str(e)))
                print(f"FAIL  {name}\n      {e}")
    if failures:
        raise SystemExit(1)
    print("\nAll isolation gates passed.")
