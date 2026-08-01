# Data Dictionary

All data is **synthetic**, produced by `python -m src.datagen --seed <n>`. No real
person, employer, or financial record exists anywhere in this project.

## Source A — `data/sources/hr_system.xlsx` (HR system export)

Sheet **Employees**:

| Column | Type | Notes |
|---|---|---|
| employee_id | str | `E#####`, unique per person (duplicate *rows* exist by design) |
| full_name | str | synthetic; ~5% have trailing whitespace (injected) |
| department | str | messy: aliases like `OPS`, `Operations `, `People & Culture` (injected) |
| role_level | str | L1–L4, M1–M2 |
| hire_date | str | ISO |
| termination_date | str | ISO or empty = still employed |
| fte_percent | num | **mixed scales**: ~30% of rows are 0–100, rest 0–1 (injected) |
| location | str | Calgary / Toronto / Remote |

Sheet **Summary**: live `COUNTIFS` / `SUMIFS` headcount aggregation and an
`XLOOKUP` employee lookup. (openpyxl cannot author a PivotTable from scratch —
it only preserves existing ones — so the same aggregation is expressed as
formulas; documented limitation, not an oversight.)

Injected defects: ~2% exact duplicate rows; department aliases; FTE scale mix;
trailing whitespace.

## Source B — `data/sources/financials.csv` (finance system export)

| Column | Type | Notes |
|---|---|---|
| cost_centre_code | str | CC-100 … CC-500; maps to department via the lookup below |
| period | str | `YYYY-MM`; **CC-300 is missing 2024-02 and 2025-06** (injected) |
| budget_amount | num | set at fiscal-year start; deliberately ignorant of the month-24 reorg |
| actual_amount | mixed | ~4% are currency strings like `$12,345.67` (injected) |
| account_category | str | Salaries, Contractor Costs, Software, Travel |

## Lookup — `data/lookup/cost_centre_map.csv`

`cost_centre_code → department`. **CC-500 (Sales) is absent by design** — the
reconciliation engine must quarantine unmapped rows and surface the gap, never
guess or drop.

## Source D — `data/sources/headcount_plan.csv` (workforce planning template)

The analyst-maintained plan — the one file a human curates, so it ships clean
(canonical departments, ISO months, no injected mess) but is still validated
like any source (a hand-typed department typo is quarantined, not trusted).

| Column | Type | Notes |
|---|---|---|
| department | str | canonical |
| month | str | ISO first-of-month |
| planned_fte | num | planned employee FTE for that department-month; ignorant of the month-24 reorg, so post-break plan-vs-actual gaps are real |

## Source C — `contractor_roster` table (SQLite default, Postgres via `HELIOS_DB_URL`)

| Column | Type | Notes |
|---|---|---|
| vendor_id | str | `V####` |
| contractor_name | str | ~5% deliberately share a name with an employee in the same department (double-count trap) |
| department_code | str | CC code, joined via the same incomplete lookup |
| start_date / end_date | str | **three formats mixed**: `2024-03-05`, `05/03/2024` (day-first), `Mar 05, 2024` (injected) |
| daily_rate | num | department-banded; +15% after the month-24 reorg |
| status | str | active / ended |

## Ground truth — `data/truth/` (evaluation only)

`monthly_truth.csv` (department × month headcount + true costs) and
`budget_truth.csv`. **Never read by reconciliation or forecasting code** — they
exist so model accuracy can be reported against the known generating process.

Embedded signal: ~2–5%/yr growth per department; Q1/September hiring
seasonality; Q4 travel spike; gaussian noise; and a **structural break at month
24** (Operations ×0.82, Technology ×1.28, contractor rates +15%) that the Phase
6 drift detector must catch.

## Reconciled outputs — `data/reconciled/`

- `employees.csv`, `contractors.csv`, `financials.csv` — the trusted view; canonical departments, FTE as 0–1 ratio, real dates, numeric amounts
- `quarantine.csv` — every excluded record: `source`, `reason`, full JSON payload
- `scorecard.json` — per-run data quality scorecard (in/kept/quarantined per source, reason counts, period gaps)

## Warehouse — `data/warehouse/helios.db` (SQLite) or `HELIOS_WAREHOUSE_URL` (Postgres)

Star schema loaded by `python -m src.warehouse` from `data/reconciled/`.

| Table | Grain / notes |
|---|---|
| `dim_date` | month; `date_key` = YYYYMM |
| `dim_department` | canonical department |
| `dim_cost_centre` | CC code → department (only mapped codes; CC-500 stays quarantined upstream) |
| `dim_role_level` | L1–L4, M1–M2 |
| `dim_employee` | **Type 2 SCD**: any attribute change closes the current row (`valid_to`, `is_current=0`) and opens a new version; unique `(employee_id, valid_from)`; same-day corrections update in place |
| `fact_headcount` | department × month; `employee_fte` + `contractor_fte` + `total_fte`. **Semi-additive**: sums across departments, never across months. Contractors count 1.0 FTE by convention |
| `fact_spend` | cost_centre × month × account_category; `budget_amount`, `actual_amount` |

Views: `vw_budget_vs_actual` (variance amount + %), `vw_headcount_trend`,
`vw_contractor_mix`, and `vw_headcount_vs_plan` (vacancy). Vacancy =
`planned_fte - employee_fte` (planned permanent positions not filled by
employees; contractors excluded — they backfill work but do not fill a budgeted
seat). This became honestly derivable once the workforce-plan source (Source D)
was added; before that it was deliberately omitted rather than fabricated from
budget dollars.

## Variance analysis — `python -m src.variance`

Budget-vs-actual at line-item grain with:
- **Direction**: favourable / unfavourable / on_budget
- **Materiality**: flagged only when |variance%| ≥ 5% AND |variance$| ≥ $50k
  (both, so trivial-dollar percentage swings don't crowd out large ones); both
  thresholds configurable
- **Driver attribution** (Salaries): volume (headcount vs plan) + mix
  (department composition) + rate (cost per head), which **reconcile exactly**
  to total variance by construction
- **Anomaly detection**: robust MAD-based z-score vs each cell's own history
- Plain-language period summaries. Outputs: `data/reports/variance_report.{csv,json}`

## Drift monitoring & retraining — `python -m src.monitoring`

Four distinct signals: **data drift** and **prediction drift** (PSI + KS,
hand-rolled, verified against scipy; abstain below 24 samples), **concept
drift** (residual distribution/bias shift), **performance decay** (error-ratio
vs baseline). Retraining is threshold-triggered by output-quality monitors
only; a retrained challenger is promoted only if it beats the incumbent by a
required margin on a fresh holdout. Output: `data/reports/drift_report.json`.
On 36-point monthly series the residual-based monitors are primary and the
distributional ones abstain — the correct behaviour for the data size, not a gap.

Loads are idempotent: dims upsert on natural keys, SCD2 merge is a no-op on
unchanged data, facts delete-then-insert per month in one transaction.

## Forecasting — `python -m src.forecasting`

Targets: `headcount::<dept>` / `spend::<dept>` plus `::TOTAL`, built only from
warehouse views (never raw sources, never ground truth). Spend excludes Sales
— CC-500 is quarantined upstream by the lookup gap. Interior period gaps
(CC-300) are linearly interpolated for modeling; the gap itself stays on the
QC scorecard.

Models: `naive`, `seasonal_naive` (baselines), `sarima`, `prophet`, `xgboost`.
Evaluation: rolling-origin expanding-window CV (min_train=18, horizon=3,
step=3 → 6 folds on 36 months); pooled MAPE/RMSE/MAE + empirical 80% interval
coverage. MLflow tracking in `data/mlflow/` (sqlite backend); champion per
target registered as a refit-on-predict pyfunc, promoted only if it beats
both baselines. Full results: `docs/modeling_decisions.md` and
`data/reports/backtest_report.json`.

### Quarantine reason codes

| Code | Meaning |
|---|---|
| DUPLICATE_ROW | exact duplicate export row (first kept) |
| UNKNOWN_DEPARTMENT | department not confidently mappable to canonical |
| INVALID_FTE | FTE outside [0, 1] after scale normalization (not clamped — clamping hides defects) |
| INVALID_DATE | unparseable required date |
| UNMAPPED_COST_CENTRE | code absent from lookup (the CC-500 gap) |
| UNPARSEABLE_AMOUNT | neither budget nor actual parseable |
| CONTRACTOR_ALSO_EMPLOYEE | same normalized name + department + overlapping period as an employee → counted once as employee |

Invariant asserted every run: **rows_in == rows_kept + rows_quarantined** per source.
