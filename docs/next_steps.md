# Your next steps

Three pieces are left, and each needs something only you have: your data, your
Render account, your copy of Power BI Desktop. They are independent — do them in
any order.

| | What | Time | Needs |
|---|---|---|---|
| **A** | Real data instead of synthetic | 1–3 days | A dataset |
| **B** | Deploy so it has a public URL | 45–90 min | Render + GitHub account |
| **C** | Assemble the actual `.pbix` | ~30 min | Power BI Desktop (Windows) |

Every step below ends with a **check** — run it before moving on. If a check
fails, stop there; the next step will not fix it.

Throughout, `py` means the venv's Python. On Windows:

```
cd C:\Users\saifa\Projects\helios
.venv\Scripts\python.exe -m pytest        # this is what "py -m pytest" means below
```

---

# Option B — Deploy it (do this first)

Start here. It is the shortest, it gives you a link to put on a résumé, and it
does not touch any code.

## B1. Put the repo on GitHub

You have a private repo at `github.com/aldeeian/helios` with nothing pushed yet.

```
git status                 # see what is uncommitted
git add -A
git commit -m "Add explorer UI, alerting, and Power BI model export"
git push -u origin master
```

**Check:** open `github.com/aldeeian/helios` and confirm you see `render.yaml`,
`src/alerting/`, and `src/api/static/`.

> If `git push` asks for a password, use a personal access token, not your
> GitHub password — GitHub stopped accepting passwords in 2021.

## B2. Confirm no secrets went up

```
git log --all --full-history -- .env
```

**Check:** this prints **nothing**. If it prints commits, stop and tell me —
a key is in your git history and needs removing before the repo goes anywhere.

## B3. Create the Render blueprint

1. Go to <https://render.com> and sign in with GitHub.
2. **New → Blueprint**.
3. Pick the `helios` repo. Render finds `render.yaml` automatically.
4. It will show two resources: `helios-api` (web service) and
   `helios-warehouse` (Postgres). Click **Apply**.

Render now builds the Docker image on its own servers — you do not need Docker
installed locally.

**Check:** the build log ends with something like `==> Build successful`. First
build takes 5–10 minutes.

## B4. Expect the health check to fail — this is correct

Your service will go live and immediately report **unhealthy**. That is the
system working as designed: `/health` returns 503 when the warehouse is empty,
so a blank deploy shows as down instead of serving an empty dashboard.

You have to load data into it.

## B5. Load the warehouse into the cloud database

1. In Render, open the **helios-warehouse** database page.
2. Copy the **External Database URL**. It looks like
   `postgres://user:pass@host.oregon-postgres.render.com/helios`.
3. Back in your terminal, point Helios at it **for one command only**:

```
$env:HELIOS_WAREHOUSE_URL = "postgresql+psycopg://...paste the URL..."
.venv\Scripts\python.exe -m src.warehouse
```

Two things to fix in the URL you paste:
- Change `postgres://` to `postgresql+psycopg://` — SQLAlchemy needs the driver named.
- Nothing else.

If it complains about a missing driver:

```
.venv\Scripts\pip install "psycopg[binary]"
```

**Check:**

```
.venv\Scripts\python.exe -c "import sqlalchemy as sa, os; e=sa.create_engine(os.environ['HELIOS_WAREHOUSE_URL']); print(e.connect().execute(sa.text('SELECT COUNT(*) FROM fact_spend')).scalar(), 'spend rows')"
```

This should print `568 spend rows`. Then close that terminal so the env var
does not leak into later work.

## B6. Confirm it is live

Open `https://helios-api-XXXX.onrender.com` (Render shows your exact URL).

**Check, in order:**
- `/health` returns `{"status":"ok", ...}` — not 503
- `/` shows the explorer with real numbers
- The month and department dropdowns are populated
- Clicking an example question under "Ask a question" returns an answer

If `/health` is still 503, B5 did not actually write to the cloud database —
most likely `HELIOS_WAREHOUSE_URL` was not set in the shell you ran it from.

## B7. Know the free-tier catch

Render's free plan **sleeps the service after 15 minutes idle**, and the next
visitor waits ~50 seconds for it to wake. If you are sending this link to a
recruiter, that first impression is a blank loading screen.

Two ways to handle it:
- **Free:** put a monitor on `https://your-app.onrender.com/health` at
  <https://uptimerobot.com> every 5 minutes. This keeps it awake and emails you
  if it breaks.
- **$7/month:** upgrade the service to Starter. No sleeping.

Also note Render's free Postgres **expires after 90 days**. Set a calendar
reminder now — if it lapses without you noticing, the live link dies silently.

## B8. Optional — turn on real alert emails

1. Sign up at <https://resend.com> (free tier: 3,000 emails/month).
2. Create an API key.
3. In Render → helios-api → **Environment**, set:
   - `RESEND_API_KEY` = your key
   - `HELIOS_ALERT_FROM` = `onboarding@resend.dev`
   - `HELIOS_ALERT_TO` = `saifaldeeian@gmail.com`
4. Verify the wiring without waiting for a real breach:

```
.venv\Scripts\python.exe -m src.alerting --test
```

**Check:** the email arrives. Note the sandbox limit — until you verify a
domain, Resend will **only** send to the address that owns the account. Sending
to anyone else returns 403.

---

# Option C — Build the real `.pbix` (~30 minutes)

The HTML dashboard is a faithful preview. This produces the actual Power BI
file, which is what a BI job posting means when it says "Power BI".

## C1. Install Power BI Desktop

Free, Windows-only: <https://powerbi.microsoft.com/desktop/> — or install
"Power BI Desktop" from the Microsoft Store (the Store version auto-updates).

## C2. Export the data

```
.venv\Scripts\python.exe -m src.warehouse
.venv\Scripts\python.exe -m powerbi.export_model
```

**Check:** `powerbi/model/` contains 10 CSV files, and `dim_date.csv` opens with
`date_key` values like `202301` — **not** `1970-01-01`. (That was a real bug;
if you somehow see 1970 dates, the fix did not apply.)

## C3. Load it

1. Open Power BI Desktop → **Get Data → More → Folder → Connect**.
2. Browse to `C:\Users\saifa\Projects\helios\powerbi\model` → **OK**.
3. In the preview window click **Combine → Combine & Load**.

**Check:** the Data pane on the right lists all 10 tables.

## C4. Build the relationships

Go to **Model view** (the third icon down the left edge). Drag to connect:

| From | To |
|---|---|
| `fact_headcount[date_key]` | `dim_date[date_key]` |
| `fact_headcount[department_key]` | `dim_department[department_key]` |
| `fact_spend[date_key]` | `dim_date[date_key]` |
| `fact_spend[cost_centre_key]` | `dim_cost_centre[cost_centre_key]` |
| `dim_cost_centre[department_key]` | `dim_department[department_key]` |
| `dim_employee[department_key]` | `dim_department[department_key]` |
| `dim_employee[role_level_key]` | `dim_role_level[role_level_key]` |

For each one, double-click the line and confirm:
- **Cardinality:** Many to one (*:1)
- **Cross filter direction:** Single

> Single direction matters. Both-directions relationships create ambiguous
> filter paths that produce numbers nobody can explain — and being unable to
> explain your own dashboard is the worst outcome in an interview.

Then click `dim_date` → **Table tools → Mark as Date Table** → pick `month_start`.

**Check:** every relationship line has an arrow pointing *toward* the dimension,
and no line is dotted (dotted = inactive).

## C5. Add the measures

Open [`powerbi/dax_measures.md`](../powerbi/dax_measures.md). For each measure:

**Home → New Measure**, then paste the DAX exactly as written.

Start with these four; they carry the story:
- `Total Actual`
- `Total Budget`
- `Variance`
- `Headcount (EOP)` ← the semi-additive one

**Check:** drop `Headcount (EOP)` into a card visual with `dim_date[month_start]`
on a slicer. As you change months the number should *change*, not accumulate. If
it grows month over month, the measure was pasted wrong — headcount is a
point-in-time balance, not a sum.

## C6. Build the pages

Follow [`powerbi/dashboard_spec.md`](../powerbi/dashboard_spec.md) — it names
every visual and which measure goes on it. Four pages: Executive, Variance,
Workforce, Model health.

Then hide the raw columns: right-click each `*_fte` column in the Data pane →
**Hide in report view**. This forces every headcount number through the
semi-additive measures instead of letting someone drag in a raw column that
sums incorrectly.

## C7. Save and commit

Save as `powerbi/helios.pbix`.

```
git add powerbi/helios.pbix
git commit -m "Add Power BI report file"
git push
```

**Check:** the file is under 100 MB (GitHub's hard limit). It should be ~2 MB.
If it is huge, you loaded the data as Import with too much history — not a
problem at this size, but worth knowing.

---

# Option A — Real data (the big one)

This is the one that most changes how a hiring manager reads the project.
Right now every number traces back to a random seed. After this, the pipeline
has survived contact with data it did not generate.

## A1. Understand what you are replacing

Currently: `src/datagen` **invents** data → writes files → `src/ingestion/readers.py`
**reads** those files → reconciliation cleans them.

After: **your real data** → `readers.py` reads it → reconciliation cleans it.

You are replacing the *readers*, not the reconciliation engine. That is the whole
point — the cleaning logic should not care where rows came from.

## A2. Pick a dataset

You need something with **people, departments, and time**. Good hunting grounds:

- **Kaggle** — search "HR analytics", "employee attrition", "IBM HR". The IBM HR
  dataset is the classic one.
- **data.gov / open.canada.ca** — public-sector payroll disclosure. These are
  *real* and genuinely messy, which is better.
- **City of Calgary open data** — you already know this portal from
  transit-monitor, and municipal salary disclosure is usually published.

**Pick criteria, in priority order:**
1. Has a **date** column spanning 24+ months (you need history for forecasting)
2. Has a **department**-like column
3. Has a **headcount or salary** number
4. Is genuinely messy (missing values, inconsistent naming) — messiness is the
   feature here, not a problem

> A dataset with no time dimension cannot be forecast. If you can only find a
> single-snapshot dataset, stop and pick a different one — do not fabricate
> months, that is the same dishonesty as backdating commits.

## A3. Map your columns to what the engine expects

This is the actual work. The reconciliation engine requires these exact column
names. Your job is to rename your dataset's columns to match.

**HR source** (`read_hr`):

| Column | Type | Notes |
|---|---|---|
| `employee_id` | string | must be unique per person |
| `full_name` | string | any format |
| `department` | string | messy is fine — the normalizer handles aliases |
| `fte_percent` | number | `1.0` or `100` both work |
| `hire_date` | date | many formats accepted |
| `termination_date` | date or blank | blank = still employed |
| `role_level` | string | e.g. "Junior", "Senior" |

**Financials source** (`read_financials`):

| Column | Type | Notes |
|---|---|---|
| `cost_centre_code` | string | e.g. `CC-100` |
| `period` | string | `YYYY-MM` |
| `account_category` | string | e.g. "Salaries", "Travel" |
| `budget_amount` | number or string | `"$1,234.00"` is fine |
| `actual_amount` | number or string | same |

**Cost centre lookup** (`read_cost_centre_map`): `cost_centre_code`, `department`

**Contractors** (`read_contractors`): `contractor_id`, `contractor_name`,
`department_code`, `start_date`, `end_date`

**Headcount plan** (`read_headcount_plan`): `department`, `month`, `planned_fte`

> **You do not need all five sources.** HR + financials + the lookup is enough
> for the pipeline to run. Contractors and the plan are optional — if you skip
> the plan, vacancy analysis goes away, and you should say so in the README
> rather than leaving a broken chart.

## A4. Fix the department catalogue — the trap

**This is where you will get stuck if you skip it.**

`src/reconciliation/normalize.py` imports its department list from
`src/datagen/org_model.py`:

```python
from ..datagen.org_model import DEPT_ALIASES
```

That list is hardcoded to the *synthetic* departments — Operations, Technology,
Finance, Human Resources, Sales. If your real data has "Engineering" or
"Marketing", `normalize_department` returns `None` and **every single row gets
quarantined as UNKNOWN_DEPARTMENT**. The pipeline will run, report zero errors,
and produce an empty warehouse.

Edit `DEPT_ALIASES` in `src/datagen/org_model.py` to your real departments, with
each real-world spelling variant listed:

```python
DEPT_ALIASES: dict[str, list[str]] = {
    "Engineering": ["Engineering", "ENG", "Eng.", "Software Engineering"],
    "Marketing":   ["Marketing", "MKT", "Mktg"],
    # ...one entry per department in your data
}
```

To find every variant actually present in your data:

```python
import pandas as pd
df = pd.read_csv("your_data.csv")
print(sorted(df["department"].dropna().unique()))
```

**Check:** after editing, this returns your canonical name, not `None`:

```
.venv\Scripts\python.exe -c "from src.reconciliation.normalize import normalize_department as n; print(n('ENG'))"
```

## A5. Write the readers

Create `src/ingestion/real_readers.py`. Do **not** clean the data here — return
it as raw as possible. Cleaning belongs to reconciliation, so problems stay
visible and countable.

```python
"""Readers for real source data. Read-only, and deliberately dumb: rename
columns to the engine's contract and return. Every quality decision belongs to
src/reconciliation, where it gets counted and reported."""

from __future__ import annotations
from pathlib import Path
import pandas as pd

RAW = Path(__file__).resolve().parent.parent.parent / "data" / "real"

def read_hr() -> pd.DataFrame:
    df = pd.read_csv(RAW / "employees.csv", dtype=object)
    return df.rename(columns={
        "EmployeeNumber": "employee_id",     # <- your dataset's names on the left
        "Name":           "full_name",
        "Department":     "department",
        "FTE":            "fte_percent",
        "HireDate":       "hire_date",
        "TermDate":       "termination_date",
        "JobLevel":       "role_level",
    })

def read_financials() -> pd.DataFrame:
    df = pd.read_csv(RAW / "finance.csv", dtype=object)
    return df.rename(columns={ ... })
```

Then point the pipeline at them in `flows/steps.py` — change
`from src.ingestion import readers` to your module, or swap the functions inside
`readers.py` directly.

## A6. Run it and read the scorecard

```
.venv\Scripts\python.exe -m src.reconciliation
```

**This is the interesting moment.** You will get a scorecard like:

```
hr         :  5000 in ->  4200 kept,  800 quarantined
reasons: UNKNOWN_DEPARTMENT: 600, INVALID_DATE: 200
```

**Do not panic at a high quarantine rate, and do not "fix" it by loosening
rules.** Open `data/reconciled/quarantine.csv` and read actual rejected rows.
Then decide, per reason code:

- `UNKNOWN_DEPARTMENT` → a real department missing from `DEPT_ALIASES` (fix A4),
  or genuinely junk data (correctly rejected)
- `INVALID_DATE` → a date format the parser does not know (add it), or a real
  blank (correctly rejected)
- `UNMAPPED_COST_CENTRE` → your lookup table is incomplete — that is a genuine
  finding, exactly like CC-500 in the synthetic data

**Check:** `rows_in == rows_kept + rows_quarantined` for every source. The engine
asserts this, so a mismatch crashes rather than lying.

> The QC gate halts the pipeline if any source exceeds **50%** quarantined. If
> you hit that, something is systematically wrong with your column mapping —
> do not raise the threshold to get past it.

## A7. Run the rest of the pipeline

```
.venv\Scripts\python.exe -m src.warehouse
.venv\Scripts\python.exe -m src.forecasting --no-prophet
.venv\Scripts\python.exe -m src.variance
.venv\Scripts\python.exe -m src.monitoring
```

Expect things to break here, and expect the breaks to be informative:

- **Forecasting needs ≥24 months.** Fewer and the backtest cannot form folds.
- **Variance needs budget *and* actual.** If your data has no budget column,
  variance analysis does not apply — say so rather than inventing a budget.
- **Drift detection needs a regime change** to detect. Real data may simply not
  have one, and reporting "no drift detected, correctly" is an honest result.

## A8. Fix the tests

Tests currently assert against synthetic values (e.g. exactly 5 departments,
Sales missing spend). With real data these will fail — correctly.

```
.venv\Scripts\python.exe -m pytest
```

For each failure, decide honestly: is the test asserting something about the
*synthetic scenario* (update it) or about the *engine's logic* (the engine
broke — fix the engine, not the test).

**Never** delete a test to make the suite green. A green suite that proves
nothing is worse than a red one that tells the truth.

## A9. Update the README honestly

Replace every "all data is synthetic" claim with what is now true:

```markdown
## Data

Built on [dataset name](link), covering [N] employees across [N] departments
from [start] to [end]. Licensed under [license].

The reconciliation engine quarantined [N] of [N] rows on first run
([breakdown]). [What you did about it.]

[If you kept synthetic data for any part, say exactly which part and why.]
```

**Check:** search the repo for "synthetic" and confirm every remaining mention
is accurate. Leaving a stale claim is worse than never having made it.

---

## What to tell recruiters, at each stage

Only claim what is actually true right now.

**After B (deployed):**
> Deployed at [URL]. Data is synthetic, generated from a seeded simulation with
> a deliberate structural break used to demonstrate drift detection.

**After B + C:**
> ...plus a Power BI report with semi-additive DAX measures over a Type 2 SCD
> star schema.

**After A + B + C:**
> Ingests [real dataset], reconciles it with an explicit quarantine (X% of rows
> flagged with reason codes on first run), forecasts with rolling-origin
> backtesting, and detects its own degradation.

Do **not** say "deployed to Azure" — the Bicep is real and correct, but it has
never been provisioned. "Deployable" is the honest word until you stand it up.

---

## If you get stuck

Tell me which step number, paste the exact error, and I will work it with you.
Steps B5, A4, and A6 are where things usually go wrong.
