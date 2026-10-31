# Cross-source consistency auditing of seven MCP registries

Code and aggregate data for the paper:

> **What Model Context Protocol registry records establish: a cross-source audit of seven public registries**
> Kecai Fan, Jialiang Peng — Heilongjiang University, Harbin, China.
> Submitted to *Computers & Security* (2026).

## What this repository contains

| Path | Contents |
|---|---|
| `code/mcp_audit/` | Crawler, registry adapters, manifest parser, nine static detectors, alignment and divergence analysis, drift and re-ingestion checks, annotation tooling |
| `data/derived_tables/` | The aggregate tables behind every number printed in the paper (24 CSV files) |
| `docs/DATA.md` | Column-by-column description of each aggregate table |
| `docs/SCOPE.md` | What is deliberately **not** in this repository, and why |

## Withheld analysis assets

Two further items are withheld because they name real repositories or owners, which the paper does not print: `case_studies.py`, the script that assembles the four case studies, and the internal per-table working notes. The aggregate counterparts remain in `data/derived_tables/`.

## What this repository does not contain

The analysis depends on three artefacts that cannot be redistributed:

1. **Raw crawl snapshots** of the seven registries, eight fetch days. They contain third-party content under the registries' own terms.
2. **Cloned repository contents** (86,983 unexecuted depth-1 clones). These are third-party source code under their own licences.
3. **Per-unit records** carrying repository URLs, and the label-to-account mapping for the ten largest owners. Publishing these would attach adverse counts to identifiable accounts, which the paper states it does not do.

These are held in a restricted deposit and are available to the editor and the reviewers through the journal editorial office. `docs/SCOPE.md` states the exact boundary.

## Reproducing the analysis

Python 3.11 or later.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium      # needed only for the JavaScript-rendered registries
```

Full reproduction starts from a fresh crawl and therefore needs network access to the seven registries; the derived tables in `data/derived_tables/` reproduce every printed figure and statistic without re-crawling. The crawl is read-only: no repository code, install script or manifest hook is executed at any point, and no MCP server process is started.

## Licence

Code and aggregate data: MIT (see `LICENSE`). The third-party content described above is not included and remains under its own terms.

## Contact

Jialiang Peng — pengjialiang@hlju.edu.cn

