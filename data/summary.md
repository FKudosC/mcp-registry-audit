# CSEA dispatch — first pass

Units: 22,104   (read-only on the frozen corpus)

## DECLARATION_COMPLETE (n = 22,104)

| status | n | share |
|---|---:|---:|
| SUPPORTED | 623 | 2.8% |
| PARTIAL | 3,198 | 14.5% |
| UNSUPPORTED | 5,335 | 24.1% |
| INSUFFICIENT | 12,835 | 58.1% |
| UNRESOLVED | 113 | 0.5% |

| verdict | n | share of claim |
|---|---:|---:|
| (INSUFFICIENT, CHANNEL_ABSENT) | 5,993 | 27.1% |
| (UNSUPPORTED, UNDECLARED_CAPABILITY) | 5,335 | 24.1% |
| (PARTIAL, DECLARATION_SUBSET) | 3,198 | 14.5% |
| (INSUFFICIENT, CODE_OPAQUE) | 2,964 | 13.4% |
| (INSUFFICIENT, CODE_UNAVAILABLE) | 1,210 | 5.5% |
| (INSUFFICIENT, CODE_GONE) | 1,015 | 4.6% |
| (INSUFFICIENT, NO_DECLARATION) | 912 | 4.1% |
| (INSUFFICIENT, NO_DECLARING_CHANNEL) | 741 | 3.4% |
| (SUPPORTED, FULL_MATCH) | 623 | 2.8% |
| (UNRESOLVED, IDENTITY_AMBIGUOUS) | 113 | 0.5% |

## DECLARATION_SOUND (n = 22,104)

| status | n | share |
|---|---:|---:|
| SUPPORTED | 2,311 | 10.5% |
| PARTIAL | 1,510 | 6.8% |
| UNSUPPORTED | 112 | 0.5% |
| INSUFFICIENT | 18,058 | 81.7% |
| UNRESOLVED | 113 | 0.5% |

| verdict | n | share of claim |
|---|---:|---:|
| (INSUFFICIENT, NO_DECLARATION) | 13,858 | 62.7% |
| (SUPPORTED, FULL_MATCH) | 2,311 | 10.5% |
| (INSUFFICIENT, CODE_OPAQUE) | 1,975 | 8.9% |
| (PARTIAL, GHOST_DECLARATION) | 1,510 | 6.8% |
| (INSUFFICIENT, CODE_UNAVAILABLE) | 1,210 | 5.5% |
| (INSUFFICIENT, CODE_GONE) | 1,015 | 4.6% |
| (UNRESOLVED, IDENTITY_AMBIGUOUS) | 113 | 0.5% |
| (UNSUPPORTED, GHOST_DECLARATION) | 112 | 0.5% |

## VERSION_PIN (n = 12,149)

| status | n | share |
|---|---:|---:|
| SUPPORTED | 7,708 | 63.4% |
| STALE | 4,140 | 34.1% |
| INSUFFICIENT | 301 | 2.5% |

| verdict | n | share of claim |
|---|---:|---:|
| (SUPPORTED, FULL_MATCH) | 7,708 | 63.4% |
| (STALE, UNPINNED_VERSION) | 4,140 | 34.1% |
| (INSUFFICIENT, CODE_GONE) | 301 | 2.5% |

## Record level

The record verdict is the primary claim (declaration completeness). Soundness is reported beside it, and currency beside both: staleness is a property of the binding, not of the declaration, so it is never merged in.

| record verdict | n | share |
|---|---:|---:|
| SUPPORTED | 623 | 2.8% |
| PARTIAL | 3,198 | 14.5% |
| UNSUPPORTED | 5,335 | 24.1% |
| INSUFFICIENT | 12,835 | 58.1% |
| UNRESOLVED | 113 | 0.5% |

| currency | n | share |
|---|---:|---:|
| FRESH | 7,708 | 34.9% |
| STALE | 4,140 | 18.7% |
| UNKNOWN | 10,256 | 46.4% |

### verdict x currency

| verdict | FRESH | STALE | UNKNOWN | n |
|---|---:|---:|---:|---:|
| SUPPORTED | 442 | 169 | 12 | 623 |
| PARTIAL | 1915 | 1188 | 95 | 3,198 |
| UNSUPPORTED | 2697 | 2227 | 411 | 5,335 |
| INSUFFICIENT | 2645 | 550 | 9640 | 12,835 |
| UNRESOLVED | 9 | 6 | 98 | 113 |

## Decomposition of the omission — class-weighted

| bucket | class-instances | share |
|---|---:|---:|
| schema | 26,023 | 50.4% |
| artefact | 11,833 | 22.9% |
| behavioural | 13,816 | 26.7% |
| total | 51,672 | 100% |

`S_schema` = 0.5036   `S_structural` = 0.7326

## Decomposition — unit-weighted (units with an omission)

| bucket | units | share |
|---|---:|---:|
| omission entirely schema/artefact | 7,834 | 35.4% |
| some behavioural share | 6,221 | 28.1% |
| every omission is behavioural | 2,325 | 10.5% |
| nothing omitted | 5,724 | 25.9% |

## Sensitivity: `CHANNEL_ABSENT` read as refutation

| status | abstain (primary) | refute | difference |
|---|---:|---:|---:|
| SUPPORTED | 2.8% | 2.8% | +0 |
| PARTIAL | 14.5% | 14.5% | +0 |
| UNSUPPORTED | 24.1% | 51.2% | -5,993 |
| INSUFFICIENT | 58.1% | 31.0% | +5,993 |
| UNRESOLVED | 0.5% | 0.5% | +0 |

Read as refutation, the abstention rate falls and the refutation rate carries the format choice. The paper reports the primary reading and this table as the bound on it.

## E1b: per-source omission rate, naive vs stratified

| source | units | naive | stratified | vs median |
|---|---:|---:|---:|---|
| glama | 14,531 | 0.839 | 0.847 | +0.027 (within) |
| official | 9,785 | 0.552 | 0.606 | -0.214 (below) |
| mcpso | 6,855 | 0.925 | 0.840 | +0.019 (within) |
| aibase | 2,747 | 0.983 | 0.817 | -0.004 (within) |
| cursor | 1,254 | 0.926 | 0.821 | +0.000 (within) |
| smithery | 291 | 0.611 | 0.559 | -0.262 (below) |
| modelscope | 153 | 0.971 | 0.900 | +0.080 (above) |

Stratification on (channel present, number of declarable classes detected), 6 strata.

- naive spread: 0.552 to 0.983 (width 0.431)
- stratified spread: 0.559 to 0.900 (width 0.342)

Stratified median 0.821. Below the band by more than 0.05: official, smithery.

A spread that survives stratification is not a composition artefact. What survives here is the set of sources that carry a declaring field of their own, which is Corollary 1 rather than the strong form of Proposition 1.

## Crosswalk: old similarity band x new status (DECLARATION_COMPLETE)

| band | SUPPORTED | PARTIAL | UNSUPPORTED | CONFLICT | STALE | INSUFFICIENT | UNRESOLVED | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| different | 74 | 487 | 676 | 0 | 0 | 4265 | 71 | 5,573 |
| identical | 5 | 19 | 16 | 0 | 0 | 161 | 3 | 204 |
| insufficient | 505 | 2479 | 4360 | 0 | 0 | 7798 | 34 | 15,176 |
| near | 8 | 29 | 29 | 0 | 0 | 95 | 1 | 162 |
| partial | 31 | 184 | 254 | 0 | 0 | 516 | 4 | 989 |

## Per-source status share (DECLARATION_COMPLETE, naive)

| source | n | SUPPORTED | PARTIAL | UNSUPPORTED | CONFLICT | STALE | INSUFFICIENT | UNRESOLVED |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| glama | 19,577 | 3.1% | 16.0% | 26.7% | 0.0% | 0.0% | 53.6% | 0.5% |
| official | 13,582 | 4.5% | 23.0% | 38.3% | 0.0% | 0.0% | 34.1% | 0.1% |
| mcpso | 9,969 | 1.1% | 7.2% | 10.0% | 0.0% | 0.0% | 80.8% | 0.9% |
| aibase | 4,235 | 0.3% | 1.4% | 1.7% | 0.0% | 0.0% | 95.6% | 1.1% |
| cursor | 1,791 | 1.2% | 6.9% | 8.2% | 0.0% | 0.0% | 80.2% | 3.5% |
| smithery | 341 | 1.2% | 5.3% | 41.9% | 0.0% | 0.0% | 51.0% | 0.6% |
| modelscope | 189 | 0.0% | 3.7% | 6.9% | 0.0% | 0.0% | 87.3% | 2.1% |

## Diagnostic: how many sources declare a capability set

| declaring sources | units |
|---:|---:|
| 0 | 12,822 |
| 1 | 5,156 |
| 2 | 3,323 |
| 3 | 670 |
| 4 | 118 |
| 5 | 12 |
| 6 | 3 |

`CONFLICT` can only be populated where two non-empty declarations disagree. The histogram is that precondition; the measured count is zero.
