# Forward paper validation

## Objective

The current objective is to collect clean, unbiased observations on the locked candidate. It is not to improve recent results.

Forward paper trading uses the existing v3 path and public market data only. Real capital and private exchange endpoints remain out of scope.

## Required provenance

Every forward result must remain traceable to:

- strategy/candidate version;
- execution protocol version;
- locked strategy hash;
- code commit;
- market-data timestamp;
- quote timestamp;
- execution timestamp;
- schedule key and run identifier.

Historical v2 fills keep their original labels. New records use v3.

## Decision and execution evidence

The audit trail records, where applicable:

- invocation timestamp and schedule status;
- regime and strategy diagnostics;
- available, selected, rejected, and skipped assets;
- target weights and proposed turnover;
- observed bid, ask, midpoint, spread, minimum-spread adjustment, slippage, fee, and execution price;
- market-rule validation and rejection reasons;
- orders, fills, cash, positions, and equity;
- state before and after committed execution;
- missed windows, validation failures, data-quality failures, incidents, and system errors.

## Experimental separation

Forward observations live in the paper database and forward-report paths. They must not be added to:

- backtest input data;
- candidate grids;
- training or validation tables;
- model-development datasets;
- parameter-selection reports.

The monthly report reads forward observations only and uses identical timestamps for descriptive benchmarks.

## Measurement plan

As sample size permits, cumulative reporting can include:

- total and annualized return;
- volatility, Sharpe, Sortino, drawdown, and Calmar;
- hit rate, trade count, average winner/loser, payoff ratio, and profit factor;
- turnover and transaction-cost impact;
- spread and slippage impact;
- exposure and concentration;
- asset-, regime-, and signal-level contributions where defined.

Metrics that are statistically meaningless for the available sample should be omitted or labeled insufficient rather than forced.

## Forward-versus-backtest review

Once sufficient observations exist, a descriptive comparison can evaluate:

- return and Sharpe degradation;
- drawdown and hit-rate differences;
- turnover and execution-cost differences;
- asset and regime contribution;
- signal frequency;
- realized versus expected trade characteristics;
- evidence consistent with overfitting, instability, regime dependence, signal decay, or execution-model mismatch.

The comparison cannot be used to silently retune the strategy.

## Review cadence

- 12 weeks: operational checkpoint only
- 26 weeks: preliminary performance observation
- 52 weeks: minimum substantive forward review

Duration alone does not prove profitability.

## Interpretation standard

A technically correct system is not evidence of a profitable strategy. Passing tests, reliable scheduling, functioning governance, historical backtest results, or a small number of favorable forward trades are insufficient to claim validation or live-trading readiness.