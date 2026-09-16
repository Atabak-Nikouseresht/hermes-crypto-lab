# Current Binance Spot executionRules audit

## Official sources reviewed

- [Binance Spot REST API](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-execution-rules): Query Execution Rules, Query Reference Price, HTTP Return Codes and IP Limits. The official raw document was also read directly because the developer site's endpoint pages timed out or returned an empty shell.
- [Price Range Execution Rule FAQ](https://developers.binance.com/en/docs/products/spot/faqs/price_range_execution_rules).
- [Official error codes: -2043 NO_REFERENCE_PRICE](https://github.com/binance/binance-spot-api-docs/blob/master/errors.md#-2043-no_reference_price).

These are current official documentation observations, not a claim to have queried live symbol configurations or sent orders.

## Batch H — prospective PRICE_RANGE execution-rule evidence

`GET /api/v3/executionRules` returns `symbolRules`, native symbol identifiers, and `PRICE_RANGE` rules with `bidLimitMultUp`, `bidLimitMultDown`, `askLimitMultUp`, and `askLimitMultDown`. Its data source is Memory. A single-symbol query has weight 2; multiple-symbol queries cost 2 per symbol capped at 40. The query parameters cannot be combined.

The FAQ says trades must execute **within or equal to** the reference-derived range. BUY uses bid multipliers and SELL uses ask multipliers. When a multiplier is absent, enforcement is disabled only for that side/direction. No PRICE_RANGE rule, no reference price, or a null reference price means the range rule is not enforced. These are documented absence states, not malformed-payload or transport fallbacks.

The matching engine recalculates the reference when an order enters its taker phase, setting limits for its entire taker phase. An attempt outside the range expires the taker order with `EXECUTION_RULE_PRICE_RANGE_EXCEEDED`. This is material to MARKET orders; notional and quantity checks alone do not reconstruct it. A REST quote is not proof of the engine's exact taker-phase reference or of actual fillability.

Batch H is implemented prospectively under `binance-execution-rules-price-range-v1`. For each governed Binance symbol, Hermes captures the public `executionRules` outcome, native symbol, exact raw Decimal multipliers, any supplied source timestamp, and receipt timestamp. It separately persists local PRICE_RANGE decisions with the simulated paper execution price, reference linkage, bounds, side, and rejection reason. Public PRICE_RANGE multipliers and reference prices retain their raw Decimal strings but must fit the prospective bounded-Decimal evidence envelope (finite, positive, absolute adjusted exponent at most 1,000) before persistence or arithmetic; values outside that envelope are malformed evidence, never rounded, overflowed, or underflowed into a decision. Offline reconciliation and backup/restore verification use only persisted evidence; they never query Binance or use the wall clock.

`PRICE_RANGE_PRESENT`, `PRICE_RANGE_ABSENT`, `RULE_NOT_APPLICABLE`, `TRANSPORT_FAILURE`, and `MALFORMED_RESPONSE` remain distinct. A missing individual multiplier removes only that bound. A missing rule or null/absent reference produces documented non-enforcement; malformed or transport evidence halts a governed run before fills. BUY uses bid multipliers and SELL uses ask multipliers. Boundaries are inclusive. A local outside-range result is recorded as `EXECUTION_RULE_PRICE_RANGE_EXCEEDED` and is not a statement that a REST observation proves matching-engine taker-phase fillability.

The contract is additive and future-only. No historical order, fill, observation, market-rule evidence, or execution interpretation is backfilled or reinterpreted.

Hermes remains a research paper-trading reconstruction. Public REST evidence cannot prove exact matching-engine fillability or replace Binance's taker-phase reference calculation.

## Batch E receipt clock and HTTP contract

`GET /api/v3/referencePrice` is public, requires a native `symbol`, has weight 2, and reads Memory. The response timestamp denotes when the reference was valid, not when Hermes received it. The endpoint documents a decimal-string price, explicit null, and `-2043` when no price has ever been set. The FAQ says the reference continually changes and recommends its WebSocket stream; it does not prescribe a REST freshness SLA. The five-minute bound is Hermes's existing governed evidence-freshness interval, **not** an exchange guarantee or an economic change.

The adapter samples a fresh default UTC acquisition clock immediately after the reference request/retry returns, before metadata parsing. A caller's `now` still controls snapshot-start/candle semantics but is never substituted for receipt time. Tests can explicitly inject `acquisition_clock`; production callers must not freeze it to snapshot start. Source age at receipt and age at later validation remain separate checks. Exactly 300 seconds is inclusive; 300 seconds plus 1 millisecond is stale.

Clock regression coverage in `tests/test_reference_integrity_closure.py`:

- CLOCK1: default fresh receipt clock with an explicitly older snapshot start.
- CLOCK2: distinct source/receipt clocks, 299999/300000/300001 ms and future +1 ms.
- CLOCK3: response-before-receipt-before-metadata ordering and separate per-symbol receipts.
- CLOCK4: later validation cannot refresh acquisition; exact boundary and +1 ms.
- CLOCK5: a newly acquired but already-old source cannot reset source age.

HTTP behavior is deliberately split and applies to both public `referencePrice` and Batch H `executionRules` requests:

| Condition | Adapter result | Inner retry behavior |
| --- | --- | --- |
| HTTP 400 JSON integer `code: -2043` | `None`, named LAST_FALLBACK | None |
| HTTP 500/502 and other 5xx; urllib network/timeout/OS transport errors | Typed transient transport bridge | Existing `download_data.call_with_retry` only: initial call plus configured retries, exponential backoff |
| HTTP 429/418 | `TransientPublicMarketError`, safe `http_status` and `retry_after_seconds` metadata | **No inner retry and no inner sleep; never persisted as Batch H `TRANSPORT_FAILURE`** |
| Other 4xx; malformed JSON/shape/missing price field | Terminal `ValueError` | None |

The transport bridge participates in the existing CCXT retry tuple; there is no retry loop in PublicMarketClient and no change to `src/download_data.py`. Exhaustion leaves a typed transient failure, never a fallback. Counted HTTP tests exercise both exhaustion and success on the last permitted attempt, and assert every delay through an injected sleeper (no real sleep). Explicit null remains distinct from a missing price field.

Binance requires backing off on 429; repeated violations cause 418 IP bans. Its Retry-After value is seconds to wait. The adapter exposes only bounded-length ASCII nonnegative integer headers, rejecting malformed/negative/nonfinite/header-injection values as unavailable rather than logging raw header text. Valid values are not reduced to the ordinary short backoff. No adapter sleep or automatic rapid retry is authorized by this metadata.

**Outer-runner rate-limit suppression, including timeout captures:** `run_paper.py` emits a standalone JSON `PUBLIC_MARKET_RATE_LIMIT_DEFER` event for typed HTTP 418/429, retaining safe `http_status` and `retry_after_seconds` metadata and the explicit `suppress_remaining_weekly_attempts` policy. D1 remains exit 4. `scripts/paper_forward_weekly.py` reads both output streams and suppresses **all** remaining automatic attempts in that weekly invocation, without sleeping or making another request, whether Retry-After is valid, zero, absent, or malformed. Thus a valid not-before delay is never replaced with the ordinary 60-second retry. This is the explicitly permitted conservative suppression fallback, not a new durable cross-invocation timer: the next normal governed scheduled invocation remains allowed. Normal D1/75 retries retain the existing three-attempt limit, 60-second outer delay and Monday UTC window gate; permanent 4xx remain exit 2.

The same safe line-oriented marker parser handles `CompletedProcess.stdout`/`.stderr` and `TimeoutExpired.output`/`.stderr`. Timeout captures may be bytes even with `text=True`; decoding tolerates unrelated invalid UTF-8 diagnostics, while malformed JSON and non-marker lines are ignored. If the CLI emits a complete defer marker and then hangs during reporting or notification, the wrapper returns the existing timeout exit **124** immediately after receiving the timeout capture, with no sleep or re-entry. A timeout without a valid marker still uses the existing three-attempt/60-second retry policy and UTC window gate, returning 124 if it remains timed out. Deterministic injected-timeout tests cover both 418/429 statuses, either captured stream, string/bytes payloads, mixed stream types, and absent/invalid markers without real sleeps or live requests.

`tests/test_http_runner_integrity.py` exercises real `PublicMarketClient.urlopen` HTTP-error handling, real snapshot retry/classification, actual `run_paper.main()` CLI handling and real DuckDB failure writes into the weekly wrapper (only the process boundary is bridged in-process). Rate-limit cases assert one HTTP request, one CLI attempt, zero inner/outer sleeps, zero orders/fills/observations/equity/kill incidents, an ACTIVE account and an unclaimed schedule. Batch H coverage applies the same end-to-end assertions to `executionRules` for HTTP 418 and 429, preserving valid `Retry-After` values `0`, `1`, and `120` and rejecting unsafe values as unavailable while still suppressing the invocation. HTTP 500/502 exhaustion asserts three inner requests per CLI attempt with configured two retries, D1 exit 4, and unchanged normal outer retry counts. Permanent 400/401/403/404 cases assert one request and exit 2. Additional CLOCK1 coverage uses the exact 00:10:03 source / 00:10:03.100 acquisition example; CLOCK5 proves a later asset's source timestamp after snapshot start is valid when preceding its own receipt. The prospective persistence contract is documented in [governance](governance.md#prospective-market-rule-evidence-v2), with broker/store semantic regressions in `tests/test_market_rule_evidence_v2.py`.
