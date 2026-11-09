# Benchmark v2 ceilings

Each circuit's reference design, graded as built, 500 Monte Carlo builds (seed 1). Admitted if the gaussian yield is at least 85%. Definitions and reasons: `bench2/suite.yaml`.

| Circuit | Parts | Nominal | Gaussian | Uniform | Admitted |
|---|---|---|---|---|---|
| rc_lowpass | precision | pass | 100.0% | 94.4% | yes |
| rc_highpass | precision | pass | 90.4% | 63.6% | yes |
| rlc_bandpass | precision + {'l': 0.05} | pass | 95.4% | 82.8% | yes |
| voltage_divider | precision, supply ±1% | pass | 100.0% | 97.0% | yes |
| halfwave_rectifier | commodity | pass | 100.0% | 100.0% | yes |
| ce_bjt_amp | commodity | pass | 92.4% | 71.4% | yes |
| inv_active_lowpass | precision | pass | 97.8% | 82.6% | yes |
| noninv_opamp | precision | pass | 100.0% | 100.0% | yes |
| inv_opamp_gbw | precision | pass | 100.0% | 100.0% | yes |
| buck_converter | commodity, supply ±1% | pass | 98.8% | 95.0% | yes |
| boost_converter | commodity, supply ±1% | pass | 98.8% | 94.0% | yes |
| bjt_diffamp | commodity | pass | 100.0% | 99.0% | yes |
