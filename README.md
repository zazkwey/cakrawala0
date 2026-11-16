## Harmonization method

The goal is one monthly series per 5 km cell in MODIS-equivalent units, so that the sensor of origin no longer matters. The method is deliberately simple and transparent: one multiplicative ratio per region and season, calibrated on the years where both sensors fly, with uncertainty bands measured by cross-validation.

**Notation.** *c* = 0.05° cell, *r* = 5° × 5° region containing the cell, *t* = month, *μ*(*t*) = calendar month of the year (1 to 12), *s* ∈ {MODIS, VIIRS}. For each cell, sensor and month three measures *x* are computed:

| measure | definition |
|---|---|
| `n_hotspots` | number of detections |
| `fire_days` | number of distinct dates with at least one detection |
| `frp_sum` | sum of fire radiative power, in MW |

**Pipeline**

1. **Clean** (`clean.py`). Merge archive and near-real-time files (NRT rows are kept only after the last archive date of the same satellite), drop duplicates, keep only vegetation-fire detections (FIRMS `type = 0`), and drop low-confidence detections (MODIS confidence below 30, VIIRS class `low`).
2. **Grid** (`grid.py`). Aggregate to cell × sensor × month. Cells that burn in more than 50% of all months of a sensor's record (gas flares, industry) are removed.
3. **Sampling effort.** Let *n*(*s*, *t*) be the number of satellites of sensor *s* that reported in month *t*. VIIRS goes from 1 to 2 satellites over time, which would create a fake rise, so every measure is divided by it:

$$\tilde{x}^{\,s}_{c,t} = \frac{x^{\,s}_{c,t}}{n_{s,t}}$$

4. **Overlap period.** *O* is the set of months from January 2013 onward in which both sensors reported. At least 12 months are required.
5. **Ratio per region and season.** Sum the per-satellite values over the cells of a region (*M* for MODIS and *V* for VIIRS), then:

$$k_{r,\mu} = \frac{\sum_{t \in O,\ \mu(t)=\mu} M_{r,t}}{\sum_{t \in O,\ \mu(t)=\mu} V_{r,t}}$$

   A ratio is used only if ∑*V* > 0 and ∑(*M* + *V*) ≥ 20. Otherwise the code falls back to the same ratio pooled over all regions for that month of the year (*k*(*μ*)), and finally to one overall ratio (*k*(all)). One ratio is fitted separately for each of the three measures.
6. **Map VIIRS to MODIS-equivalent.** MODIS flies two satellites (Terra and Aqua), so the per-satellite value is scaled back up by *N*(ref) = 2:

$$\hat{x}_{c,t} = \frac{x^{\,\text{VIIRS}}_{c,t}}{n_{\text{VIIRS},t}} \; k_{r(c),\,\mu(t)} \; N_{\text{ref}}$$

7. **Splice.** Real MODIS is used up to its last month *T*(M) and mapped VIIRS after it. In the demo build, *T*(M) is set to December 2022:

$$x^{*}_{c,t} = \begin{cases} x^{\,\text{MODIS}}_{c,t} & t \le T_M \quad (\text{source} = \text{MODIS}) \\ \hat{x}_{c,t} & t > T_M \quad (\text{source} = \text{VIIRS\_mapped}) \end{cases}$$

8. **Uncertainty bands (leave-one-year-out cross-validation).** For each year *y* in the overlap, the region-months of that year are predicted from ratios fitted on the other years, and the log error is kept:

$$e_{r,t} = \ln\frac{M_{r,t}+1}{\hat{M}^{(-y)}_{r,t}+1}$$

   The errors are pooled and split into up to four size classes by the size of the predicted total. In each class *b*, the 2.5th and 97.5th percentiles give *q*(lo) and *q*(hi). For a mapped region-month with estimated total *T* = ∑*x̂* in class *b*:

$$lo = \max\big((T+1)\,e^{q^{lo}_b}-1,\ 0\big), \qquad hi = (T+1)\,e^{q^{hi}_b}-1$$

   The bands of the cells are scaled so that they add up exactly to the region band, which treats cell errors as fully correlated (conservative):

$$\hat{x}^{\,lo}_{c,t} = \hat{x}_{c,t}\,\min\!\Big(\tfrac{lo}{T},\,1\Big), \qquad \hat{x}^{\,hi}_{c,t} = \hat{x}_{c,t}\,\max\!\Big(\tfrac{hi}{T},\,1\Big)$$

   Real MODIS months have no band (*lo* = *hi* = *x*).
9. **Validate** (`backtest()`). Pretend MODIS ended at a cutoff, fit on the months before it, predict the region-months after it from VIIRS, and compare with the MODIS that really exists. Reported: bias = ∑pred / ∑obs, the correlation of ln(1 + obs) with ln(1 + pred), the median absolute percentage error, and the coverage mean[*lo* ≤ obs ≤ *hi*].

**From series to calendar and anomalies** (`climatology.py`). For a chosen set of baseline years *B*, the normal for calendar month *μ* is the median of that month over *B*, and each month is compared with it:

$$\text{normal}_\mu = \underset{y \in B}{\text{median}}\; x^{*}_{y,\mu}, \qquad \rho_{y,\mu} = \frac{x^{*}_{y,\mu}+1}{\text{normal}_\mu + 1}$$

A month is flagged **high** if its band lies above the 75th percentile of the baseline and *ρ* ≥ 1.5, and **low** if its band lies below the 25th percentile and *ρ* ≤ 2/3. The "+1" keeps the ratio defined when the normal is zero.

**What the method assumes, and where it is approximate**
- The ratio depends only on region and month of the year, not on how intense the season is. That is why intense seasons are under-predicted for `fire_days`.
- *n*(*s*, *t*) counts satellites that reported anywhere in the study area in that month, not days of coverage.
- The error size classes in step 8 are fitted on the per-satellite scale but looked up from the two-satellite total *T*, a small approximation.
- Neither SNPP-only calibration nor per-cell ratios are used in the published build (SNPP-only was tested and not adopted, see Known limits).