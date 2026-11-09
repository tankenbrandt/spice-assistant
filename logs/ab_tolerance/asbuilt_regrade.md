# Post hoc: the A/B decks graded as built (not pre-registered)

Every deck from the A/B, unchanged, through benchmark v2's as-built grader (`asbuilt.py`): values snapped to E24 resistors / E12 capacitors, series and parallel chains merged into the one part they make, ideal current sources scored 0. *merge-only* skips the rounding, isolating the resistor splitting. *structural* rounds too and keeps each deck's own transistor model; *v2* also swaps in the handout 2N2222. The A/B prompts never stated these rules. 500 builds, seed 1, gaussian draws.

Reference deck (`bench2/ref/ce_bjt_amp.cir`): merge-only 93%, structural 93%, v2 92%.

| Model | Arm | n | A/B grader | merge-only | structural | v2 | Nominal pass: A/B, merge-only, structural, v2 | Decks with merges | Rule violations |
|---|---|---|---|---|---|---|---|---|---|
| claude-opus-5-5 | A0 | 5 | 75% | 75% | 74% | 65% | 5, 5, 5, 4 of 5 | 0 | 0 |
| claude-opus-5-5 | A1 | 5 | 70% | 70% | 73% | 51% | 4, 4, 5, 3 of 5 | 0 | 0 |
| claude-opus-5-5 | A2 | 5 | 100% | 77% | 36% | 51% | 5, 4, 2, 3 of 5 | 5 | 1 |
| claude-sonnet-5-5 | A0 | 6 | 55% | 55% | 65% | 71% | 4, 4, 5, 5 of 6 | 0 | 0 |
| claude-sonnet-5-5 | A1 | 6 | 62% | 61% | 48% | 30% | 5, 5, 4, 2 of 6 | 1 | 0 |
| claude-sonnet-5-5 | A2 | 6 | 44% | 44% | 51% | 39% | 3, 3, 4, 3 of 6 | 0 | 0 |

| Model | Contrast | A/B grader | merge-only | structural | v2 |
|---|---|---|---|---|---|
| pooled | A2-A0 | +6 pts (-26 to +37) | -5 pts (-37 to +28) | -25 pts (-54 to +4) | -23 pts (-54 to +8) |
| pooled | A2-A1 | +4 pts (-28 to +35) | -6 pts (-39 to +26) | -15 pts (-46 to +16) | +5 pts (-30 to +41) |
| pooled | A1-A0 | +2 pts (-25 to +30) | +2 pts (-26 to +29) | -10 pts (-34 to +14) | -29 pts (-60 to +4) |
| claude-sonnet-5-5 | A2-A0 | -11 pts (-55 to +34) | -11 pts (-55 to +34) | -13 pts (-51 to +25) | -32 pts (-72 to +11) |
| claude-sonnet-5-5 | A2-A1 | -19 pts (-60 to +25) | -18 pts (-60 to +26) | +3 pts (-38 to +45) | +10 pts (-35 to +54) |
| claude-sonnet-5-5 | A1-A0 | +8 pts (-32 to +48) | +7 pts (-33 to +47) | -17 pts (-54 to +22) | -41 pts (-83 to +4) |
| claude-opus-5-5 | A2-A0 | +25 pts (+10 to +41) | +2 pts (-38 to +36) | -38 pts (-80 to +5) | -14 pts (-61 to +36) |
| claude-opus-5-5 | A2-A1 | +30 pts (+10 to +65) | +7 pts (-34 to +47) | -36 pts (-78 to +6) | +1 pts (-52 to +52) |
| claude-opus-5-5 | A1-A0 | -5 pts (-45 to +25) | -5 pts (-45 to +25) | -2 pts (-23 to +20) | -14 pts (-61 to +34) |
