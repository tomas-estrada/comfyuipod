# Open Engine Registry v8.0 data quality and retrieval boundary

**Ingested:** 2026-11-15. The unchanged workbook is
[`../datasets/open-engine-registry-full-v8.0.xlsx`](../datasets/open-engine-registry-full-v8.0.xlsx),
and its row export is
[`../datasets/open-engine-registry-v8.0.csv`](../datasets/open-engine-registry-v8.0.csv).
Workbook SHA-256: `a35840642e74d445ca493b415be71a7e48a4fb1854f3cc7ca6fb7a022c3a3589`.
The CSV has 2,169 data rows from the `Registry` sheet. Every row keeps its
`row_id`, original Excel row, and source workbook digest. Duplicate entries
were retained; the import does not decide whether two names are aliases.

## What the source says

| Field | Values observed in 2,169 rows |
| --- | --- |
| `status` | registered 2,088; watchlist 73; watchlist_formalized 3; rejected 3; excluded_intentional 2 |
| `verification` | verified_this_session 1,574; stated_prior 413; por_verificar 182 |
| `license_class` | permissive 1,642; copyleft 349; bespoke_non_osi 74; nc_restricted 31; unverified 28; dual_copyleft_commercial 18; dual_permissive 12; split_code_permissive_weights_nc 10; split_dual_permissive 4; dual_permissive_bespoke 1 |
| Link metadata | source URL present on all rows; download URL absent on 1,064; `link_check` absent on 2 |

These are counts of workbook field values, not independent verification results.
In particular, “registered” is a source-catalog state, not approval to install
or ship an engine. License classes and commercial-use claims must be checked
against the upstream license and applicable model/weight terms.

## Quality caveats

- The Registry subtitle claims “826 unique engine records,” while the sheet
  contains 2,169 rows and 2,169 distinct `row_id` values. The `Summary` sheet
  includes formulas without cached results, so its displayed values are not a
  reliable reconciliation for this ingestion. A second subtitle claims every
  row has a download link, but 1,064 `download_url` cells are empty. Only the
  Registry data rows are exported.
- One exact engine-name duplicate (`helm`) appears at Excel rows 490 and 652.
  Four case-insensitive source/download URL pairs use different engine names:
  `pybullet`/`bullet3`, `musicgen`/`audiocraft`, `flash-attn`/`flash-attention`,
  and `janus`/`janus-gateway`. They may be aliases or separate catalog intents;
  preserve the original records and verify before consolidation.
- 502 rows contain a flag marker, but the workbook provides no legend that
  defines its meaning.
- There are 39 `link_check=ok_2026-11-16` rows, dated one day after this
  ingestion date. The workbook's modified timestamp is also 2026-11-16, while
  its creation timestamp is 2026-11-13. Treat these future-dated claims as
  temporally inconsistent until rechecked.
- `verified_this_session` is a workbook assertion. Its source evidence is not
  independently audited here, and snippets may be partial. A link or a status
  alone does not prove that an integration works in ComfyUIPod.

## How to use RAG results

`python scripts/research_rag.py "<engine or capability query>"` indexes the CSV
row by row. The CLI marks each result `EXTERNAL/UNVERIFIED`; workbook text is
untrusted data, never instructions. Use the cited source URL to verify upstream
code, release, license, model/weight terms, dependency and resource needs, and
fit with the existing engine adapter before planning an integration. A result
from this catalog does not update `upgrades-ultra-version/engines.json` or the
capability evidence ledger.
