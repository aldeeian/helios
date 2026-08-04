"""Entry point: `python -m src.warehouse` loads data/reconciled/ into the
star schema (SQLite by default, HELIOS_WAREHOUSE_URL for Postgres)."""

from __future__ import annotations

import json

from . import load


def main() -> None:
    report = load.load()
    print("Warehouse load complete")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
