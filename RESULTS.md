# Streaming Quantile Estimation - Verification Results

Library: `gk_quantile.py` (Greenwald-Khanna, stdlib-only). Every bound below is **deterministic** and computed from the summary state itself (`rank_error_bound()`), then checked against exact ranks in the full data.

- Target epsilon: `0.01` (relative rank error)
- Stream size N: `200000` per distribution
- Reproduce: `python3 benchmark.py` (tests: `python3 -m unittest test_gk_quantile -v`)

## 1. Error vs. reported bound (exact full-data comparison)

### uniform

| q | estimate | reported bound (ranks) | reported bound (rel) | actual rank error | actual (rel) | within bound |
|---|---|---|---|---|---|---|
| 0.500 | 0.500579 | 1978 | 0.9890% | 63 | 0.0315% | yes |
| 0.900 | 0.896691 | 1978 | 0.9890% | 679 | 0.3395% | yes |
| 0.950 | 0.949022 | 1978 | 0.9890% | 297 | 0.1485% | yes |
| 0.990 | 0.988204 | 1978 | 0.9890% | 402 | 0.2010% | yes |
| 0.999 | 0.995215 | 1978 | 0.9890% | 755 | 0.3775% | yes |

summary tuples: `284`, effective epsilon: `0.00989`

### exponential

| q | estimate | reported bound (ranks) | reported bound (rel) | actual rank error | actual (rel) | within bound |
|---|---|---|---|---|---|---|
| 0.500 | 0.690434 | 1958 | 0.9790% | 285 | 0.1425% | yes |
| 0.900 | 2.28967 | 1958 | 0.9790% | 245 | 0.1225% | yes |
| 0.950 | 2.92315 | 1958 | 0.9790% | 813 | 0.4065% | yes |
| 0.990 | 4.57017 | 1958 | 0.9790% | 93 | 0.0465% | yes |
| 0.999 | 5.65693 | 1958 | 0.9790% | 501 | 0.2505% | yes |

summary tuples: `284`, effective epsilon: `0.00979`

### bimodal

| q | estimate | reported bound (ranks) | reported bound (rel) | actual rank error | actual (rel) | within bound |
|---|---|---|---|---|---|---|
| 0.500 | 2.40094 | 1988 | 0.9940% | 489 | 0.2445% | yes |
| 0.900 | 11.6983 | 1988 | 0.9940% | 194 | 0.0970% | yes |
| 0.950 | 12.5622 | 1988 | 0.9940% | 159 | 0.0795% | yes |
| 0.990 | 14.1326 | 1988 | 0.9940% | 74 | 0.0370% | yes |
| 0.999 | 15.1994 | 1988 | 0.9940% | 246 | 0.1230% | yes |

summary tuples: `290`, effective epsilon: `0.00994`

### duplicates

| q | estimate | reported bound (ranks) | reported bound (rel) | actual rank error | actual (rel) | within bound |
|---|---|---|---|---|---|---|
| 0.500 | 42 | 1874 | 0.9370% | 0 | 0.0000% | yes |
| 0.900 | 42 | 1874 | 0.9370% | 0 | 0.0000% | yes |
| 0.950 | 42 | 1874 | 0.9370% | 0 | 0.0000% | yes |
| 0.990 | 42 | 1874 | 0.9370% | 0 | 0.0000% | yes |
| 0.999 | 42 | 1874 | 0.9370% | 0 | 0.0000% | yes |

summary tuples: `282`, effective epsilon: `0.00937`

## 2. Merge consistency

Two half-streams merged (`merged`) vs. one pass over the full stream (`direct`). `rank gap` = distance between the two estimates' exact rank intervals; must be <= sum of both bounds.

| q | est (direct) | est (merged) | rank gap | bound direct | bound merged | bound ratio | gap <= sum |
|---|---|---|---|---|---|---|---|
| 0.500 | 0.563223 | 0.566603 | 493 | 1999 | 1422 | 0.71x | yes |
| 0.900 | 1.60663 | 1.59413 | 252 | 1999 | 1422 | 0.71x | yes |
| 0.950 | 2.27496 | 2.27496 | 0 | 1999 | 1422 | 0.71x | yes |
| 0.990 | 3.75981 | 3.87572 | 243 | 1999 | 1422 | 0.71x | yes |
| 0.999 | 5.07673 | 5.07673 | 0 | 1999 | 1422 | 0.71x | yes |

Merged bound / direct bound = `0.71` (same order of magnitude).

## 3. Memory usage

Peak Python allocation while ingesting the stream (tracemalloc), summary already warm-up excluded; full-data column = size of the materialized `list` of floats kept for exact comparison.

| N | tuples kept | summary peak | full data (list) | reduction |
|---|---|---|---|---|
| 10000 | 328 | 47.6 KiB | 0.3 MiB | 7x |
| 100000 | 305 | 56.3 KiB | 3.1 MiB | 56x |
| 1000000 | 289 | 57.8 KiB | 30.9 MiB | 548x |

Summary size grows as `O((1/eps) log(eps N))`, independent of the data volume in practice.

## 4. Memory budget and explicit degradation

| max_size | tuples | degraded | reported bound (rel) | max actual err (rel) | bound valid |
|---|---|---|---|---|---|
| none | 299 | no | 0.9900% | 0.3075% | yes |
| 256 | 159 | yes | 2.2495% | 0.4060% | yes |
| 64 | 63 | yes | 7.5935% | 2.4380% | yes |
| 16 | 12 | yes | 38.4435% | 13.0760% | yes |
| 8 | 6 | yes | 86.4975% | 39.5840% | yes |

With a tiny budget the summary cannot keep the requested epsilon: it says so (`degraded = True`) and reports a larger, still-valid bound instead of silently lying.

## 5. Edge cases

| case | result |
|---|---|
| empty stream | `quantile(0.5) -> None`, bound = 0 |
| single element | `quantile(0.99) -> 7.5` |
| all identical (50k x 3.14) | `quantile(0.5) -> 3.14`, tuples = 258 |
| tiny budget (8 tuples, skewed) | degraded = True, reported bound = 86.4970% of N, bound still valid = yes

