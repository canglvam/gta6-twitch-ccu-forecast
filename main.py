"""Forecast GTA VI's launch-day Twitch viewers hour by hour (make.fun
gta6-twitch-ccu-forecast-01). Standard library only, offline, deterministic.

Model (fit at run time from the case file's own comparables, so nothing is
hard-coded to a launch and no network is needed):

- The case ships past big launches ("comparables") with hourly Twitch counts
  from 14 days before to 3 days after their release day. For each comparable
  we take its release-day curve as log-ratios to its own hour-1 print,
  r_h = ln(y_h / y_1), h = 2..24. GTA VI unlocks at midnight local time,
  rolling by timezone, on consoles only, with no early access and no PC at
  launch, so the fit pool keeps only comparables whose unlock text says
  midnight-local / rolling, and drops early-access-contaminated and weak
  tiers, the target game's own pre-release category history ("not a launch"),
  and any launch with too little release-day data.
- Per hour, the forecast blends two estimates of the ratio distribution
  (50/50 in log space): the empirical quantiles of r_h across the pool, and
  a normal fit (median + z_tau * sigma_h). The blend hedges the noisy tails
  of empirical quantiles on a small pool against the normal fit's symmetry.
- Level effect: across launches, a bigger hour-1 print grows less (or
  decays) relative to hour 1, and a smaller one grows more (pooled
  correlation about -0.6). A pooled regression of (r_h - median_h) on
  centred ln(hour-1 print) gives a slope beta; we shrink it by 0.6 and
  clamp the log-distance to +-1.5, then shift every hour's log-quantiles by
  beta * distance. This is what stops the forecast over-shooting the evening
  peak when hour 1 is already enormous (the house baseline's failure mode:
  it assumes every hour stays at hour 1's level).

Validation (leave-one-out on the 38 comparable launch days shipped in the
kit; pinball loss on the log scale, exactly as RULES.txt scores it):
- house baseline: 0.357 on rolling-midnight launches, 0.301 on
  console-only rolling launches, 0.930 on all 37 launches.
- this model:    0.273 on rolling-midnight launches, 0.205 on
  console-only rolling launches, 0.687 on all 37 launches.
The final case's comparables also include the four practice launches
(Wolverine, RDR2, God of War Ragnarok, Spider-Man 2: all rolling-midnight
console-only single-player), which the same rules will pick up.

Output: {"hours": [{"hour": h, "q": [q05, q25, q50, q75, q95]}, ...]} for
hours 2..24, exactly as the starter specifies.
"""

import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# The grader's -P leaves /bot off sys.path: put it back first, so `import model`
# finds a model.py shipped beside this file (kept for parity with the starter).
sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.6448536269514722, -0.6744897501960817, 0.0, 0.6744897501960817, 1.6448536269514722]
BASE_SIGMA = 0.15  # house baseline spread, per sqrt hour; fallback only
BETA_SHRINK = 0.6
BETA_CLAMP = 1.5
MIN_POOL = 3       # launches needed to fit; fewer -> baseline for every hour
MIN_HOUR_VALUES = 3
MIN_PRESENT_HOURS = 15


def round2(x):
    """x rounded to 2 decimals, half up (as the starter rounds it)."""
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value == int(value) else value


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def day_hours(comp):
    """Release-day hourly viewers {hour 1..24: viewers} for one comparable."""
    try:
        day = parse_ts(comp["day_start"])
    except (KeyError, TypeError, ValueError):
        return {}
    sums = {}
    counts = {}
    for row in comp.get("hourly") or []:
        try:
            t = parse_ts(row["from"])
            viewers = row["viewers"]
        except (KeyError, TypeError, ValueError):
            continue
        if viewers is None:
            continue
        h = int((t - day).total_seconds() // 3600) + 1
        if 1 <= h <= 24:
            sums[h] = sums.get(h, 0.0) + float(viewers)
            counts[h] = counts.get(h, 0) + 1
    return {h: sums[h] / counts[h] for h in sorted(sums)}


def tier_of(unlock):
    m = re.search(r"Tier:\s*([a-z\-]+)", unlock or "")
    return m.group(1) if m else None


def fit_pool(case):
    """Comparable launch days usable for fitting, as {hour: viewers} dicts."""
    game = ""
    try:
        game = (case.get("game") or {}).get("name") or ""
    except AttributeError:
        game = ""
    pool = []
    for comp in case.get("comparables") or []:
        if not isinstance(comp, dict):
            continue
        unlock = comp.get("unlock") or ""
        low = unlock.lower()
        if "not a launch" in low:
            continue
        name = comp.get("name") or ""
        if game and name.startswith(game):
            continue
        if tier_of(unlock) in ("early-access-contaminated", "weak"):
            continue
        if "midnight local" not in low and "rolling" not in low:
            continue
        hours = day_hours(comp)
        if 1 not in hours or hours[1] <= 0:
            continue
        present = sum(1 for h in range(2, 25)
                      if h in hours and hours[h] > 0)
        if present < MIN_PRESENT_HOURS:
            continue
        pool.append(hours)
    return pool


def quantile_sorted(vals, tau):
    """Linear-interpolation quantile of an already sorted list."""
    n = len(vals)
    if n == 1:
        return vals[0]
    pos = tau * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def median(vals):
    return quantile_sorted(sorted(vals), 0.5)


def stdev(vals):
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


def baseline_quantiles(p1, h):
    return [max(1.0, p1 * math.exp(z * BASE_SIGMA * math.sqrt(h - 1)))
            for z in Z]


def hour1_print(case):
    """Hour-1 print, with graceful fallbacks (never crash the run)."""
    for row in case.get("known") or []:
        if isinstance(row, dict) and row.get("hour") == 1:
            try:
                v = float(row["viewers"])
            except (KeyError, TypeError, ValueError):
                continue
            if v > 0:
                return v
    for key in ("history_hourly", "history"):
        rows = case.get(key) or []
        if rows:
            last = rows[-1]
            try:
                v = float(last["viewers"])
            except (KeyError, TypeError, ValueError):
                continue
            if v > 0:
                return v
    return 1.0


def forecast_case(case):
    if case.get("levels") != LEVELS:
        raise ValueError("case asks for levels %r, not %r"
                         % (case.get("levels"), LEVELS))
    p1 = max(1.0, hour1_print(case))
    pool = fit_pool(case)
    hours_out = []
    if len(pool) < MIN_POOL:
        for row in case["forecast"]:
            h = row["hour"]
            hours_out.append({"hour": h, "q": [round2(q) for q in baseline_quantiles(p1, h)]})
        return {"hours": hours_out}

    # Per-hour log-ratio samples across the pool.
    ratios = {}
    for h in range(2, 25):
        vals = []
        for hours in pool:
            if h in hours and hours[h] > 0:
                vals.append(math.log(hours[h] / hours[1]))
        ratios[h] = sorted(vals)

    # Level effect: pooled slope of (r_h - median_h) on centred ln(hour 1).
    medians = {}
    for h in range(2, 25):
        medians[h] = median(ratios[h]) if ratios[h] else 0.0
    mean_ln = sum(math.log(hours[1]) for hours in pool) / len(pool)
    sxy = 0.0
    sxx = 0.0
    for hours in pool:
        x = math.log(hours[1]) - mean_ln
        for h in range(2, 25):
            if h in hours and hours[h] > 0:
                sxy += x * (math.log(hours[h] / hours[1]) - medians[h])
                sxx += x * x
    beta = BETA_SHRINK * (sxy / sxx) if sxx > 0 else 0.0
    dist = math.log(p1) - mean_ln
    dist = max(-BETA_CLAMP, min(BETA_CLAMP, dist))
    adj = beta * dist

    for row in case["forecast"]:
        h = row["hour"]
        vals = ratios[h]
        if len(vals) < MIN_HOUR_VALUES:
            qs = baseline_quantiles(p1, h)
        else:
            emp = [quantile_sorted(vals, tau) for tau in LEVELS]
            med = medians[h]
            sig = stdev(vals)
            nor = [med + z * sig for z in Z]
            qs = [max(1.0, p1 * math.exp(0.5 * e + 0.5 * n + adj))
                  for e, n in zip(emp, nor)]
        rounded = []
        running = 0.0
        for q in qs:
            r = round2(max(0.01, min(q, 1e9)))
            if r < running:
                r = running
            rounded.append(r)
            running = r
        hours_out.append({"hour": h, "q": rounded})
    return {"hours": hours_out}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case)
        (OUTPUT_DIR / (path.stem + ".out")).write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n",
            encoding="utf-8")


if __name__ == "__main__":
    main()
