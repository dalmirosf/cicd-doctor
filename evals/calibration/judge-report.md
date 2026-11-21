# Cause judge calibration

Judge `gpt-6.2-sol`, prompt `judge-v1`.

**Calibrated** (bars: exact >= 75%, weighted kappa >= 0.6, no 0-against-2).

- Labelled pairs: 20 (judge failed on 0)
- Exact agreement: 90.0% (95% interval 0.70 to 0.97; wide: n is small)
- Quadratic-weighted kappa: 0.9091
- Gross disagreements: none

Confusion matrix (rows: human, columns: judge):

| human \ judge | 0 | 1 | 2 |
|---|---|---|---|
| 0 | 3 | 0 | 0 |
| 1 | 0 | 2 | 0 |
| 2 | 0 | 2 | 13 |

Reserve pairs (5): exact 0.8, kappa 0.5455.
