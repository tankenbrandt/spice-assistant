# A/B: does telling the model how it is graded change yield?

Circuit `ce_bjt_amp`; 500 Monte Carlo builds per deck, seed 1. API spend $1.82 of a $2.00 cap. Protocol: the docstring of `ab_tolerance.py`.

Ceiling (hand-recentred reference deck): gaussian 92%, uniform 73%.

## Yield, gaussian draws (primary)

| Scope | Arm | n | Nominal pass | Mean yield (95% CI) | Of ceiling | Yield ≥ 90% | >6 resistors |
|---|---|---|---|---|---|---|---|
| pooled | A0 | 11 | 9/11 | 64% (43%–81%) | 69% | 9% | 4 |
| pooled | A1 | 11 | 9/11 | 66% (44%–84%) | 71% | 18% | 2 |
| pooled | A2 | 11 | 8/11 | 69% (43%–93%) | 75% | 55% | 7 |
| claude-sonnet-5-5 | A0 | 6 | 4/6 | 55% (25%–83%) | 59% | 0% | 2 |
| claude-sonnet-5-5 | A1 | 6 | 5/6 | 62% (35%–84%) | 68% | 17% | 2 |
| claude-sonnet-5-5 | A2 | 6 | 3/6 | 44% (14%–74%) | 47% | 17% | 2 |
| claude-opus-5-5 | A0 | 5 | 5/5 | 75% (59%–90%) | 81% | 20% | 2 |
| claude-opus-5-5 | A1 | 5 | 4/5 | 70% (35%–90%) | 76% | 20% | 0 |
| claude-opus-5-5 | A2 | 5 | 5/5 | 100% (100%–100%) | 108% | 100% | 5 |

| Scope | Contrast | Difference (95% CI) |
|---|---|---|
| pooled | A2-A0 | +6 pts (-26 to +37) |
| pooled | A1-A0 | +2 pts (-25 to +30) |
| pooled | A2-A1 | +4 pts (-28 to +35) |
| claude-sonnet-5-5 | A2-A0 | -11 pts (-55 to +34) |
| claude-sonnet-5-5 | A1-A0 | +8 pts (-32 to +48) |
| claude-sonnet-5-5 | A2-A1 | -19 pts (-60 to +25) |
| claude-opus-5-5 | A2-A0 | +25 pts (+10 to +41) |
| claude-opus-5-5 | A1-A0 | -5 pts (-45 to +25) |
| claude-opus-5-5 | A2-A1 | +30 pts (+10 to +65) |

## Yield, uniform draws

| Scope | Arm | n | Nominal pass | Mean yield (95% CI) | Of ceiling | Yield ≥ 90% | >6 resistors |
|---|---|---|---|---|---|---|---|
| pooled | A0 | 11 | 9/11 | 53% (36%–67%) | 73% | 0% | 4 |
| pooled | A1 | 11 | 9/11 | 54% (37%–68%) | 73% | 0% | 2 |
| pooled | A2 | 11 | 8/11 | 63% (39%–85%) | 86% | 45% | 7 |
| claude-sonnet-5-5 | A0 | 6 | 4/6 | 45% (21%–68%) | 61% | 0% | 2 |
| claude-sonnet-5-5 | A1 | 6 | 5/6 | 52% (30%–69%) | 71% | 0% | 2 |
| claude-sonnet-5-5 | A2 | 6 | 3/6 | 35% (12%–60%) | 48% | 0% | 2 |
| claude-opus-5-5 | A0 | 5 | 5/5 | 63% (53%–74%) | 86% | 0% | 2 |
| claude-opus-5-5 | A1 | 5 | 4/5 | 56% (28%–71%) | 76% | 0% | 0 |
| claude-opus-5-5 | A2 | 5 | 5/5 | 96% (94%–98%) | 131% | 100% | 5 |

| Scope | Contrast | Difference (95% CI) |
|---|---|---|
| pooled | A2-A0 | +10 pts (-19 to +38) |
| pooled | A1-A0 | +1 pts (-22 to +23) |
| pooled | A2-A1 | +9 pts (-19 to +37) |
| claude-sonnet-5-5 | A2-A0 | -9 pts (-45 to +26) |
| claude-sonnet-5-5 | A1-A0 | +7 pts (-25 to +40) |
| claude-sonnet-5-5 | A2-A1 | -17 pts (-51 to +19) |
| claude-opus-5-5 | A2-A0 | +33 pts (+22 to +45) |
| claude-opus-5-5 | A1-A0 | -7 pts (-39 to +15) |
| claude-opus-5-5 | A2-A1 | +41 pts (+25 to +69) |

## Post hoc: same-value resistors drift together (not pre-registered)

Reference deck: gaussian 91%, uniform 71%.

| Model | Arm | n | Gaussian: independent → same-reel | Uniform: independent → same-reel | Decks with ideal current sources |
|---|---|---|---|---|---|
| claude-opus-5-5 | A0 | 5 | 75% → 74% | 63% → 62% | 0 |
| claude-opus-5-5 | A1 | 5 | 70% → 69% | 56% → 56% | 0 |
| claude-opus-5-5 | A2 | 5 | 100% → 97% | 96% → 76% | 1 |
| claude-sonnet-5-5 | A0 | 6 | 55% → 55% | 45% → 43% | 0 |
| claude-sonnet-5-5 | A1 | 6 | 62% → 62% | 52% → 51% | 0 |
| claude-sonnet-5-5 | A2 | 6 | 44% → 44% | 35% → 35% | 0 |
