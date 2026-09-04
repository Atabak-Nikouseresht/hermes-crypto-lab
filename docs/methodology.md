# Research methodology

## Research boundary

Hermes Crypto Lab is a systematic-crypto research and paper-validation system. It does not automatically promote a candidate to real trading, and forward observations are not reused for parameter tuning.

All strategy timestamps are UTC and use finalized public Binance spot daily candles.

## Universe

The fixed universe is:

- BTC/USDT
- ETH/USDT
- BNB/USDT
- XRP/USDT
- TRX/USDT

## Strategy family

At a high level, the research family combines:

1. cross-sectional momentum ranking;
2. a BTC trend regime filter;
3. deterministic top-N selection;
4. inverse-volatility target weights;
5. individual and combined-altcoin caps;
6. explicit residual USDT;
7. long-only, unlevered execution.

The root configuration contains two deliberately separate contracts:

- `strategy` — the fixed general baseline used by `run_backtest.py`;
- `parameter_grid` — the historical controlled grid consumed only by `run_experiments.py`.

The forward paper system does not read the grid for adaptation or reselection.

## Locked candidate

The frozen forward candidate is:

```text
mw120_sw00_ma150_n2_r07_v30
```

Its locked dimensions are:

| Dimension | Value |
|---|---:|
| Momentum window | 120 days |
| Skip window | 0 days |
| BTC trend filter | 150 days |
| Maximum selected assets | 2 |
| Rebalance interval | 7 days |
| Volatility window | 30 days |

The locked strategy SHA-256 is:

```text
29451632091c5cf6d33cd58a03a2bd5a1bf52297a21375b9ae5e5b6fbbbac2d6
```

Do not infer the locked parameters from the separate fixed-baseline fields in `config/strategy.yaml`; the candidate ID and trust-anchor verification define the forward strategy.

## Controlled research chronology

The historical manager enforced:

1. a 60% training period over the allowed 96-case grid;
2. stable-neighborhood scoring rather than isolated best-return selection;
3. five fixed finalists for the 20% validation period;
4. three fixed-finalist rolling pre-test evaluation folds ending before final-test access;
5. penalties for degradation, dispersion, drawdown, and turnover;
6. a hash-chained candidate-lock event;
7. final-test access for the locked candidate only.

The repository preserves two deterministic run directories. The first final-period access is canonical for sealed-result interpretation; the later run is retained because deleting it would conceal provenance. Neither is reused for forward retuning.

`forward_experiment/research_lifecycle.json` seals V1 candidate selection and
final-test access. The V1 entry point fails closed before it can open research
prices or create a run. Any future research requires separately governed V2
specification and genuinely untouched out-of-sample data; it cannot reopen V1.
Every declared finalist must complete validation before the gate permits a
candidate lock.
The historical V1 entrypoint rejects a substituted V2 lifecycle: future research
requires its own separately governed entrypoint as well as its own contract.

The historical reports retain the compatibility label `walk_forward`, but the
implementation does not retrain or reselect candidates in each fold. It evaluates
the already selected finalists over sequential pre-test periods using all price
history available through each fold end. This is rolling pre-test evaluation, not
a true expanding retrain→test walk-forward process.

For future experiment runs, methodology `future-selection-v2-nonoverlap-oos`
uses disjoint training, validation, and final-test calendars. Validation is the
only scored OOS input to candidate locking; non-overlapping validation folds are
retained as diagnostics and are not scored again. Historical sealed runs retain
their original methodology and artifacts unchanged.

## Historical execution

The sealed historical engine:

- calculates a signal from the final daily row available for the week;
- sizes fixed quantities using signal-time information;
- queues orders;
- fills on the next available daily row's close;
- applies adverse slippage and proportional fees;
- executes sells before buys;
- scales buys proportionally if necessary to preserve non-negative cash.

Orders, fills, positions, cash, and equity are stored as separate artifacts.

## Metrics and benchmarks

Historical reports include CAGR, annualized volatility, Sharpe, Sortino, maximum drawdown, Calmar, CVaR, turnover, recovery duration, fees, and ending equity.

Benchmarks include BTC buy-and-hold, static equal weight, static BTC/ETH, and
`Cash (USDT, zero modeled yield)` under explicitly documented timing and cost
assumptions. Cash is reporting-only and does not enter candidate scoring or
selection.

## Forward measurement

Forward observations are measured separately from historical and optimization data. When enough observations exist, comparisons may cover return, risk-adjusted return, drawdown, hit rate, turnover, execution costs, exposure, concentration, asset contribution, regime behavior, and signal frequency.

A comparison is descriptive evidence. It may identify possible overfitting, instability, regime dependence, signal decay, or execution mismatch, but it does not authorize retuning.

## Scientific limitations

- Historical selection creates post-selection uncertainty.
- A sealed backtest cannot reproduce live market microstructure.
- Paper fills do not establish real fillability.
- The forward bid/ask protocol and sealed next-close model differ materially;
  this remains explicitly classified as `EXECUTION_MODEL_MISMATCH`.
- Small forward samples have low statistical power.
- Stable software and passing tests do not establish an economic edge.