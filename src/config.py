"""Central configuration: paths and environment.

Secrets are read from the environment only (populated from a gitignored .env).
Nothing in this module — or anywhere in the repo — hardcodes a credential.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = REPO_ROOT / "data"
SOURCES_DIR = DATA_DIR / "sources"          # the three simulated "source systems"
LOOKUP_DIR = DATA_DIR / "lookup"            # cost centre -> department mapping (deliberately incomplete)
TRUTH_DIR = DATA_DIR / "truth"              # ground truth kept OUT of the source dirs
RECONCILED_DIR = DATA_DIR / "reconciled"    # phase 2 outputs

HR_XLSX = SOURCES_DIR / "hr_system.xlsx"
FINANCIALS_CSV = SOURCES_DIR / "financials.csv"
HEADCOUNT_PLAN_CSV = SOURCES_DIR / "headcount_plan.csv"
COST_CENTRE_MAP_CSV = LOOKUP_DIR / "cost_centre_map.csv"


def _load_dotenv() -> None:
    """Minimal .env loader (no extra dependency). Never overrides real env vars."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def warehouse_db_url() -> str:
    """Connection URL for the star-schema warehouse.

    SQLite at data/warehouse/helios.db by default; set HELIOS_WAREHOUSE_URL
    for Postgres. Kept separate from the contractor *source* database — the
    warehouse is a downstream system and must never share a store with a source.
    """
    url = os.environ.get("HELIOS_WAREHOUSE_URL", "").strip()
    if url:
        return url
    wh_dir = DATA_DIR / "warehouse"
    wh_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(wh_dir / 'helios.db').as_posix()}"


def contractor_db_url() -> str:
    """Connection URL for the contractor 'source system'.

    Defaults to a local SQLite file so the project runs with zero setup;
    set HELIOS_DB_URL for Postgres. SQLAlchemy abstracts the difference —
    the ingestion code path is identical either way.
    """
    url = os.environ.get("HELIOS_DB_URL", "").strip()
    if url:
        return url
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(SOURCES_DIR / 'contractor_roster.db').as_posix()}"
