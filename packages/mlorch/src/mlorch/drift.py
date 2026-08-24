"""Statistical drift detection between a reference feature distribution and a new batch.

`scipy` is not installed in this environment (`pip show scipy` finds nothing), so both
statistics below are implemented by hand from their textbook definitions rather than pulled
from a stats library — which is arguably more in keeping with this project's from-scratch
ethos anyway. Two independent statistics are provided since they catch different things and
neither is strictly better:

- Population Stability Index (PSI): a binned measure, standard in the ML monitoring world,
  cheap to compute and easy to threshold against the conventional 0.1 / 0.25 rule of thumb.
- Two-sample Kolmogorov-Smirnov (KS): distribution-free (no binning choice to get wrong),
  based on the maximum gap between empirical CDFs, with an asymptotic p-value.

Both take raw numeric samples (`numpy` arrays or anything array-like) — no dependency on the
rest of mlorch's feature/dataset types, so they're usable standalone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True, slots=True)
class DriftReport:
    statistic_name: str
    value: float
    threshold: float
    drifted: bool


def population_stability_index(
    reference: npt.ArrayLike, current: npt.ArrayLike, *, bins: int = 10
) -> float:
    """PSI = sum over bins of (cur_pct - ref_pct) * ln(cur_pct / ref_pct).

    Bin edges are built from *reference* quantiles (not fixed-width bins), so each reference
    bin starts with roughly equal mass — the standard PSI construction, which avoids one wide,
    low-density bin dominating the sum. Percentages are clipped away from zero before the log
    so a bin that is merely rare (rather than genuinely impossible) doesn't produce -inf.

    Conventional reading: PSI < 0.1 no significant shift, 0.1-0.25 moderate shift, > 0.25
    significant shift.
    """
    ref = np.asarray(reference, dtype=float)
    cur = np.asarray(current, dtype=float)
    if ref.size == 0 or cur.size == 0:
        raise ValueError("population_stability_index requires non-empty reference and current samples")

    quantiles = np.linspace(0.0, 1.0, bins + 1)
    edges = np.unique(np.quantile(ref, quantiles))
    if edges.size < 2:
        return 0.0  # reference has zero spread (all one value) -- nothing meaningful to compare
    edges[0], edges[-1] = -np.inf, np.inf

    ref_counts, _ = np.histogram(ref, bins=edges)
    cur_counts, _ = np.histogram(cur, bins=edges)
    ref_pct = np.clip(ref_counts / ref.size, 1e-6, None)
    cur_pct = np.clip(cur_counts / cur.size, 1e-6, None)
    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def ks_statistic(reference: npt.ArrayLike, current: npt.ArrayLike) -> tuple[float, float]:
    """Two-sample Kolmogorov-Smirnov statistic D (the maximum absolute distance between the
    two samples' empirical CDFs) and an approximate two-sided p-value from the asymptotic
    Kolmogorov distribution.

    Implemented from the definition: at every point in the pooled, sorted sample, compare the
    fraction of each sample at or below that point.
    """
    ref = np.sort(np.asarray(reference, dtype=float))
    cur = np.sort(np.asarray(current, dtype=float))
    n, m = ref.size, cur.size
    if n == 0 or m == 0:
        raise ValueError("ks_statistic requires non-empty reference and current samples")

    pooled = np.concatenate([ref, cur])
    cdf_ref = np.searchsorted(ref, pooled, side="right") / n
    cdf_cur = np.searchsorted(cur, pooled, side="right") / m
    d = float(np.max(np.abs(cdf_ref - cdf_cur)))

    effective_n = n * m / (n + m)
    p_value = _ks_asymptotic_pvalue(d, effective_n)
    return d, p_value


def _ks_asymptotic_pvalue(d: float, effective_n: float) -> float:
    """Kolmogorov's asymptotic distribution for the two-sample KS statistic (Marsaglia et al.
    form): P(D > d) = 2 * sum_{k=1..inf} (-1)^(k-1) * exp(-2 k^2 lambda^2), with lambda a
    rescaling of `d` by the effective sample size. The series converges fast; 100 terms is
    massive overkill but the computation is trivial, so there's no reason to cut it closer.
    """
    lam = (np.sqrt(effective_n) + 0.12 + 0.11 / np.sqrt(effective_n)) * d
    if lam < 0.2:
        return 1.0
    total = sum((-1) ** (k - 1) * np.exp(-2 * k**2 * lam**2) for k in range(1, 101))
    return float(min(max(2 * total, 0.0), 1.0))


def check_drift(
    reference: npt.ArrayLike,
    current: npt.ArrayLike,
    *,
    method: Literal["psi", "ks"] = "psi",
    threshold: float | None = None,
) -> DriftReport:
    """Run one of the two drift statistics and flag drift against a threshold.

    For "psi", `value` is the PSI itself and drift is flagged when it *exceeds* the threshold
    (default 0.25, the conventional "significant shift" cutoff). For "ks", `value` is the
    p-value of the two-sample test and drift is flagged when it *falls below* the threshold
    (default 0.05) -- a small p-value means the two samples are unlikely to come from the same
    distribution.
    """
    match method:
        case "psi":
            value = population_stability_index(reference, current)
            thr = 0.25 if threshold is None else threshold
            return DriftReport("psi", value, thr, drifted=value > thr)
        case "ks":
            _d, p_value = ks_statistic(reference, current)
            thr = 0.05 if threshold is None else threshold
            return DriftReport("ks_pvalue", p_value, thr, drifted=p_value < thr)
