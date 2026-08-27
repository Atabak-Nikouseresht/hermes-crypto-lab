# Execution-timing audit

Status: **EXECUTION_MODEL_MISMATCH — explicitly versioned, not erased**

## Sealed historical protocol

- Daily candle labels are Binance/CCXT candle-open timestamps.
- A Sunday candle labeled `Sunday 00:00 UTC` is finalized at `Monday 00:00 UTC`.
- The signal uses data through the Sunday row only.
- The event engine queues fixed quantities sized from the Sunday close and fills them on the next daily row's **close**.
- That Monday close is available at Tuesday `00:00 UTC`.
- All 480 audited historical fills had a one-row/one-day label delta.
- The original sealed backtest and artifacts remain unchanged.

## Forward protocol

Version for future orders after hardening: `paper-exec-v3-ask-bid-minspread-utc0010`. Existing genuine scheduled v2 fills remain labeled `paper-exec-v2-ask-bid-utc0010` and were not rewritten.

- Valid window: Monday `00:05–00:20 UTC`; target `00:10 UTC`.
- Previous finalized daily candle: Sunday, available Monday `00:00 UTC`.
- Public Binance spot ticker is required to provide timestamped bid and ask.
- Buys start from ask; sells start from bid; additional adverse slippage and fees are applied.
- Missing, crossed, non-positive, non-finite, future-dated, or stale quotes are rejected.
- Public exchange amount/notional/precision rules are required and invalid proposals are rejected.
- New order and fill rows record the execution-protocol version.

The forward quote is approximately 23h50m earlier than the sealed model's Monday close availability and is therefore not temporally/economically equivalent. Forward sizing also uses execution-time quote midpoints rather than carrying fixed Sunday-close quantities. Forward observations cannot be presented as a direct validation of the sealed next-close assumption.

## Concrete historical examples

1. **2019-04-07 → 2019-04-08**
   - BTC BUY quantity `0.22099841`: signal close `5,170.27`; next-close market `5,236.90`; simulated execution `5,239.51845`.
   - BNB BUY quantity `42.21123558`: signal close `18.9523`; next-close market `17.9777`; simulated execution `17.98668885`.

2. **2023-04-09 → 2023-04-10**
   - ETH fully sold: `9.19102229`.
   - BTC target 63.9383%: requested `0.19316465`, cash-scaled fill `0.19189981`; close moved `28,323.76 → 29,637.34`.
   - XRP target 36.0617%: requested `22,980.26501585`, cash-scaled fill `22,829.79064277`; close moved `0.5054 → 0.5176`.

3. **2025-11-02 → 2025-11-03**
   - BNB SELL `29.16988070`: market `993.54`; simulated execution `993.04323`.
   - ETH SELL `11.16151103`: market `3,603.83`; simulated execution `3,602.028085`.

These examples document the sealed model; they were not used to choose the new forward execution time or alter the locked candidate.
