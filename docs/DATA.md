# Aggregate tables

Every file is derived from the crawl of 3 to 21 September 2026 and backs a number printed in the paper. All are aggregate: no repository URL, owner name or per-unit record appears in this directory.

| File | Rows | Columns |
|---|---|---|
| `d_registry.csv` | 7 | `registry, entries, indexed_repos, clone_ok, clone_total` |
| `d_funnel.csv` | — | `stage, value` — 158,631 records to 100,688 listings |
| `d_fetchday.csv` | 8 | `day, snapshots, same_day_units, comparable, different, rate_pct` |
| `d_cap_all.csv` | — | `cap, comparable, different, rate_pct` — divergence by capability class, full corpus |
| `d_cap_same.csv` | — | same, same-day comparable units only |
| `d_tau_all.csv` | — | `tau, different, comparable, rate_pct` — similarity-threshold sweep |
| `d_tau_same.csv` | — | same, same-day units |
| `d_bands.csv` | — | `band, units` — the five divergence bands |
| `d_visibility.csv` | — | `field, ge1, ge2, v_pct, miss_pct` — declaration visibility per field |
| `d_visibility2.csv` | — | adds `carriers` |
| `d_visibility_k.csv` | — | `k, units, pct` |
| `d_overgrant.csv` | — | `category, units` |
| `d_overgrant2.csv` | — | `category, declarable, units, mp_units, undeclared, share_pct` |
| `d_blame.csv` | 7 | `registry, underdisclosed, with_code, rate_pct` |
| `d_blame2.csv` | — | `registry, with_code, numerator, unit_level, cross_disagreement` |
| `d_singletable.csv` | 7 | `registry, covered, missed, miss_rate_pct` |
| `d_danger.csv` | — | `category, repos, pct` — static surface flags |
| `d_drift_out.csv` | — | `outcome, units, share_all` — 5,069 moved, 9,388 unchanged, 340 fail-closed |
| `d_reingest.csv` | — | `setting, indexed, cross_units, comparable, different, rate_pct, desc_single_pct` |
| `d_validity.csv` | — | `auto, gold, units` — agreement matrix |
| `d_kappa.csv` | — | `group, pair, kappa, lo, hi` |
| `d_span.csv` | — | `span, units, comparable, different, rate_pct` |
| `d_span2.csv` | — | `label, group, comparable, divergent, rate_pct` |
| `d_zeroshot.csv` | — | `setting, cross_units, comparable, different, rate_pct, desc_single_pct` |

Percentages are printed to one decimal place in the paper; the tables carry the unrounded values used for them.

## Files outside derived_tables

| File | Contents |
|---|---|
| summary.json | Totals behind Tables 1 and 5 |
| blation.json | The four ablation arms behind Tables S10 and S11 |
| alidation.json | Agreement statistics behind Tables S13 and S14 |
| invariants.json | Output of the four invariants, which must reproduce on any run |
| d_kappa.csv | Pairwise kappa in the first annotation round, the source of Table S15 |
| ig7_data.json | The two panels of Figure 7 |
| c1_reclone_probe.json | Probe outcomes behind the drift census |

Per-unit files, the corpus database and the annotation labels are in 
estricted/ and are described in SCOPE.md.
