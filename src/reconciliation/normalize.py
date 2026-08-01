"""Normalization primitives: department names, FTE scales, dates, currency.

Each function is pure and unit-tested. Unknown/unparseable values return None
rather than raising or guessing — the reconciliation engine decides what to do
with them (quarantine, never silently drop).
"""

from __future__ import annotations

import difflib
import re
from datetime import date

from dateutil import parser as dateparser

from ..datagen.org_model import DEPT_ALIASES

# Exact alias -> canonical, built from the documented alias catalogue.
_ALIAS_MAP: dict[str, str] = {
    alias.strip().lower(): canonical
    for canonical, aliases in DEPT_ALIASES.items()
    for alias in aliases
}
_CANONICAL = list(DEPT_ALIASES)


def normalize_department(raw: str | None) -> str | None:
    """Map a messy department string to its canonical name.

    Exact (case/whitespace-insensitive) alias match first; then a conservative
    fuzzy match (difflib ratio >= 0.85) as backstop. Returns None when not
    confidently matched — caller quarantines it.
    """
    if raw is None or not str(raw).strip():
        return None
    key = str(raw).strip().lower()
    if key in _ALIAS_MAP:
        return _ALIAS_MAP[key]
    best = difflib.get_close_matches(key, [c.lower() for c in _CANONICAL], n=1, cutoff=0.85)
    if best:
        return next(c for c in _CANONICAL if c.lower() == best[0])
    return None


def normalize_fte(raw: float | int | str | None) -> float | None:
    """Normalize mixed-scale FTE (0.5 vs 50) to the 0–1 ratio.

    Values in (1.5, 100] are treated as percentages. Anything outside [0, 1]
    after normalization is invalid -> None (quarantine), not clamped: clamping
    would hide a source-system defect.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if 1.5 < v <= 100.0:
        v = v / 100.0
    if 0.0 < v <= 1.0:
        return round(v, 2)
    return None


def parse_date_multi(raw: str | date | None) -> date | None:
    """Parse ISO (2024-03-05), day-first (05/03/2024) and long (Mar 05, 2024) forms.

    Ambiguous all-numeric forms are resolved day-first, matching the generator's
    documented DD/MM/YYYY convention for that style.
    """
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s)
    try:
        return dateparser.parse(s, dayfirst=bool(re.match(r"\d{1,2}/", s))).date()
    except (ValueError, OverflowError):
        return None


_CURRENCY_RE = re.compile(r"[$,\s]")


def parse_amount(raw: float | int | str | None) -> float | None:
    """'$12,345.67' -> 12345.67; numeric passthrough; unparseable -> None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = _CURRENCY_RE.sub("", str(raw))
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def normalize_person_name(raw: str | None) -> str | None:
    """Casefold + collapse whitespace, for cross-system person matching."""
    if raw is None or not str(raw).strip():
        return None
    return re.sub(r"\s+", " ", str(raw)).strip().casefold()
