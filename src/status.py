"""Pipeline status line — shows current phase and progress.

Auto-detects terminal vs piped output:
- Terminal: colored ANSI status lines with progress bars
- Piped/captured (e.g., Claude Code): plain text phase summaries
"""

from __future__ import annotations

import sys

_IS_TTY = sys.stderr.isatty()

# Colors (only used in TTY mode)
CYAN = "\033[0;36m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
MAGENTA = "\033[0;35m"
RED = "\033[0;31m"
BLUE = "\033[0;34m"
BOLD = "\033[1m"
DIM = "\033[2m"
NC = "\033[0m"

PHASES = ["DISCOVER", "FILTER", "MAP", "ANALYZE", "ACT", "REMEMBER"]

PHASE_COLORS = {
    "DISCOVER": CYAN,
    "FILTER": MAGENTA,
    "MAP": BLUE,
    "ANALYZE": YELLOW,
    "ACT": RED,
    "REMEMBER": GREEN,
}


def _bar(done: int, total: int, width: int = 12) -> str:
    if total == 0:
        return f"{GREEN}{'█' * width}{NC}"
    filled = int(width * done / total)
    return f"{GREEN}{'█' * filled}{DIM}{'░' * (width - filled)}{NC}"


def _dots(phase: str) -> str:
    phase_idx = PHASES.index(phase) if phase in PHASES else 0
    dots = ""
    for i in range(len(PHASES)):
        if i < phase_idx:
            dots += f"{GREEN}●{NC}"
        elif i == phase_idx:
            dots += f"{YELLOW}●{NC}"
        else:
            dots += f"{DIM}○{NC}"
    return dots


def status(agent: str, phase: str, message: str, done: int = 0, total: int = 0) -> None:
    """Print a status line. In-place update in TTY, silent in piped mode."""
    if not _IS_TTY:
        return

    dots = _dots(phase)
    color = PHASE_COLORS.get(phase, NC)

    if total > 0:
        bar = _bar(done, total)
        progress = f"{bar} {DIM}{done}/{total}{NC}"
    else:
        bar = _bar(1, 1)
        progress = bar

    line = f"\r{DIM}[{NC}{BOLD}{agent}{NC}{DIM}]{NC} {dots} {color}{phase:8s}{NC} {progress} {message}"
    sys.stderr.write(f"\033[2K{line}")
    sys.stderr.flush()


def status_done(agent: str, phase: str, message: str) -> None:
    """Print a completed status line. Colorful in TTY, plain in piped mode."""
    if _IS_TTY:
        dots = _dots(phase)
        color = PHASE_COLORS.get(phase, NC)
        bar = _bar(1, 1)
        line = f"{DIM}[{NC}{BOLD}{agent}{NC}{DIM}]{NC} {dots} {color}{phase:8s}{NC} {bar} {GREEN}{message}{NC}"
        sys.stderr.write(f"\033[2K\r{line}\n")
        sys.stderr.flush()
    else:
        sys.stderr.write(f"[{agent}] {phase:8s} — {message}\n")
        sys.stderr.flush()
