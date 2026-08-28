# DAX Measures Library

Every measure in the Helios Power BI model, with the reasoning that makes each
defensible in an interview. The model connects to the warehouse star schema
(`vw_budget_vs_actual`, `fact_headcount`, `fact_spend`, `dim_date`, …); mark
`dim_date` as the model's date table.

> The measure that matters most is **Headcount FTE** — headcount is
> *semi-additive* and summing it across months is the single most common
> Power BI modelling mistake. See it first.

## Semi-additive headcount (the trap)

`fact_headcount` stores a point-in-time FTE per department per month. It sums
correctly **across departments** but is nonsense **across months** — adding
January's 265 FTE to February's 267 FTE does not make 532 people. The fix is to
take the value at the *last* date in the current filter context.

```dax
Headcount FTE =
CALCULATE (
    SUM ( fact_headcount[total_fte] ),
    LASTDATE ( dim_date[month_start] )
)
```

Equivalent, more explicit about intent:

```dax
Headcount FTE (Closing) =
CLOSINGBALANCEMONTH ( SUM ( fact_headcount[total_fte] ), dim_date[month_start] )
```

At month grain both return that month's headcount; at quarter or year grain they
return the closing month's headcount — never a meaningless sum. Employee- and
contractor-only variants:

```dax
Employee FTE   = CALCULATE ( SUM ( fact_headcount[employee_fte] ),   LASTDATE ( dim_date[month_start] ) )
Contractor FTE = CALCULATE ( SUM ( fact_headcount[contractor_fte] ), LASTDATE ( dim_date[month_start] ) )
Planned FTE    = CALCULATE ( SUM ( fact_headcount[planned_fte] ),    LASTDATE ( dim_date[month_start] ) )
```

## Spend base measures (additive)

Spend *is* additive across time, so plain sums are correct.

```dax
Total Actual = SUM ( fact_spend[actual_amount] )
Total Budget = SUM ( fact_spend[budget_amount] )
```

## Variance

```dax
Variance $ = [Total Actual] - [Total Budget]

Variance % =
DIVIDE ( [Variance $], [Total Budget] )        -- DIVIDE handles /0 -> blank

-- Sign made explicit for conditional formatting: overspend is unfavourable.
Variance Direction =
SWITCH (
    TRUE (),
    [Variance $] > 0, "Unfavourable",
    [Variance $] < 0, "Favourable",
    "On budget"
)

-- Materiality flag mirrors the Python engine: both a % and a $ floor.
Is Material =
IF (
    ABS ( [Variance %] ) >= 0.05 && ABS ( [Variance $] ) >= 50000,
    1, 0
)
```

## Time intelligence

```dax
Actual PY =
CALCULATE ( [Total Actual], SAMEPERIODLASTYEAR ( dim_date[month_start] ) )

YoY Growth % =
DIVIDE ( [Total Actual] - [Actual PY], [Actual PY] )

Actual PM =
CALCULATE ( [Total Actual], DATEADD ( dim_date[month_start], -1, MONTH ) )

MoM Growth % =
DIVIDE ( [Total Actual] - [Actual PM], [Actual PM] )

Actual Rolling 3M Avg =
AVERAGEX (
    DATESINPERIOD ( dim_date[month_start], LASTDATE ( dim_date[month_start] ), -3, MONTH ),
    [Total Actual]
)
```

Headcount YoY uses the semi-additive base, not a sum:

```dax
Headcount FTE PY =
CALCULATE ( [Headcount FTE], SAMEPERIODLASTYEAR ( dim_date[month_start] ) )

Headcount YoY % = DIVIDE ( [Headcount FTE] - [Headcount FTE PY], [Headcount FTE PY] )
```

## Vacancy (workforce planning)

```dax
Vacancy FTE  = [Planned FTE] - [Employee FTE]     -- unfilled budgeted seats
Vacancy Rate = DIVIDE ( [Vacancy FTE], [Planned FTE] )
Contractor Share = DIVIDE ( [Contractor FTE], [Employee FTE] + [Contractor FTE] )
```

## Forecast accuracy (from the backtest fact)

Load `data/reports/backtest_report.json` (flattened) or the champion metrics as
a `fact_forecast_accuracy` table (target, model, mape, rmse, coverage80). Then:

```dax
Forecast MAPE = AVERAGE ( fact_forecast_accuracy[mape] )

-- Accuracy reads better than error on an executive tile.
Forecast Accuracy % = 1 - [Forecast MAPE]

Interval Coverage 80 = AVERAGE ( fact_forecast_accuracy[coverage80] )
```

## Data quality (from the QC scorecard)

Load `data/reconciled/scorecard.json` reason counts as `fact_quarantine`
(source, reason, rows). Then:

```dax
Rows Quarantined = SUM ( fact_quarantine[rows] )

Quarantine Rate =
DIVIDE ( [Rows Quarantined], [Rows Quarantined] + SUM ( fact_quarantine[rows_kept] ) )
```

## Notes on model design

- **One date table** (`dim_date`), marked as such, drives all time intelligence.
- Relationships are single-direction (dimension → fact); no bidirectional
  filters, which avoids ambiguity and keeps measures predictable.
- Headcount measures never appear as implicit sums in a visual — always via the
  semi-additive measures above. This is enforced by hiding the raw
  `total_fte`/`employee_fte` columns and exposing only the measures.
