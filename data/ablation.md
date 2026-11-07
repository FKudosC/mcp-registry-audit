# §7.1 Channel ablation

Units: 22,104   (verdict recomputed under each ablation)

## Verdict distribution by arm

| arm | SUPPORTED | PARTIAL | UNSUPPORTED | CONFLICT | STALE | INSUFFICIENT | UNRESOLVED | abstained |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| complete chain | 2.8% | 14.5% | 24.1% | 0.0% | 0.0% | 58.1% | 0.5% | 58.6% |
| every artefact assumed to carry the declaring field | 2.8% | 14.5% | 51.2% | 0.0% | 0.0% | 31.0% | 0.5% | 31.5% |
| declarations from the official registry only | 9.4% | 18.3% | 0.7% | 0.0% | 0.0% | 71.0% | 0.5% | 71.5% |
| largest single declaration set per unit (post-hoc best) | 9.5% | 18.7% | 0.8% | 0.0% | 0.0% | 70.6% | 0.5% | 71.1% |

## What each channel decides

| arm removed | verdicts changed | share | most common shift |
|---|---:|---:|---|
| every artefact assumed to carry the declaring field | 5,993 | 27.1% | INSUFFICIENT → UNSUPPORTED (5,993) |
| declarations from the official registry only | 5,644 | 25.5% | UNSUPPORTED → INSUFFICIENT (2,783) |
| largest single declaration set per unit (post-hoc best) | 5,565 | 25.2% | UNSUPPORTED → INSUFFICIENT (2,760) |

## Coverage of a single registry

| source | units it can address | share of corpus | SUPPORTED | PARTIAL | UNSUPPORTED | INSUFFICIENT |
|---|---:|---:|---:|---:|---:|---:|
| glama | 19,577 | 88.6% | 3.1% | 16.0% | 0.2% | 80.2% |
| official | 13,582 | 61.4% | 15.3% | 29.8% | 1.2% | 53.6% |
| mcpso | 9,969 | 45.1% | 1.1% | 7.2% | 0.1% | 90.7% |
| aibase | 4,235 | 19.2% | 0.3% | 1.4% | 0.0% | 97.3% |
| cursor | 1,791 | 8.1% | 1.2% | 6.9% | 0.2% | 88.2% |
| smithery | 341 | 1.5% | 6.2% | 41.6% | 0.6% | 51.0% |
| modelscope | 189 | 0.9% | 0.0% | 3.7% | 0.5% | 93.7% |
