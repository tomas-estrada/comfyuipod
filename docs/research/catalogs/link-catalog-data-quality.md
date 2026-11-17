# User link-catalog data quality

**Date:** 2026-11-12. Inputs are the unchanged CSVs in `docs/research/datasets/`.
The catalogs are now indexed row by row by `scripts/research_rag.py`; retrieved
rows are labeled `EXTERNAL/UNVERIFIED` and keep source-file/row/title/URL
provenance.

## Measured structure

| Check | ComfyUI catalog | Blender/3D catalog |
|---|---:|---:|
| Data rows / columns | 466 / 45 | 314 / 15 |
| Nonempty distinct raw URLs | 464 | 312 |
| Missing raw URLs | 2 | 2 |
| Distinct URL hosts | 49 | 63 |
| Citation references / distinct citation URLs | 11,906 / 2,021 | 2,620 / 681 |
| JSON basis cells parse | 100% | 100% |

ComfyUI has one duplicated nonempty title (`ComfyUI-VideoHelperSuite`); exact
URL duplication is limited to the two blank URL cells. Its canonical URL field
contains the literal `skipped` 75 times; 81 rows differ between raw and
canonical URL values. Many differences are intentional URL normalization or
repository renames, but the field cannot be used as a directly fetchable URL
without row-level fallback logic. The ComfyUI check distributions include
`ecosystem=463 Yes / 3 Pending`, `ComfyUI-specific=462 Yes / 1 No / 3 Pending`,
`distinct-resource=459 Yes / 4 No / 3 Pending`, and `recent-activity=411 Yes /
52 No / 3 Pending`.

The Blender catalog has two blank URLs and no repeated nonempty raw URLs or
titles. Its check distributions include `ecosystem=310 Yes / 3 No / 1 Pending`,
`canonical-source=313 Yes / 1 No`, `current-or-significant=309 Yes / 5 No`, and
`unrelated-duplicate-spam=312 Yes / 1 No / 1 Pending`. It does not have a
normalized license column, so license suitability cannot be scored reliably
from this dataset alone. Examples its ecosystem check rejects include the
Blender Logo Guidelines and two Render a House web tools; a Unity Engine row is
pending. Preserve these labels as triage signals, not as proof of irrelevance.

## Audit caveats

- A first automated verification sample had an off-by-one association between
  row titles and citations. That sample is not used to claim the CSV has
  cross-wired citations; verify a record by its actual CSV row before acting.
- Counts of citations are references contained in the submitted enrichment,
  not unique studies or verified claims. The URLs in citation fields may
  repeat across records.
- ComfyUI `malware_nsfw_check=Yes` is a positive/clear check result. Do not
  interpret `Yes` as meaning the link is malicious or adult-only.
- Some records represent docs, tutorials, community accounts, hosted services,
  commercial add-ons, open movies or competing products rather than software
  that belongs in the pod image.
- This pass spot-checked a small set of primary sources with Exa; it did not
  fetch all 776 distinct catalog URLs afresh. Source pages can change, be gated
  or redirect after this snapshot.

## Repeatable check

Use Python's `csv.DictReader` with `encoding="utf-8-sig"`, count only nonempty
URLs for uniqueness, parse each `*_basis` JSON cell, and inspect canonical URL
status separately from raw links. The original inputs' SHA-256 values are:

- ComfyUI: `ae429b72eb5b2702e22cb440e3567f413c7169fda03a0caa6193d3a2263213ff`
- Blender/3D: `f2a5657c7f3a69a52b6e7787e3e03d9c083c9daad05f3f7595c02fe4de70535c`
