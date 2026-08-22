# Robustness and controlled-selection report

- Run ID: `20260822T000641481839Z`
- Unique grid configurations: **96**
- Candidate backtest trials: **117**
- Immutable ledger records: **124**
- Live promotion: **disabled**
- Final test access: only after the candidate-lock ledger event.

## Scoring rule

stage score = Sharpe + 0.25*Calmar - 0.75*max_drawdown - 0.02*annualized_turnover; training selection = 0.25*candidate score + 0.75*one-parameter-neighborhood median - 0.50*neighborhood dispersion; lock score = 0.25*training region score + 0.35*validation score + 0.40*walk-forward median - 0.25*|training-validation| - 0.25*walk-forward dispersion

## Stable-region finalists

| Candidate | Training region score | Validation score | Walk-forward median | Instability penalty | Lock score |
|---|---:|---:|---:|---:|---:|
| mw120_sw00_ma150_n2_r07_v30 | 1.3566 | 1.1684 | 0.7897 | 0.2656 | 0.7983 |
| mw120_sw00_ma150_n2_r07_v60 | 1.2794 | 1.0946 | 0.6776 | 0.2609 | 0.7131 |
| mw060_sw07_ma150_n2_r07_v30 | 1.1971 | 1.7583 | 0.4875 | 0.4664 | 0.6433 |
| mw120_sw07_ma150_n2_r07_v30 | 1.2216 | 1.1515 | 0.7873 | 0.3851 | 0.6383 |
| mw060_sw07_ma150_n2_r14_v30 | 1.2095 | 1.1502 | 0.4274 | 0.4053 | 0.4706 |

## Locked candidate

- Candidate: `mw120_sw00_ma150_n2_r07_v30`
- Parameters: `{"momentum_window": 120, "rebalance_days": 7, "selected_assets": 2, "skip_window": 0, "trend_filter": 150, "volatility_window": 30}`
- Lock score: `0.7983`

## Sealed final-test result

- CAGR: `-0.08%`
- Sharpe: `0.122`
- Maximum drawdown: `20.80%`
- Turnover: `17.859`

> The locked strategy remains research-only. No live-trading promotion is performed.
