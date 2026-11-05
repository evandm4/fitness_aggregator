"""Statistics over sessions, as pandas DataFrames.

Everything starts from ``sets_frame``: one row per set, built from sessions
that already have corrections applied. The other functions are pure
``DataFrame -> DataFrame`` (or dict) transforms, so a GUI can reuse them.

Conventions:

* Warm-ups are left out unless ``include_warmups=True``, and never count
  toward top sets, e1RM, or PRs.
* Drop sets count toward volume and set counts, not top sets, e1RM, or PRs.
* ``load`` is the weight that counts: the recorded weight, or for assisted
  lifts bodyweight - assistance.
* e1RM is only estimated from sets of 1-12 reps; beyond that it's unreliable.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable
from datetime import datetime

import numpy as np
import pandas as pd

from .bodyweight import Bodyweight
from .catalog import ASSISTED, LOADED, TIMED, UNLOADED, Catalog
from .models import SetType
from .sessions import Session

E1RM_MAX_REPS = 12
REP_MAX_RANGE = 15
FORMULAS = ("epley", "brzycki")
PR_CANDIDATES = ("normal", "failure")  # set types that can be top sets / PRs

SET_COLUMNS = [
    "session", "session_name", "workout", "exercise", "kind", "set_type", "position",
    "weight", "reps", "seconds", "rest_seconds", "rpe", "load", "bodyweight", "volume",
    "corrected", "pr_candidate", "merged_from", "merge_label", "pre_merge_weight",
]


# --- the base frame -----------------------------------------------------------

def sets_frame(
    sessions: Iterable[Session],
    catalog: Catalog,
    bodyweight: Bodyweight,
    *,
    include_warmups: bool = False,
) -> pd.DataFrame:
    rows = []
    for session in sessions:
        for w in session.members:
            for entry in w.exercises:
                kind = catalog.kind(entry.name)
                for s in entry.sets:
                    if s.set_type is SetType.WARMUP and not include_warmups:
                        continue
                    bw = bodyweight.at(w.started_at) if kind == ASSISTED else np.nan
                    load = bw - s.weight if kind == ASSISTED else s.weight
                    counts_load = kind in (LOADED, ASSISTED)
                    rows.append({
                        "session": session.started_at,
                        "session_name": session.name,
                        "workout": w.started_at,
                        "exercise": entry.name,
                        "kind": kind,
                        "set_type": s.set_type.value,
                        "position": s.position,
                        "weight": s.weight,
                        "reps": s.reps,
                        "seconds": s.seconds,
                        "rest_seconds": np.nan if s.rest_seconds is None else s.rest_seconds,
                        "rpe": np.nan if s.rpe is None else s.rpe,
                        "load": load,
                        "bodyweight": bw,
                        "volume": load * s.reps if counts_load else 0.0,
                        # Set fixes only; merge adjustments are shown through merged_from.
                        "corrected": any(c != entry.merge_correction_id for c in s.correction_ids),
                        "pr_candidate": s.set_type.value in PR_CANDIDATES,
                        "merged_from": entry.merged_from or "",
                        "merge_label": entry.merge_label,
                        "pre_merge_weight": np.nan if s.pre_merge_weight is None else s.pre_merge_weight,
                    })
    df = pd.DataFrame(rows, columns=SET_COLUMNS)
    for col in ("session", "workout"):
        df[col] = pd.to_datetime(df[col])
    return df


def e1rm(load, reps, formula: str = "epley") -> np.ndarray:
    """Estimated one-rep max. NaN where it can't be estimated (no load, 0 or >12 reps)."""
    if formula not in FORMULAS:
        raise ValueError(f"unknown formula {formula!r}; use one of {', '.join(FORMULAS)}")
    load = np.asarray(load, dtype=float)
    reps = np.asarray(reps, dtype=float)
    valid = (reps >= 1) & (reps <= E1RM_MAX_REPS) & (load > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        if formula == "epley":
            est = np.where(reps == 1, load, load * (1 + reps / 30))
        else:
            est = load * 36 / (37 - reps)
    return np.where(valid, est, np.nan)


def _with_e1rm(df: pd.DataFrame, formula: str) -> pd.DataFrame:
    df = df.copy()
    df["e1rm"] = np.where(df["pr_candidate"], e1rm(df["load"], df["reps"], formula), np.nan)
    return df


def _top_row(g: pd.DataFrame) -> pd.Series | None:
    cand = g[g["pr_candidate"]]
    if cand.empty:
        return None
    return cand.sort_values(["load", "reps"], ascending=False).iloc[0]


# --- per exercise -------------------------------------------------------------

def exercise_history(df: pd.DataFrame, exercise: str, formula: str = "epley") -> pd.DataFrame:
    """One row per session in which ``exercise`` was done."""
    d = _with_e1rm(df[df["exercise"] == exercise], formula)
    rows = []
    for session, g in d.groupby("session", sort=True):
        top = _top_row(g)
        best = g.loc[g["e1rm"].idxmax()] if g["e1rm"].notna().any() else None
        cand = g[g["pr_candidate"]]
        rows.append({
            "session": session,
            "kind": g["kind"].iloc[0],
            "top_load": np.nan if top is None else top["load"],
            "top_reps": np.nan if top is None else top["reps"],
            "top_weight": np.nan if top is None else top["weight"],
            "top_pre_merge_weight": np.nan if top is None else top["pre_merge_weight"],
            "e1rm": np.nan if best is None else best["e1rm"],
            "e1rm_load": np.nan if best is None else best["load"],
            "e1rm_reps": np.nan if best is None else best["reps"],
            "sets": len(g),
            "reps": g["reps"].sum(),
            "volume": g["volume"].sum(),
            "max_seconds": g["seconds"].max(),
            "total_seconds": g["seconds"].sum(),
            "max_reps": g["reps"].max(),
            "min_assistance": cand["weight"].min() if not cand.empty else np.nan,
            "corrected": bool(g["corrected"].any()),
            "merged_from": next((m for m in g["merged_from"] if m), ""),
            "merge_label": next((m for m in g["merge_label"] if m), ""),
        })
    return pd.DataFrame(rows)


def rep_maxes(df: pd.DataFrame, exercise: str, max_reps: int = REP_MAX_RANGE) -> pd.DataFrame:
    """Best load for at least N reps, for N = 1..max_reps (only where achieved)."""
    d = df[(df["exercise"] == exercise) & df["pr_candidate"] & (df["load"] > 0)]
    rows = []
    for n in range(1, max_reps + 1):
        eligible = d[d["reps"] >= n]
        if eligible.empty:
            continue
        best = eligible.sort_values(["load", "reps", "session"], ascending=[False, False, True]).iloc[0]
        rows.append({"reps": n, "load": best["load"], "actual_reps": best["reps"],
                     "weight": best["weight"], "session": best["session"]})
    return pd.DataFrame(rows, columns=["reps", "load", "actual_reps", "weight", "session"])


def _add_pr_event(events, baseline, session, exercise, kind, pr_type, value, previous, detail) -> None:
    """Record a PR, unless this is the exercise's baseline session or there's nothing to beat."""
    if not baseline and previous is not None:
        events.append((session, exercise, kind, pr_type, value, previous, detail))


def pr_events(df: pd.DataFrame, formula: str = "epley") -> pd.DataFrame:
    """Every PR, in order. The first session of each exercise is a baseline, not a PR.

    Types: ``e1rm``, ``weight`` (heaviest load), ``rep`` (a load beating your
    previous best for at least that many reps, up to 15), ``seconds`` (timed),
    ``reps`` (unloaded).

    This walks plain Python rows rather than pandas groups: it runs on every
    analysis command and GUI refresh, and per-group pandas indexing was the
    bottleneck.
    """
    columns = ["session", "exercise", "kind", "pr_type", "value", "previous", "detail"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    d = _with_e1rm(df, formula)
    # exercise → session → rows, keeping first-appearance order of exercises.
    grouped: dict[str, dict] = {}
    for row in zip(d["exercise"], d["session"], d["kind"], d["load"], d["reps"], d["seconds"],
                   d["e1rm"], d["pr_candidate"], strict=True):
        grouped.setdefault(row[0], {}).setdefault(row[1], []).append(row)

    events = []
    for exercise, sessions in grouped.items():
        kind = next(iter(sessions.values()))[0][2]
        best_e1rm = best_load = best_seconds = best_reps = None
        at_least: dict[int, float] = {}  # reps → best load for at least that many reps
        for i, session in enumerate(sorted(sessions)):
            rows = sessions[session]
            baseline = i == 0
            add = functools.partial(_add_pr_event, events, baseline, session, exercise, kind)

            if kind == TIMED:
                v = max(r[5] for r in rows)
                if best_seconds is None or v > best_seconds:
                    add("seconds", v, best_seconds, f"{v:g} s")
                    best_seconds = v
                continue
            if kind == UNLOADED:
                v = max(r[4] for r in rows)
                if best_reps is None or v > best_reps:
                    add("reps", v, best_reps, f"{v:g} reps")
                    best_reps = v
                continue

            cand = [r for r in rows if r[7]]
            if not cand:
                continue
            top = max(cand, key=lambda r: (r[3], r[4]))
            with_e1rm = [r for r in rows if not np.isnan(r[6])]
            if with_e1rm:
                b = max(with_e1rm, key=lambda r: r[6])  # first of equal maxima, like idxmax
                if best_e1rm is None or b[6] > best_e1rm:
                    add("e1rm", b[6], best_e1rm, f"{b[3]:g} lb × {b[4]:g}")
                    best_e1rm = b[6]
            weight_pr_set = None
            if best_load is None or top[3] > best_load:
                add("weight", top[3], best_load, f"{top[3]:g} lb × {top[4]:g}")
                weight_pr_set = (top[3], top[4])
                best_load = top[3]

            # Rep PRs against previous sessions only, one per rep count. A set
            # that another set this session matches or beats on both load and
            # reps isn't reported (140×3 adds nothing next to 140×5), nor is the
            # heaviest-weight PR set itself.
            pairs = [(r[3], r[4]) for r in cand if r[3] > 0]
            rep_prs: dict[int, float] = {}
            for load, reps in pairs:
                n = int(min(reps, REP_MAX_RANGE))
                prev = at_least.get(n)
                dominated = any(
                    (l2, r2) != (load, reps) and l2 >= load and r2 >= reps for l2, r2 in pairs
                )
                if (n >= 1 and prev is not None and load > prev and not dominated
                        and (load, reps) != weight_pr_set):
                    if load > rep_prs.get(n, -np.inf):
                        rep_prs[n] = load
            for n, load in sorted(rep_prs.items()):
                add("rep", load, at_least[n], f"{load:g} lb × {n}{'+' if n == REP_MAX_RANGE else ''}")
            for load, reps in pairs:
                for n in range(1, int(min(reps, REP_MAX_RANGE)) + 1):
                    if load > at_least.get(n, -np.inf):
                        at_least[n] = load
    out = pd.DataFrame(events, columns=columns)
    if not out.empty:
        out["session"] = pd.to_datetime(out["session"])
    return out.sort_values(["session", "exercise"], kind="stable").reset_index(drop=True)


def current_bests(df: pd.DataFrame, formula: str = "epley") -> pd.DataFrame:
    """One row per exercise: best e1RM, heaviest set, longest hold or most reps."""
    d = _with_e1rm(df, formula)
    rows = []
    for exercise, g in d.groupby("exercise"):
        kind = g["kind"].iloc[0]
        row = {"exercise": exercise, "kind": kind, "sessions": g["session"].nunique(),
               "last": g["session"].max(), "e1rm": np.nan, "e1rm_set": "", "e1rm_date": pd.NaT,
               "heaviest": np.nan, "heaviest_set": "", "heaviest_date": pd.NaT,
               "max_seconds": np.nan, "max_reps": np.nan}
        if kind == TIMED:
            row["max_seconds"] = g["seconds"].max()
        elif kind == UNLOADED:
            row["max_reps"] = g["reps"].max()
        else:
            if g["e1rm"].notna().any():
                b = g.loc[g["e1rm"].idxmax()]
                row.update(e1rm=b["e1rm"], e1rm_set=f"{b['load']:g} × {b['reps']:g}", e1rm_date=b["session"])
            top = _top_row(g)
            if top is not None:
                row.update(heaviest=top["load"], heaviest_set=f"{top['load']:g} × {top['reps']:g}",
                           heaviest_date=top["session"])
        rows.append(row)
    return pd.DataFrame(rows)


def exercise_summary(df: pd.DataFrame) -> pd.DataFrame:
    """One row per exercise: sessions, sets, first/last done, average rest."""
    if df.empty:
        return pd.DataFrame(columns=["exercise", "kind", "sessions", "sets", "first", "last", "avg_rest"])
    g = df.groupby("exercise")
    out = pd.DataFrame({
        "kind": g["kind"].first(),
        "sessions": g["session"].nunique(),
        "sets": g.size(),
        "first": g["session"].min(),
        "last": g["session"].max(),
        "avg_rest": g["rest_seconds"].mean(),
    }).reset_index()
    return out


# --- over time ----------------------------------------------------------------

_FREQ = {"week": "W-SUN", "month": "M"}


def _period(ts: pd.Series, by: str) -> pd.Series:
    if by not in _FREQ:
        raise ValueError("by must be 'week' or 'month'")
    return pd.to_datetime(ts).dt.to_period(_FREQ[by])


def sessions_frame(sessions: Iterable[Session]) -> pd.DataFrame:
    rows = [{
        "session": s.started_at,
        "name": s.name,
        "duration_min": s.duration.total_seconds() / 60 if s.duration is not None else np.nan,
        "merged": s.is_merged,
    } for s in sessions]
    df = pd.DataFrame(rows, columns=["session", "name", "duration_min", "merged"])
    df["session"] = pd.to_datetime(df["session"])
    return df


def period_stats(
    sessions_df: pd.DataFrame, sets_df: pd.DataFrame, events: pd.DataFrame, by: str = "week"
) -> pd.DataFrame:
    """Per week or month, including empty periods between the first and last session."""
    cols = ["period", "sessions", "duration_min", "avg_duration_min", "sets", "reps", "volume", "prs"]
    if sessions_df.empty:
        return pd.DataFrame(columns=cols)
    sp = _period(sessions_df["session"], by)
    periods = pd.period_range(sp.min(), sp.max(), freq=sp.dt.freq)
    s = sessions_df.assign(period=sp).groupby("period")
    st = sets_df.assign(period=_period(sets_df["session"], by)).groupby("period")
    out = pd.DataFrame(index=periods)
    out["sessions"] = s.size()
    out["duration_min"] = s["duration_min"].sum(min_count=1)
    out["avg_duration_min"] = s["duration_min"].mean()
    out["sets"] = st.size()
    out["reps"] = st["reps"].sum()
    out["volume"] = st["volume"].sum()
    if events.empty:
        out["prs"] = 0
    else:
        out["prs"] = events.assign(period=_period(events["session"], by)).groupby("period").size()
    for c in ("sessions", "sets", "prs"):
        out[c] = out[c].fillna(0).astype(int)
    for c in ("reps", "volume"):
        out[c] = out[c].fillna(0.0)
    return out.rename_axis("period").reset_index()[cols]


def name_counts(sessions_df: pd.DataFrame) -> pd.DataFrame:
    """Sessions per workout name, with a per-week rate over the whole span."""
    if sessions_df.empty:
        return pd.DataFrame(columns=["name", "sessions", "per_week", "last"])
    weeks = max((sessions_df["session"].max() - sessions_df["session"].min()).days / 7, 1)
    g = sessions_df.groupby("name")
    out = pd.DataFrame({"sessions": g.size(), "last": g["session"].max()})
    out["per_week"] = out["sessions"] / weeks
    return out.reset_index().sort_values("sessions", ascending=False)[["name", "sessions", "per_week", "last"]]


def consistency(sessions_df: pd.DataFrame, today: datetime) -> dict:
    """Frequency, gaps between sessions, and weekly streaks."""
    if sessions_df.empty:
        return {}
    starts = sessions_df["session"].sort_values().reset_index(drop=True)
    days = starts.dt.normalize()
    gaps = days.diff().dt.days.dropna()
    weeks_span = max((starts.iloc[-1] - starts.iloc[0]).days / 7, 1)

    week_set = set(days.dt.to_period("W-SUN"))
    first_week, last_week = min(week_set), max(week_set)
    longest = current = run = 0
    week = first_week
    while week <= last_week:
        run = run + 1 if week in week_set else 0
        longest = max(longest, run)
        week += 1
    this_week = pd.Timestamp(today).to_period("W-SUN")
    # The current streak survives an in-progress week with no session yet.
    w = this_week if this_week in week_set else this_week - 1
    while w in week_set:
        current += 1
        w -= 1

    longest_gap_idx = gaps.idxmax() if not gaps.empty else None
    return {
        "first": starts.iloc[0],
        "last": starts.iloc[-1],
        "sessions": len(starts),
        "per_week": len(starts) / weeks_span,
        "avg_gap_days": gaps.mean() if not gaps.empty else np.nan,
        "longest_gap_days": int(gaps.max()) if not gaps.empty else 0,
        "longest_gap_from": starts.iloc[longest_gap_idx - 1] if longest_gap_idx is not None else None,
        "longest_gap_to": starts.iloc[longest_gap_idx] if longest_gap_idx is not None else None,
        "days_since_last": (pd.Timestamp(today).normalize() - days.iloc[-1]).days,
        "longest_week_streak": longest,
        "current_week_streak": current,
    }


# --- sets per muscle group ------------------------------------------------------

HARD_SET_TYPES = ("normal", "failure", "drop")  # warm-ups never count
PRIMARY_CREDIT, SECONDARY_CREDIT = 1.0, 0.5


def weekly_muscle_sets(sets_df: pd.DataFrame, catalog: Catalog, by: str = "week") -> pd.DataFrame:
    """Hard sets per muscle per week (``by="week"``, W-SUN periods) or month (``"M"``).

    Columns: ``period`` (pandas Period), ``muscle`` (str), ``sets`` (float).
    Each normal, failure or drop set credits 1.0 to every primary muscle of its
    exercise and 0.5 to every secondary one (``catalog.muscles``: the explicit
    mapping, else a keyword guess). Periods between the first and last session
    with no sets for a muscle are included as 0, for every muscle that appears
    at all. Exercises with no muscles (mapped or guessed) don't count.
    """
    cols = ["period", "muscle", "sets"]
    empty = pd.DataFrame({"period": pd.PeriodIndex([], freq=_FREQ.get(by, "W-SUN")),
                          "muscle": pd.Series([], dtype=object), "sets": pd.Series([], dtype=float)})
    if sets_df.empty:
        _period(sets_df["session"], by)  # validates ``by``
        return empty
    all_periods = _period(sets_df["session"], by)
    hard = sets_df[sets_df["set_type"].isin(HARD_SET_TYPES)]
    credit = muscle_credits(catalog, hard["exercise"].unique())
    if hard.empty or credit.empty:
        return empty
    counts = (hard.assign(period=_period(hard["session"], by))
              .groupby(["period", "exercise"]).size().rename("n").reset_index())
    rows = counts.merge(credit, on="exercise")
    rows["sets"] = rows["n"] * rows["credit"]
    totals = rows.groupby(["period", "muscle"])["sets"].sum()
    periods = pd.period_range(all_periods.min(), all_periods.max(), freq=all_periods.dt.freq)
    muscles = _muscle_order(totals.index.get_level_values("muscle").unique())
    grid = pd.MultiIndex.from_product([periods, muscles], names=["period", "muscle"])
    out = totals.reindex(grid, fill_value=0.0).astype(float).reset_index()
    return out[cols]


def muscle_credits(catalog: Catalog, exercises: Iterable[str]) -> pd.DataFrame:
    """One row per (exercise, muscle) with its per-set credit (1.0 primary, 0.5 secondary)."""
    rows = []
    for name in exercises:
        primary, secondary = catalog.muscles(name)
        for m in dict.fromkeys(primary):
            rows.append({"exercise": name, "muscle": m, "credit": PRIMARY_CREDIT})
        for m in dict.fromkeys(secondary):
            if m not in primary:
                rows.append({"exercise": name, "muscle": m, "credit": SECONDARY_CREDIT})
    return pd.DataFrame(rows, columns=["exercise", "muscle", "credit"])


def _muscle_order(muscles: Iterable[str]) -> list[str]:
    from .catalog import MUSCLES

    order = {m: i for i, m in enumerate(MUSCLES)}
    return sorted(muscles, key=lambda m: (order.get(m, len(order)), m))
