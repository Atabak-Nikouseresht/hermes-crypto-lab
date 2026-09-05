# Governance and protocol preservation

## Frozen forward baseline

Forward validation began from:

| Item | Frozen value |
|---|---|
| Commit | `ebeac389b1c309f1ef8f5a9056e96c3b28e08e01` |
| Locked candidate | `mw120_sw00_ma150_n2_r07_v30` |
| Strategy SHA-256 | `29451632091c5cf6d33cd58a03a2bd5a1bf52297a21375b9ae5e5b6fbbbac2d6` |
| Comprehensive economic-spec v2 SHA-256 | `1dc381ce5e8c663b1e1aab496503837945547d77944d082f873eab8f127eb395` |
| Future protocol | `paper-exec-v3-ask-bid-minspread-utc0010` |
| Baseline hardening-manifest SHA-256 | `44dcd4d6c65fb88a2fb4ed6c55607ef4351570d8a70ac665ea5851a6670efe1d` |

A byte-for-byte copy of the baseline hardening manifest and sidecar is preserved under `forward_experiment/baselines/ebeac389/`. The active hardening manifest may be regenerated after documentation or operational-maintenance changes so that CI can attest the current tree; the archived baseline remains the trust anchor for the start of forward validation.

## Trust anchors

`forward_experiment/` contains:

- `checkpoint_manifest.json` — pre-forward research checkpoint and data cutoff;
- `governance.json` — original sealed forward-governance contract;
- `governance_amendment_v2.json` — additive schedule/review terminology amendment;
- `governance_amendment_v3_economic_spec.json` — additive comprehensive economic-integrity anchor;
- `execution_protocol_v2.json` — preserved protocol used by historical paper fills;
- `execution_protocol_v3.json` — protocol for future fills;
- `paper_schema.sql` — captured paper-state schema;
- `scheduler_manifest.json` — expected scheduler and wrapper evidence;
- `hardening_manifest.json` — current critical-file hash chain.

Original governance and protocol records are not consolidated or rewritten for presentation convenience.

The legacy strategy hash remains unchanged for historical continuity. The
versioned `economic-spec-v2` hash additionally covers the candidate, complete
signal and allocation parameters, universe, capital denomination, rebalance and
schedule timing, public-data source/lookback and validation thresholds,
exchange-rule enforcement, quantity tolerance, modeled costs, and execution
protocol. Dictionaries are serialized with sorted keys, and economically
unordered collections are sorted before canonical JSON SHA-256 hashing. The v3
amendment explicitly records that no strategy was reselected, no parameter was
retuned, and no research result changed.

The publication-security rewrite changed public Git commit identities without
changing sealed evidence. The historical frozen baseline
`ebeac389b1c309f1ef8f5a9056e96c3b28e08e01` maps to rewritten public commit
`1ae75af22c1cf09cf3179823647f7f5a40f845c7`. Old identifiers embedded in
sealed artifacts retain their pre-rewrite meaning; see
[Public history rewrite](public-history-rewrite.md).

## Forward-validation rule

The governing sequence is:

```text
observe → record → measure → compare
```

The following are prohibited without explicit approval and a new strategy or protocol version:

- parameter changes;
- universe, ranking, eligibility, entry, exit, or regime changes;
- adaptive behavior based on forward outcomes;
- changes to trade occurrence, selection, size, timing, execution price assumptions, portfolio risk, or expected return;
- candidate reselection;
- use of forward observations as training or calibration data;
- live trading or private exchange APIs.

## Allowed maintenance

Operational fixes may address crashes, corrupt state, incorrect data handling, timestamp errors, logging failures, broken public API interaction, security defects, or violations of already-defined execution rules. A fix must preserve the strategy's economic behavior.

New official scheduled paper runs record locally verified Git commit/cleanliness,
active hardening-manifest hash, and execution-protocol provenance before any
order or fill. Historical runs are not backfilled. Quote timestamp skew is read
from the governed quote-coherence contract and must exactly match runtime.

Documentation, naming, repository metadata, and generated-artifact policy may change when they do not affect runtime behavior. Such changes are recorded separately from the frozen strategy baseline.

## Historical integrity

Existing v2 orders and fills remain unchanged and retain their original protocol version, timestamps, identifiers, and state. Future fills use v3. The two protocols are never merged into an unversioned series.

The tracked controlled-research run directories are preserved because they contain candidate locks, hash-chained ledgers, final-test records, and audit provenance. Apparent duplication is disclosed rather than deleted.

## No automatic promotion

No duration, backtest metric, passing test suite, or small forward sample authorizes live capital. Any real-money consideration requires explicit independent human review outside this repository's automated paths.