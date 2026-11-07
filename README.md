# Assertion without establishment

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23833260.svg)](https://doi.org/10.5281/zenodo.23833260)

Code and data for the paper:

> **Assertion without establishment: a cross-source evidence audit (CSEA) of Model Context Protocol registry records**
> Kecai Fan, Jialiang Peng 鈥?Heilongjiang University, Harbin, China.
> Submitted to *Computers & Security* (2026).
>
> Repository: https://github.com/FKudosC/mcp-registry-audit

## What this release contains

| Path | Contents |
|---|---|
| `code/mcp_audit/` | Crawler, registry adapters, manifest parser, nine static detectors, alignment and divergence analysis, drift and re-ingestion checks, annotation tooling |
| `code/csea/` | The audit itself: `dispatch.py` (verdicts and evidence), `ablation.py`, `analyze_answers.py`, `invariants.py`, `build_v8.py`, and the two reproduction scripts for the drift and install-path figures |
| `code/figures/` | R scripts behind Figures 1 to 7, including `make_figures.R`, `make_framework_fig.R` and `make_drift_fig.R` |
| `data/` | Aggregate outputs: `summary.json`, `ablation.json`, `validation.json`, `invariants.json`, the readability companions `summary.md`, `ablation.md` and `invariants.md`, and the two supplement data files |
| `data/derived_tables/` | The 24 aggregate tables behind every number printed in the paper |
| `data/d_kappa.csv` | Pairwise agreement in the first annotation round, the source of Table S15 |
| `docs/DATA.md` | Column-by-column description of each aggregate table |
| `docs/SCOPE.md` | What is deliberately **not** in this release, and why |
| `CHECKSUMS.sha256` | SHA-256 of every file in this release |


## What is withheld

Three classes of material are not part of this release. `docs/SCOPE.md` states the boundary and the route to them.

1. **Raw crawl snapshots** of the seven registries across eight fetch days.
2. **Cloned repository contents**, 86,983 unexecuted depth-1 clones.
3. **Per-unit records** that carry a normalised repository URL, together with the label-to-account mapping for the ten largest owners.

None of the three is part of this release. They remain in a restricted deposit described in `docs/SCOPE.md`, and the aggregate tables reproduce every printed number without them.

## Reproducing the analysis

Python 3.11 or later, and R 4.6 for the figures.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium      # only for the JavaScript-rendered registries
```

The eleven commands of the paper's reproducibility section run in this order.

```bash
python -m mcp_audit c1-reclone                        # needs network
python mcp_audit/csea/dispatch.py
python mcp_audit/csea/ablation.py
python mcp_audit/csea/analyze_answers.py
python mcp_audit/csea/invariants.py                   # must exit 0
python paper/csea/out/repro_new_numbers.py --db <corpus>
python paper/csea/out/repro_install_path.py
python paper/csea/out/export_drift_fig_data.py --db <corpus>
Rscript paper/csea/figures/make_figures.R
Rscript paper/csea/figures/make_framework_fig.R
Rscript paper/csea/figures/make_drift_fig.R
```

The crawl is read-only. No repository code, install script or manifest hook is executed at any point, and no MCP server process is started.

## Citation

If you use this code or these tables, please cite the archived release:

> Fan K, Peng J. Assertion without establishment: a cross-source evidence audit of Model Context Protocol registry records. Zenodo; 2026.
> doi:10.5281/zenodo.23833260 (version V1.0.0; concept DOI 10.5281/zenodo.23833259)

## Licence

Code and aggregate data: MIT (see `LICENSE`). The third-party content described above is not included and remains under its own terms.

## Contact

Jialiang Peng 鈥?pengjialiang@hlju.edu.cn

