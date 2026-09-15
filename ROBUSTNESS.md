# Robustness Signoff Report

- **Generated:** 2026-09-15T18:17:40+00:00
- **ngspice:** `C:\Users\tommy\Somerset Systems\SPICE-assistant\tools\Spice64\bin\ngspice_con.exe`
- **Monte Carlo:** 500 samples/deck, gaussian (3σ at the tolerance limit), seed 1
- **Tolerances:** R ±5%, C ±10%, L ±10%, supply ±5%, BJT β ±50%, T ∈ [-40, 85] °C
- **API cost: $0.00** — this entire layer is ngspice only.

## What this measures

`speccheck.py` asks *is this deck correct at nominal values, 27 °C, exact supply?*
This layer asks the question a manufacturer asks: **how often is it correct**
once the resistors are 5% parts, the supply is ±5%, the transistor β is anywhere
in its data-sheet band, and the board runs from −40 to +85 °C.

Each deck gets: Monte Carlo yield, per-parameter sensitivity (normalized,
`%` change in the spec per `%` change in the part), a sensitivity-directed
worst-case corner, and a deterministic temperature × supply × β grid.

## Headline: nominal correctness vs. manufacturable correctness

| Circuit | Spec | Nominal before | Nominal after | **Yield before** | **Yield after** |
|---------|------|----------------|---------------|------------------|-----------------|
| `ce_bjt_amp` | \|gain\| = 50.000 [47.500, 52.500] | 107.312 | 51.527 | 1% | 69% |
| `halfwave_rectifier` | peak drop = — [—, 1.000] | 1.072 | 0.599 | 0% | 100% |
| `buck_converter` | V(out) avg = 5.000 [4.850, 5.150] | 4.519 | 5.085 | 0% | 77% |
| `boost_converter` | V(out) avg = 12.000 [11.640, 12.360] | 11.129 | 11.766 | 0% | 76% |
| `bjt_diffamp` | Ad = 25.000 [22.500, 27.500] | 0.000 | 24.810 | 0% | 47% |
| `ce_amp_bias_and_gain` | \|gain\| = 80.000 [72.000, 88.000] | 77.309 | 75.675 | 0% | 53% |

*Yield = share of Monte Carlo samples meeting **every** spec on the circuit.*

## ce_bjt_amp

**Repair:** Re fully bypassed (gain set by intrinsic re) -> split Re: 56 unbypassed + 910 bypassed.

### `gain` — \|gain\| target 50.000 V/V, limits [47.500, 52.500] ← headline

| Deck | Nominal | MC mean | σ | CV | min … max | in-spec | Cpk |
|------|---------|---------|---|----|-----------|---------|-----|
| before (`ce_bjt_amp.cir`) | 107.312 | 100.299 | 23.948 | 23.9% | 41.795 … 162.726 | 1% | -0.67 |
| after (`ce_bjt_amp.cir`) | 51.527 | 51.696 | 1.515 | 2.9% | 47.155 … 56.080 | 69% | 0.18 |

**Distribution — before (gain):**

```
     41.79 | xxx 4
     47.84 | xxxxxx 7
     53.89 | xxxxxxxxxx 12
     59.93 | xxxxxxxxxxxxxxxxxxxx 25
     65.98 | xxxxxxxxxxxxxxxxxxxxxxxx 29
     72.03 | xxxxxxxxxxxxxxxxxxxxxxxxxxxx 34
     78.07 | xxxxxxxxxxxxxxxxxxxx 25
     84.12 | xxxxxxxxxxxxxxxxxxxxxxxxx 31
     90.17 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 37
     96.21 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 43
     102.3 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 48
     108.3 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 49
     114.4 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 43
     120.4 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 37
     126.4 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxx 35
     132.5 | xxxxxxxxxxxxxxxxx 21
     138.5 | xxxxxxxxxxx 14
     144.6 | xxx 4
     150.6 | x 1
     156.7 | x 1
           (# = inside spec, x = outside; LSL=47.5, USL=52.5)
```

**Distribution — after (gain):**

```
     47.15 | x 1
      47.6   # 1
     48.05   # 2
     48.49   ###### 10
     48.94   ####### 12
     49.39   ############### 24
     49.83   ######################### 40
     50.28   ############################### 51
     50.72   ############################### 51
     51.17   ############################### 50
     51.62   ######################################## 65
     52.06 | xxxxxxxxxxxxxxxxxxxxxxxxx 41
     52.51 | xxxxxxxxxxxxxxxxxxxxxxxxxx 43
     52.96 | xxxxxxxxxxxxxxxxxxxxxxxxxx 43
      53.4 | xxxxxxxxxxxxxxxxx 27
     53.85 | xxxxxxx 12
      54.3 | xxxxxxx 12
     54.74 | xxxxx 8
     55.19 | xxx 5
     55.63 | x 2
           (# = inside spec, x = outside; LSL=47.5, USL=52.5)
```

### Sensitivity ranking (gain)

`S` = % change in the metric per 1% change in the parameter (temperature: % per 10 °C). `span` = total % swing of the metric across that parameter's full tolerance.

| Rank | Parameter | Kind | Nominal | Tol | S (before) | span (before) | S (after) | span (after) |
|------|-----------|------|---------|-----|------------|---------------|-----------|--------------|
| 1 | `QN2222.BF` | model | 200.0000 | ±50% | 0.436 | +43.6% | 0.025 | +2.5% |
| 2 | `Vcc` | supply | 12.0000 | ±5% | 3.565 | +35.6% | 0.490 | +4.9% |
| 3 | `R1` | passive | 2.2e+05 | ±5% | -3.285 | -32.9% | -0.419 | -4.2% |
| 4 | `R2` | passive | 16000.0000 | ±5% | 3.021 | +30.2% | 0.406 | +4.1% |
| 5 | `TEMP` | temp | 27 °C | — | 4.815 | +9.6% | -0.551 | -1.1% |
| 6 | `QN2222.IS` | model | 1.434e-14 | ±30% | 0.115 | +6.9% | 0.008 | +0.5% |
| 7 | `Rc` | passive | 5000.0000 | ±5% | 0.621 | +6.2% | 0.561 | +5.6% |
| 8 | `Re` | passive | 100.0000 | ±5% | -0.430 | -4.3% | — | — |

### Worst-case corner and PVT grid

| Deck | WC low | WC high | PVT min | PVT max | PVT corners in spec |
|------|--------|---------|---------|---------|---------------------|
| before | 4.010 | 19.329 | 28.001 | 169.627 | 0/27 |
| after | 41.217 | 63.473 | 47.869 | 55.366 | 18/27 |

## halfwave_rectifier

**Repair:** Diode emission coefficient N=1.5 -> N=1 (physical silicon).

### `drop` — peak drop target — V, limits [—, 1.000] ← headline

| Deck | Nominal | MC mean | σ | CV | min … max | in-spec | Cpk |
|------|---------|---------|---|----|-----------|---------|-----|
| before (`halfwave_rectifier.cir`) | 1.072 | 1.074 | 0.024 | 2.3% | 0.997 … 1.130 | 0% | -1.00 |
| after (`halfwave_rectifier.cir`) | 0.599 | 0.606 | 0.072 | 11.9% | 0.470 … 0.740 | 100% | 1.83 |

### `vmin` — min V(out) target — V, limits [-0.100, —]

| Deck | Nominal | MC mean | σ | CV | min … max | in-spec | Cpk |
|------|---------|---------|---|----|-----------|---------|-----|
| before (`halfwave_rectifier.cir`) | -1e-08 | -1.01e-08 | 3.56e-10 | 3.5% | -1.19e-08 … -9.51e-09 | 100% | 93690434.77 |
| after (`halfwave_rectifier.cir`) | -1.1e-08 | -1.38e-07 | 3.12e-07 | 225.9% | -1.94e-06 … -9.51e-09 | 100% | 106873.46 |

**Distribution — before (drop):**

```
    0.9969 | x 2
     1.004 | xxxx 6
      1.01 | x 2
     1.017 | xxxxxx 9
     1.024 | xxxxxx 10
      1.03 | xxxxxx 9
     1.037 | xxxxxxxxxxx 17
     1.043 | xxxxxxxxxxxxxxx 24
      1.05 | xxxxxxxxxxxxxxxxxxxxxxx 36
     1.057 | xxxxxxxxxxxxxxxxxxxxxxxxx 38
     1.063 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 56
      1.07 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 48
     1.077 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 62
     1.083 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 56
      1.09 | xxxxxxxxxxxxxxxxxxxxxxxxxx 40
     1.097 | xxxxxxxxxxxxxxxxxxxxxxx 35
     1.103 | xxxxxxxxxxxxxxxx 25
      1.11 | xxxxxxx 11
     1.117 | xxxxx 7
     1.123 | xxxxx 7
           (# = inside spec, x = outside; USL=1)
```

**Distribution — after (drop):**

```
    0.4696   ################### 31
    0.4962   ################################## 56
    0.5227   ############################### 51
    0.5492   ######################### 40
    0.5757   ######################################## 65
    0.6022   ##################################### 60
    0.6287   ############################### 51
    0.6553   ################################## 55
    0.6818   ########################### 44
    0.7083   ############################ 45
    0.7348   # 2
    0.7613   
    0.7879   
    0.8144   
    0.8409   
    0.8674   
    0.8939   
    0.9204   
     0.947   
    0.9735   
           (# = inside spec, x = outside; USL=1)
```

### Sensitivity ranking (drop)

`S` = % change in the metric per 1% change in the parameter (temperature: % per 10 °C). `span` = total % swing of the metric across that parameter's full tolerance.

| Rank | Parameter | Kind | Nominal | Tol | S (before) | span (before) | S (after) | span (after) |
|------|-----------|------|---------|-----|------------|---------------|-----------|--------------|
| 1 | `Rload` | passive | 1000.0000 | ±5% | -0.040 | -0.4% | -0.051 | -0.5% |
| 2 | `DMOD.N` | model | 1.5000 | ±5% | 0.991 | +9.9% | 0.989 | +9.9% |
| 3 | `DMOD.IS` | model | 1e-14 | ±50% | -0.040 | -4.0% | -0.047 | -4.7% |
| 4 | `TEMP` | temp | 27 °C | — | -0.371 | -0.7% | -3.293 | -6.6% |
| 5 | `DMOD.RS` | model | 0.5000 | ±20% | 0.004 | +0.2% | 0.008 | +0.3% |

### Worst-case corner and PVT grid

| Deck | WC low | WC high | PVT min | PVT max | PVT corners in spec |
|------|--------|---------|---------|---------|---------------------|
| before | 0.963 | 1.163 | 1.048 | 1.097 | 0/27 |
| after | 0.433 | 0.769 | 0.483 | 0.729 | 27/27 |

## buck_converter

**Repair:** Duty 41.7% (ideal D*Vin) -> 46.1%, compensating the freewheel diode drop.

### `vout` — V(out) avg target 5.000 V, limits [4.850, 5.150] ← headline

| Deck | Nominal | MC mean | σ | CV | min … max | in-spec | Cpk |
|------|---------|---------|---|----|-----------|---------|-----|
| before (`buck_converter.cir`) | 4.519 | 4.519 | 0.084 | 1.9% | 4.229 … 4.742 | 0% | -1.32 |
| after (`buck_converter.cir`) | 5.085 | 5.085 | 0.091 | 1.8% | 4.772 … 5.327 | 77% | 0.24 |

### `ripple` — ripple target — Vpp, limits [—, 0.100]

| Deck | Nominal | MC mean | σ | CV | min … max | in-spec | Cpk |
|------|---------|---------|---|----|-----------|---------|-----|
| before (`buck_converter.cir`) | 0.008 | 0.008 | 0.000437 | 5.2% | 0.007 … 0.010 | 100% | 69.99 |
| after (`buck_converter.cir`) | 0.009 | 0.009 | 0.000446 | 5.2% | 0.007 … 0.010 | 100% | 68.31 |

**Distribution — before (vout):**

```
     4.229 | x 2
     4.275 | x 4
     4.321 | xxxx 11
     4.367 | xxxxxxxxxxxxxx 40
     4.414 | xxxxxxxxxxxxxxxxxxx 54
      4.46 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 107
     4.506 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 112
     4.552 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 83
     4.598 | xxxxxxxxxxxxxxxxxx 51
     4.644 | xxxxxxxxx 25
      4.69 | xx 7
     4.736 | x 4
     4.782 | 
     4.828 | 
     4.874   
      4.92   
     4.966   
     5.012   
     5.058   
     5.104   
           (# = inside spec, x = outside; LSL=4.85, USL=5.15)
```

**Distribution — after (vout):**

```
     4.772 | x 2
       4.8 | 
     4.828 | x 1
     4.855   #### 6
     4.883   ## 4
     4.911   ######## 14
     4.938   ############# 22
     4.966   ################ 28
     4.994   ######################### 42
     5.022   ############################### 52
     5.049   ################################## 58
     5.077   #################################### 62
     5.105   ######################################## 68
     5.133 | xxxxxxxxxxxxxxxxxxxxx 35
      5.16 | xxxxxxxxxxxxxxxxxxxxx 36
     5.188 | xxxxxxxxxxxxxxxxxxxx 34
     5.216 | xxxxxxxxxxx 18
     5.243 | xxxx 7
     5.271 | xxx 5
     5.299 | xxxx 6
           (# = inside spec, x = outside; LSL=4.85, USL=5.15)
```

### Sensitivity ranking (vout)

`S` = % change in the metric per 1% change in the parameter (temperature: % per 10 °C). `span` = total % swing of the metric across that parameter's full tolerance.

| Rank | Parameter | Kind | Nominal | Tol | S (before) | span (before) | S (after) | span (after) |
|------|-----------|------|---------|-----|------------|---------------|-----------|--------------|
| 1 | `C1` | passive | 4.7e-05 | ±10% | 0.000102 | +0.0% | 4.23e-05 | +0.0% |
| 2 | `L1` | passive | 0.0001 | ±10% | 7.26e-05 | +0.0% | 2.1e-05 | +0.0% |
| 3 | `Vin` | supply | 12.0000 | ±5% | 1.103 | +11.0% | 1.085 | +10.9% |
| 4 | `DMOD.N` | model | 1.0000 | ±5% | -0.107 | -1.1% | -0.088 | -0.9% |
| 5 | `DMOD.IS` | model | 1e-14 | ±50% | 0.004 | +0.4% | 0.003 | +0.3% |
| 6 | `TEMP` | temp | 27 °C | — | 0.152 | +0.3% | 0.124 | +0.2% |
| 7 | `Rload` | passive | 5.0000 | ±5% | 0.005 | +0.1% | 0.005 | +0.0% |
| 8 | `DMOD.RS` | model | 0.0100 | ±20% | -0.001 | -0.0% | -0.001 | -0.0% |

### Worst-case corner and PVT grid

| Deck | WC low | WC high | PVT min | PVT max | PVT corners in spec |
|------|--------|---------|---------|---------|---------------------|
| before | 4.195 | 4.847 | 4.225 | 4.809 | 0/27 |
| after | 4.740 | 5.433 | 4.768 | 5.398 | 9/27 |

## boost_converter

**Repair:** Duty 58.3% -> 60.5% (automated spec-repair; topology-correct magnitude).

### `vout` — V(out) avg target 12.000 V, limits [11.640, 12.360] ← headline

| Deck | Nominal | MC mean | σ | CV | min … max | in-spec | Cpk |
|------|---------|---------|---|----|-----------|---------|-----|
| before (`boost_converter.cir`) | 11.129 | 11.131 | 0.196 | 1.8% | 10.472 … 11.680 | 0% | -0.87 |
| after (`boost_converter.cir`) | 11.766 | 11.775 | 0.205 | 1.7% | 11.090 … 12.343 | 76% | 0.22 |

**Distribution — before (vout):**

```
     10.47 | x 2
     10.57 | xx 5
     10.66 | xx 6
     10.76 | xxxxxxxxx 23
     10.85 | xxxxxxxxxxxxxxxxx 43
     10.94 | xxxxxxxxxxxxxxxxxxxxxxxxxxxx 70
     11.04 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 99
     11.13 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 98
     11.23 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxx 71
     11.32 | xxxxxxxxxxxxxxxxxxxx 49
     11.42 | xxxxxxxxx 22
     11.51 | xx 6
      11.6 | xx 6
      11.7   
     11.79   
     11.89   
     11.98   
     12.08   
     12.17   
     12.27   
           (# = inside spec, x = outside; LSL=11.64, USL=12.36)
```

**Distribution — after (vout):**

```
     11.09 | x 2
     11.15 | 
     11.22 | x 2
     11.28 | xxxx 7
     11.34 | xxxxx 9
     11.41 | xxxxxxxxx 15
     11.47 | xxxxxxxxxxxxxxxx 26
     11.53 | xxxxxxxxxxxxxxxxxxxx 34
      11.6 | xxxxxxxxxxxxxxxxxxxxxxxx 41
     11.66   ##################################### 62
     11.73   ######################################## 67
     11.79   ##################################### 62
     11.85   ################################# 55
     11.92   ##################### 35
     11.98   ######################## 40
     12.04   ########### 18
     12.11   ######## 13
     12.17   ## 3
     12.23   ## 4
      12.3   ### 5
           (# = inside spec, x = outside; LSL=11.64, USL=12.36)
```

### Sensitivity ranking (vout)

`S` = % change in the metric per 1% change in the parameter (temperature: % per 10 °C). `span` = total % swing of the metric across that parameter's full tolerance.

| Rank | Parameter | Kind | Nominal | Tol | S (before) | span (before) | S (after) | span (after) |
|------|-----------|------|---------|-----|------------|---------------|-----------|--------------|
| 1 | `Vin` | supply | 5.0000 | ±5% | 1.073 | +10.7% | 1.069 | +10.7% |
| 2 | `DMOD.N` | model | 1.0000 | ±5% | -0.075 | -0.8% | -0.071 | -0.7% |
| 3 | `DMOD.IS` | model | 1e-14 | ±50% | 0.003 | +0.3% | 0.002 | +0.2% |
| 4 | `TEMP` | temp | 27 °C | — | 0.103 | +0.2% | 0.098 | +0.2% |
| 5 | `L1` | passive | 0.0001 | ±10% | -0.008 | -0.2% | 0.003 | +0.1% |
| 6 | `Cout` | passive | 0.0001 | ±10% | -0.007 | -0.1% | 0.004 | +0.1% |
| 7 | `DMOD.RS` | model | 0.0100 | ±20% | -0.001 | -0.0% | -0.001 | -0.0% |
| 8 | `Rload` | passive | 20.0000 | ±5% | 0.002 | +0.0% | 0.006 | +0.1% |

### Worst-case corner and PVT grid

| Deck | WC low | WC high | PVT min | PVT max | PVT corners in spec |
|------|--------|---------|---------|---------|---------------------|
| before | 10.409 | 11.854 | 10.457 | 11.794 | 9/27 |
| after | 11.024 | 12.554 | 11.062 | 12.462 | 12/27 |

## bjt_diffamp

**Repair:** Identity drift: generation produced an RC circuit, not a diff pair -> real differential pair with a 1 mA tail source.

### `ad` — Ad target 25.000 V/V, limits [22.500, 27.500] ← headline

| Deck | Nominal | MC mean | σ | CV | min … max | in-spec | Cpk |
|------|---------|---------|---|----|-----------|---------|-----|
| before (`bjt_diffamp.cir`) | 0.000 | 0.000 | 0.000 | — | 0.000 … 0.000 | 0% | 0.00 |
| after (`bjt_diffamp.cir`) | 24.810 | 25.573 | 3.310 | 12.9% | 20.354 … 32.637 | 47% | 0.19 |

### `cmrr` — CMRR target — dB, limits [60.000, —]

| Deck | Nominal | MC mean | σ | CV | min … max | in-spec | Cpk |
|------|---------|---------|---|----|-----------|---------|-----|
| before (`bjt_diffamp.cir`) | 200.000 | 200.000 | 0.000 | 0.0% | 200.000 … 200.000 | 100% | ∞ |
| after (`bjt_diffamp.cir`) | 116.434 | 116.543 | 1.943 | 1.7% | 111.343 … 121.823 | 100% | 9.70 |

**Distribution — before (ad):**

```
         0 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 500
     1.375 | 
      2.75 | 
     4.125 | 
       5.5 | 
     6.875 | 
      8.25 | 
     9.625 | 
        11 | 
     12.38 | 
     13.75 | 
     15.13 | 
      16.5 | 
     17.88 | 
     19.25 | 
     20.63 | 
        22 | 
     23.38   
     24.75   
     26.13   
           (# = inside spec, x = outside; LSL=22.5, USL=27.5)
```

**Distribution — after (ad):**

```
     20.35 | xxxxxxxxxxxxxxxxxxxxxxxxxx 29
     20.97 | xxxxxxxxxxxxxxxxxxxxxxxxxx 29
     21.58 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 33
      22.2 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 44
     22.81   ################################# 36
     23.42   ####################### 25
     24.04   ############################## 33
     24.65   ######################## 26
     25.27   ######################### 27
     25.88   ######################## 26
      26.5   ##################### 23
     27.11 | xxxxxxxxxxxxxxxxxxxxxxx 25
     27.72 | xxxxxxxxxxxxxxxxxxxxx 23
     28.34 | xxxxxxxxxxxxxxxxxxxxxxx 25
     28.95 | xxxxxxxxxxxxxx 15
     29.57 | xxxxxxxxxxxxxxxxxx 20
     30.18 | xxxxxxxxxxxxxxxx 18
     30.79 | xxxxxxxxxxxxxxxxxxx 21
     31.41 | xxxxxxxxxxxxxx 15
     32.02 | xxxxxx 7
           (# = inside spec, x = outside; LSL=22.5, USL=27.5)
```

### Sensitivity ranking (ad)

`S` = % change in the metric per 1% change in the parameter (temperature: % per 10 °C). `span` = total % swing of the metric across that parameter's full tolerance.

| Rank | Parameter | Kind | Nominal | Tol | S (before) | span (before) | S (after) | span (after) |
|------|-----------|------|---------|-----|------------|---------------|-----------|--------------|
| 1 | `V1` | supply | 5.0000 | ±5% | — | — | — | — |
| 2 | `R1` | passive | 1000.0000 | ±5% | — | — | — | — |
| 3 | `C1` | passive | 1e-06 | ±10% | — | — | — | — |
| 4 | `TEMP` | temp | 27 °C | — | — | — | -3.349 | -6.7% |
| 5 | `Rc2` | passive | 2600.0000 | ±5% | — | — | 0.996 | +10.0% |
| 6 | `QNPN.BF` | model | 200.0000 | ±50% | — | — | 0.006 | +0.6% |
| 7 | `QNPN.VAF` | model | 150.0000 | ±25% | — | — | 0.007 | +0.4% |
| 8 | `Rc1` | passive | 2600.0000 | ±5% | — | — | -0.004 | -0.0% |

### Worst-case corner and PVT grid

| Deck | WC low | WC high | PVT min | PVT max | PVT corners in spec |
|------|--------|---------|---------|---------|---------------------|
| before | 0.000 | 0.000 | 0.000 | 0.000 | 0/27 |
| after | 19.615 | 33.643 | 20.695 | 31.993 | 9/27 |

## ce_amp_bias_and_gain

**Repair:** Floating output node (silent DC-solution corruption) -> light 100k load.

### `vce` — Vce target 6.000 V, limits [5.400, 6.600]

| Deck | Nominal | MC mean | σ | CV | min … max | in-spec | Cpk |
|------|---------|---------|---|----|-----------|---------|-----|
| before (`ce_amp_bias_and_gain.cir`) | 12.000 | 11.979 | 0.198 | 1.7% | 11.400 … 12.519 | 0% | -9.04 |
| after (`ce_amp_bias_and_gain.cir`) | 5.959 | 5.979 | 0.178 | 3.0% | 5.412 … 6.520 | 100% | 1.09 |

### `gain` — \|gain\| target 80.000 V/V, limits [72.000, 88.000] ← headline

| Deck | Nominal | MC mean | σ | CV | min … max | in-spec | Cpk |
|------|---------|---------|---|----|-----------|---------|-----|
| before (`ce_amp_bias_and_gain.cir`) | 77.309 | 78.988 | 9.242 | 11.7% | 62.365 … 102.509 | 52% | 0.25 |
| after (`ce_amp_bias_and_gain.cir`) | 75.675 | 78.087 | 8.936 | 11.4% | 60.800 … 102.236 | 53% | 0.23 |

**Distribution — before (gain):**

```
     62.36 | xxxxx 5
     64.37 | xxxxxxxxxxxxxxxxxxxxx 22
     66.38 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 42
     68.39 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 40
     70.39 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 40
      72.4   ###################################### 40
     74.41   ################################### 37
     76.42   ############################# 30
     78.42   ################################### 37
     80.43   #################### 21
     82.44   ####################################### 41
     84.44   ########################### 28
     86.45 | xxxxxxxxxxxxxxxxxxxxxxxxxxx 28
     88.46 | xxxxxxxxxxxxxxxxxx 19
     90.47 | xxxxxxxxxxxxxxxxxxxxxx 23
     92.47 | xxxxxxxxxxxxxxxxxx 19
     94.48 | xxxxxxxxxxxx 13
     96.49 | xxxxxx 6
      98.5 | xxxx 4
     100.5 | xxxxx 5
           (# = inside spec, x = outside; LSL=72, USL=88)
```

**Distribution — after (gain):**

```
      60.8 | xx 3
     62.87 | xxxxxxx 10
     64.94 | xxxxxxxxxxxxxxxxxxxxx 29
     67.02 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 47
     69.09 | xxxxxxxxxxxxxxxxxxxxxxxxxx 36
     71.16 | xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx 55
     73.23   ############################### 43
      75.3   ############################# 40
     77.37   ###################### 30
     79.45   ############################### 43
     81.52   #################### 28
     83.59   ################ 22
     85.66   ################# 23
     87.73 | xxxxxxxxxxxxxxxxxxxx 28
      89.8 | xxxxxxxxxxxxx 18
     91.88 | xxxxxxxxxxxxx 18
     93.95 | xxxxxxxxx 13
     96.02 | xxxxxxx 9
     98.09 | x 1
     100.2 | xxx 4
           (# = inside spec, x = outside; LSL=72, USL=88)
```

### Sensitivity ranking (gain)

`S` = % change in the metric per 1% change in the parameter (temperature: % per 10 °C). `span` = total % swing of the metric across that parameter's full tolerance.

| Rank | Parameter | Kind | Nominal | Tol | S (before) | span (before) | S (after) | span (after) |
|------|-----------|------|---------|-----|------------|---------------|-----------|--------------|
| 1 | `Vcc` | supply | 12.0000 | ±5% | 1.109 | +11.1% | 1.110 | +11.1% |
| 2 | `Rc` | passive | 2200.0000 | ±5% | 0.980 | +9.8% | 0.959 | +9.6% |
| 3 | `Re` | passive | 4300.0000 | ±5% | -0.906 | -9.1% | -0.906 | -9.1% |
| 4 | `QNPN.BF` | model | 100.0000 | ±50% | 0.081 | +8.1% | 0.081 | +8.1% |
| 5 | `R1` | passive | 68000.0000 | ±5% | -0.675 | -6.8% | -0.676 | -6.8% |
| 6 | `R2` | passive | 47000.0000 | ±5% | 0.620 | +6.2% | 0.621 | +6.2% |
| 7 | `TEMP` | temp | 27 °C | — | -2.882 | -5.8% | -2.881 | -5.8% |
| 8 | `QNPN.VAF` | model | 100.0000 | ±25% | 0.017 | +0.8% | 0.016 | +0.8% |

### Worst-case corner and PVT grid

| Deck | WC low | WC high | PVT min | PVT max | PVT corners in spec |
|------|--------|---------|---------|---------|---------------------|
| before | 49.860 | 123.283 | 58.713 | 104.429 | 9/27 |
| after | 48.807 | 120.679 | 57.470 | 102.225 | 7/27 |

---

Full evidence (every sample, every corner): `robustness_log.json`.

