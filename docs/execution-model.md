# Execution model

## Classification

```text
EXECUTION_MODEL_MISMATCH — explicitly versioned, not erased
```

The sealed historical backtest and forward paper path use different execution timestamps and sizing evidence. Forward observations must not be described as a direct validation of the sealed next-close assumption.

For newly persisted paper orders, `paper_orders.requested_quantity` is the final
executable simulated quantity after position restriction, cash scaling,
step-size normalization, and exchange-filter validation. A current-semantics
`FILLED` order must therefore match its associated fill quantity. The original
pre-scaling strategy proposal remains in run diagnostics. This is ledger/audit
semantics hardening only; it does not change filled quantity, price, costs,
cash, positions, timing, risk, or the v3 execution protocol.

## Sealed historical protocol

- Daily candle labels are Binance/CCXT candle-open timestamps.
- A Sunday candle labeled `Sunday 00:00 UTC` is finalized at `Monday 00:00 UTC`.
- The signal uses data through the Sunday row only.
- The event engine queues fixed quantities sized from the Sunday close.
- Orders fill on the next daily row's **close**.
- That Monday close is available at Tuesday `00:00 UTC`.
- All 480 audited historical fills had a one-row/one-day label delta.
- Original sealed backtest artifacts remain unchanged.

## Forward protocol v3

Future orders use:

```text
paper-exec-v3-ask-bid-minspread-utc0010
```

Existing genuine v2 orders/fills remain labeled `paper-exec-v2-ask-bid-utc0010` and are not rewritten.

Forward contract:

- Decision window: Monday 00:05–00:20 UTC; target 00:10 UTC.
- Signal data: previous finalized Binance daily candle.
- Buy base: the more adverse of fresh ask or midpoint plus minimum adverse spread.
- Sell base: the more adverse of fresh bid or midpoint minus minimum adverse spread.
- Additional adverse slippage and proportional fee apply after the base price.
- Quote timestamp must be no later than execution timestamp.
- Missing, crossed, non-positive, non-finite, future-dated, or stale quotes fail closed.
- Public exchange activity, amount, notional, step-size, and precision rules are required.
- After proportional cash scaling, final buy quantities are quantized down to the public step size and min/max quantity plus minimum-notional rules are rechecked before any order or fill is persisted.
- New order/fill rows record protocol, strategy hash, quote time, spread, slippage, fee, and execution time.

The forward quote is approximately 23 hours 50 minutes earlier than availability of the sealed model's Monday close. Forward sizing also uses execution-time quote midpoints rather than carrying fixed Sunday-close quantities.

## Historical examples

### 2019-04-07 → 2019-04-08

- BTC BUY quantity `0.22099841`: signal close `5,170.27`; next-close market `5,236.90`; simulated execution `5,239.51845`.
- BNB BUY quantity `42.21123558`: signal close `18.9523`; next-close market `17.9777`; simulated execution `17.98668885`.

### 2023-04-09 → 2023-04-10

- ETH fully sold: `9.19102229`.
- BTC target 63.9383%: requested `0.19316465`, cash-scaled fill `0.19189981`; close moved `28,323.76 → 29,637.34`.
- XRP target 36.0617%: requested `22,980.26501585`, cash-scaled fill `22,829.79064277`; close moved `0.5054 → 0.5176`.

### 2025-11-02 → 2025-11-03

- BNB SELL `29.16988070`: market `993.54`; simulated execution `993.04323`.
- ETH SELL `11.16151103`: market `3,603.83`; simulated execution `3,602.028085`.

These examples document the sealed model. They were not used to retune the strategy or choose forward timing.