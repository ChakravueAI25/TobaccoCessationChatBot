"""Print a cautious statistical pilot report from an analysis frame."""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

PAIRED_MEASURES = {
    "cpd": ("cpd_baseline", "cpd_endline"),
    "readiness": ("readiness_baseline", "readiness_endline"),
    "craving": ("craving_week1_mean", "craving_lastweek_mean"),
}


def _paired_report(frame: pd.DataFrame, label: str, pre: str, post: str) -> str:
    values = frame[[pre, post]].apply(pd.to_numeric, errors="coerce").dropna()
    if values.empty:
        return f"{label}: no endline data"
    if len(values) < 2:
        return f"{label}: n={len(values)}; insufficient pairs for paired tests"

    before = values[pre].to_numpy(float)
    after = values[post].to_numpy(float)
    change = after - before
    mean_change = float(np.mean(change))
    sd_change = float(np.std(change, ddof=1))
    ci = stats.t.interval(0.95, len(change) - 1, loc=mean_change, scale=stats.sem(change))
    t_result = stats.ttest_rel(after, before)
    try:
        wilcoxon = stats.wilcoxon(after, before)
        wilcoxon_text = f"W={wilcoxon.statistic:.3g}, p={wilcoxon.pvalue:.4g}"
    except ValueError:
        wilcoxon_text = "W/p unavailable (all changes are zero)"
    effect = mean_change / sd_change if sd_change else float("nan")
    return (
        f"{label}: n={len(values)}, pre mean/median={before.mean():.2f}/{np.median(before):.2f}, "
        f"post mean/median={after.mean():.2f}/{np.median(after):.2f}, "
        f"change={mean_change:.2f}, 95% CI=[{ci[0]:.2f}, {ci[1]:.2f}], "
        f"paired t={t_result.statistic:.3g}, p={t_result.pvalue:.4g}, "
        f"Wilcoxon {wilcoxon_text}, d_paired={effect:.3g}"
    )


def render_report(frame: pd.DataFrame) -> str:
    lines = ["# Statistical pilot report", "", f"Participants: {len(frame)}"]
    completed = frame["completed"].eq("Y").sum()
    attrition = len(frame) - completed
    attrition_rate = attrition / len(frame) if len(frame) else 0
    lines += [
        f"Completed endline: {completed} ({completed / len(frame):.1%})" if len(frame) else "Completed endline: 0 (no participants)",
        f"Attrition: {attrition} ({attrition_rate:.1%})",
        "",
        "## Paired measures",
    ]
    lines.extend(_paired_report(frame, label, *columns) for label, columns in PAIRED_MEASURES.items())
    lines += ["", "## Engagement, safety and technical descriptives"]
    for column in ("sessions", "turns", "cravings_handled", "lapses", "coping_selected_n", "safety_events", "days_active"):
        values = pd.to_numeric(frame[column], errors="coerce")
        lines.append(f"{column}: mean={values.mean():.2f}, median={values.median():.2f}")
    fallback = pd.to_numeric(frame["fallback_rate"], errors="coerce").dropna()
    lines.append(f"fallback_rate: mean={fallback.mean():.1%}" if not fallback.empty else "fallback_rate: no data")
    lines += ["", "## Endline categorical responses"]
    for column in ("quit_attempt_made", "abstinent_7day"):
        counts = frame[column].dropna().astype(str).value_counts()
        lines.append(f"{column}: no data" if counts.empty else f"{column}: " + ", ".join(f"{key}={value} ({value / counts.sum():.1%})" for key, value in counts.items()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="analysis_frame.csv")
    args = parser.parse_args(argv)
    print(render_report(pd.read_csv(args.input)))


if __name__ == "__main__":
    main()
