# Public history rewrite

## Purpose and scope

The public Git history was rewritten on `2026-08-28T08:19:54Z` to remove an operational Telegram delivery destination from every reachable historical file version and to remove the accidental `witch main` file from all history. The destination itself is intentionally not reproduced here.

This was a publication-security and repository-hygiene rewrite only:

- no strategy was reselected;
- no strategy parameter was retuned;
- no economic assumption, signal, target weight, risk rule, or execution protocol was changed by the rewrite;
- no research result, order, fill, ledger, checkpoint, or sealed governance artifact was changed by the rewrite;
- the original sealed artifacts remain byte-for-byte preserved where their cryptographic evidence requires it.

The rewrite used `git filter-repo`. A private backup bundle of the pre-rewrite repository was created outside the repository before rewriting and is not part of the public tree or refs.

## Commit identity mapping

Git commit identifiers necessarily changed where a rewritten ancestor or tree changed. Important pre-rewrite references map to the public history as follows:

| Pre-rewrite commit | Rewritten public equivalent | Meaning |
|---|---|---|
| `50bc10298aa5e897aaca8691943d32b60b829324` | `50bc10298aa5e897aaca8691943d32b60b829324` | Initial history; unchanged |
| `e73adf3115b24f4fdc7f15a0b11624d327265af3` | `41ba50e053187f736a1459fd08c5aca0638a02e2` | Locked forward-paper experiment operations |
| `ef32e0aec14cd678b7f7b47453bdc4d4ce8fb743` | `7c889f177861915c5937eab5891fb71b762a3df0` | Forward-paper operations baseline |
| `ebeac389b1c309f1ef8f5a9056e96c3b28e08e01` | `1ae75af22c1cf09cf3179823647f7f5a40f845c7` | Frozen forward-validation baseline |
| `a8284d458f57c242d695926182441cd78f6eb3f9` | `47cc493815fedf4efddb14959c0c5efb6e21e48d` | Public-presentation preparation |
| `575534797cd5acc295ad11af4c8c872673352c56` | `bef8359b7afbae67d038a67956faeaa2e9c0a17d` | Known public HEAD before remediation |
| `a9780db6743bf513199952d22ed791794b577be9` | `c42923126c9ed344d1d0ffb30a3c361dc204099e` | Verified engineering and integrity remediation |
| `95bfacc72d9335b22018f7613944436203227777` | Removed as empty | Accidental-file-only commit after path removal |

The complete machine-generated mapping remains local in `.git/filter-repo/commit-map`; it is not an operational runtime artifact and is not published as a Git ref.

## Reading sealed evidence after the rewrite

Old commit SHAs embedded in sealed checkpoints, manifests, governance records, audit reports, or archival directory names refer to the pre-rewrite research history. They are retained intentionally because changing them would invalidate existing cryptographic evidence. In particular, directories such as `forward_experiment/baselines/ebeac389/` keep their archival names.

Use the mapping above to relate those historical identifiers to the rewritten public graph. The legacy locked strategy hash and the additive comprehensive economic-spec v2 hash are independent of Git commit identity and remain verified by CI.
