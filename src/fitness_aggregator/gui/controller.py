"""Data loading and actions for the desktop app.

No Qt here: the controller loads everything the tabs show once per refresh
(``reload``) and runs the same ``Store`` calls as the CLI commands. Widgets
ask it for plain rows, text and DataFrames.
"""

from __future__ import annotations

import html
import math
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .. import corrections as corr
from .. import merges, metrics, outliers, similarity
from .. import settings as settings_mod
from ..bodyweight import Bodyweight, BodyweightPlan, DayValue
from ..catalog import ASSISTED, LOADED, TIMED, UNLOADED, Catalog
from ..corrections import Correction, ReconcileResult, format_duration
from ..fixes import describe_set
from ..importers import parse_strong_csv
from ..importers.apple_health import HealthParseError, HealthResult, parse_apple_health
from ..models import SetType, WorkoutSet
from ..sessions import Session
from ..settings import Settings
from ..store import ImportPlan, ImportReport, Store, default_db_path

PR_LABELS = {"e1rm": "e1RM", "weight": "Heaviest", "rep": "Rep PR", "seconds": "Longest hold", "reps": "Most reps"}
SET_LABELS = {SetType.WARMUP: "W", SetType.DROP: "D", SetType.FAILURE: "F"}
TYPE_LABELS = {
    corr.WORKOUT_MERGE: "Workout merge",
    corr.DURATION_SUSPECT: "Duration excluded",
    corr.VALUE_FIX: "Set fix",
    corr.EXERCISE_MERGE: "Exercise combined",
}
TIER_TEXT = {
    similarity.LIKELY: "Likely the same (names match apart from numbering or plurals)",
    similarity.POSSIBLE: "Possibly the same (similar names)",
}
RECENT_PR_DAYS = 30
LOW_SETS, HIGH_SETS = 10, 20  # weekly hard sets, as ``fitness muscles`` and the sets-per-muscle chart
MUSCLE_WEEKS = 12
HEALTH_ERRORS = (HealthParseError, zipfile.BadZipFile, OSError)


# --- small formatting helpers --------------------------------------------------

def num(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:g}"


def lb(x, decimals: int | None = None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:,.{decimals}f} lb" if decimals is not None else f"{x:,g} lb"


def date(ts) -> str:
    return "" if ts is None or pd.isna(ts) else f"{pd.Timestamp(ts):%Y-%m-%d}"


def when(dt) -> str:
    return f"{pd.Timestamp(dt):%a %Y-%m-%d %H:%M}"


def rest(seconds: float | None) -> str:
    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)):
        return ""
    minutes, secs = divmod(int(seconds), 60)
    if minutes and secs:
        return f"{minutes}m {secs}s"
    return f"{minutes}m" if minutes else f"{secs}s"


def set_label(s: WorkoutSet) -> str:
    return str(s.number) if s.set_type is SetType.NORMAL else SET_LABELS[s.set_type]


def pr_value(pr_type: str, value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if pr_type == "e1rm":
        return lb(value, 0)
    if pr_type == "seconds":
        return f"{value:g} s"
    if pr_type == "reps":
        return f"{value:g} reps"
    return lb(value)


def session_duration(session: Session) -> str:
    if session.duration is not None:
        return format_duration(session.duration)
    recorded = [m.duration for m in session.members if m.duration is not None]
    if recorded:
        return f"({format_duration(sum(recorded, start=timedelta()))})"
    return "—"


def parse_adjustment(text: str) -> tuple[float | None, float | None]:
    """'-50' → offset −50 lb; 'x0.75' → factor 0.75; '0' or blank → none. Raises ValueError."""
    t = text.strip().lower().replace("lb", "").replace(" ", "").replace("−", "-")
    if t in ("", "0", "none", "+0", "-0"):
        return None, None
    if t[0] in "x*×":
        return None, float(t[1:])
    return float(t), None


def describe_reconcile(result: ReconcileResult, ids: dict | None = None) -> list[str]:
    """One line per knock-on change, like the CLI's report."""
    ids = ids or {}
    lines = []
    for p in result.insert:
        lines.append(f"+ #{ids.get(p.key, '?')} {p.reason}")
    for current, p in result.reactivate:
        lines.append(f"↺ #{current.id} applies again: {p.reason}")
    for current, p in result.update:
        note = " (still reverted)" if current.status == corr.REVERTED else ""
        lines.append(f"~ #{current.id} updated{note}: {p.reason}")
    for current in result.obsolete:
        lines.append(f"− #{current.id} no longer applies: {current.reason}")
    for current in result.stale:
        lines.append(f"! #{current.id} is stale (the imported data changed): {current.reason}")
    for current in result.unstale:
        lines.append(f"↺ #{current.id} matches the data again: {current.reason}")
    return lines


def describe_plan(plan: ImportPlan) -> str:
    s = plan.result.stats
    lines = [f"{Path(plan.source_path).name}: {s.workouts:,} workouts, {s.sets:,} sets, "
             f"{s.exercise_names:,} exercise names."]
    lines += [f"warning: {w}" for w in plan.result.warnings]
    if not plan.has_changes:
        lines.append(f"No changes: this export matches the stored data ({plan.unchanged:,} workouts).")
        return "\n".join(lines)
    if plan.is_first_import:
        lines.append(f"First import: {len(plan.new):,} workouts.")
    else:
        lines.append(f"{len(plan.new)} new, {len(plan.edited)} edited, {len(plan.removed)} removed, "
                     f"{plan.unchanged:,} unchanged.")
        for w in plan.new[:20]:
            lines.append(f"  new      {when(w.started_at)}  {w.name}")
        for e in plan.edited[:20]:
            lines.append(f"  edited   {when(e.new.started_at)}  {e.new.name}: {e.description}")
        for w in plan.removed[:20]:
            lines.append(f"  removed  {when(w.started_at)}  {w.name} (deleted in Strong?)")
    return "\n".join(lines)


# --- review helpers -------------------------------------------------------------

@dataclass
class Member:
    name: str
    sets: list[similarity.TopSet]

    @property
    def sessions(self) -> int:
        return len(self.sets)

    @property
    def span(self) -> str:
        if not self.sets:
            return "no working sets"
        return f"{self.sets[0].when:%Y-%m-%d} → {self.sets[-1].when:%Y-%m-%d}"


@dataclass
class CandidateDetail:
    group: similarity.CandidateGroup
    members: list[Member]
    kept: str
    kind: str
    suggestions: dict[str, similarity.Suggestion] = field(default_factory=dict)

    @property
    def offset_allowed(self) -> bool:
        return self.kind in (LOADED, ASSISTED)

    def suggested_text(self, name: str) -> str:
        s = self.suggestions.get(name)
        return "" if s is None or s.offset is None else f"{s.offset:+g}"


def default_kept(members: list[Member]) -> str:
    """The name to keep by default: most sessions, then the one used first (as in the CLI)."""
    def rank(m: Member):
        return (-m.sessions, m.sets[0].when if m.sets else datetime.max)
    return min(members, key=rank).name


# --- the snapshot ----------------------------------------------------------------

@dataclass
class Snapshot:
    """Everything the tabs show, loaded once per refresh."""

    sessions: list[Session]
    catalog: Catalog
    bodyweight: Bodyweight
    df: pd.DataFrame
    sessions_df: pd.DataFrame
    events: pd.DataFrame
    bests: pd.DataFrame
    summary: pd.DataFrame
    consistency: dict
    weekly: pd.DataFrame
    flags: list[outliers.Flag]
    candidates: list[similarity.CandidateGroup]
    corrections: list[Correction]
    active_merges: dict[str, merges.Merge]
    muscle_sets: pd.DataFrame
    bodyweight_days: list[DayValue]
    last_health_import: dict | None = None

    @property
    def is_empty(self) -> bool:
        return not self.sessions


def build_snapshot(store: Store, settings: Settings, today: datetime, formula: str = "epley") -> Snapshot:
    """Load everything the tabs show. Only reads ``store``, so it can run on a worker
    thread with its own ``Store`` connection (see ``Controller.load_snapshot``)."""
    workouts = store.load_workouts()
    catalog = Catalog.build(workouts)
    sessions = store.sessions()
    bodyweight = Bodyweight(settings.bodyweight_lb, store.bodyweight_entries())
    df = metrics.sets_frame(sessions, catalog, bodyweight)
    sessions_df = metrics.sessions_frame(sessions)
    if df.empty:
        events = bests = pd.DataFrame()
        summary = metrics.exercise_summary(df)
    else:
        events = metrics.pr_events(df, formula)
        bests = metrics.current_bests(df, formula)
        summary = metrics.exercise_summary(df).merge(
            bests[["exercise", "e1rm", "max_seconds", "max_reps"]], on="exercise")
    return Snapshot(
        sessions=sessions,
        catalog=catalog,
        bodyweight=bodyweight,
        df=df,
        sessions_df=sessions_df,
        events=events,
        bests=bests,
        summary=summary,
        consistency=metrics.consistency(sessions_df, today) if not sessions_df.empty else {},
        weekly=metrics.period_stats(sessions_df, df, events, "week") if not sessions_df.empty else pd.DataFrame(),
        flags=store.open_flags(catalog),
        candidates=store.merge_candidates(),
        corrections=store.corrections(),
        active_merges=store.active_merges(),
        muscle_sets=metrics.weekly_muscle_sets(df, catalog) if not df.empty else pd.DataFrame(
            columns=["period", "muscle", "sets"]),
        bodyweight_days=store.bodyweight_days(),
        last_health_import=store.last_health_import(),
    )


class Controller:
    def __init__(self, store: Store, settings: Settings, *, today: datetime | None = None,
                 formula: str = "epley"):
        self.store = store
        self.settings = settings
        self._today = today
        self.formula = formula
        self.snapshot: Snapshot | None = None
        self._history_cache: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}

    @classmethod
    def open(cls, db: str | Path | None = None, config: str | Path | None = None, **kwargs) -> "Controller":
        """Same database and settings resolution as the CLI."""
        return cls(Store(db or default_db_path()), settings_mod.load(config), **kwargs)

    @property
    def today(self) -> datetime:
        return self._today or datetime.now()

    def close(self) -> None:
        self.store.close()

    # --- loading ---------------------------------------------------------------

    def reload(self) -> Snapshot:
        """Reload synchronously on this thread's connection."""
        return self.set_snapshot(build_snapshot(self.store, self.settings, self.today, self.formula))

    @property
    def can_load_elsewhere(self) -> bool:
        """False for an in-memory database, which another connection can't see."""
        return str(self.store.path) != ":memory:"

    def load_snapshot(self) -> Snapshot:
        """Build a snapshot on a fresh connection (safe on a worker thread). Doesn't install it."""
        if not self.can_load_elsewhere:
            return build_snapshot(self.store, self.settings, self.today, self.formula)
        with Store(self.store.path) as store:
            return build_snapshot(store, self.settings, self.today, self.formula)

    def set_snapshot(self, snapshot: Snapshot) -> Snapshot:
        self.snapshot = snapshot
        self._history_cache.clear()
        return snapshot

    @property
    def snap(self) -> Snapshot:
        return self.snapshot or self.reload()

    # --- overview ----------------------------------------------------------------

    def headline(self) -> dict[str, str]:
        c = self.snap.consistency
        if not c:
            return {"Sessions": "0", "Per week": "—", "Weekly streak": "—", "Days since last": "—",
                    "Total volume": "—"}
        return {
            "Sessions": f"{c['sessions']:,}",
            "Per week": f"{c['per_week']:.1f}",
            "Weekly streak": f"{c['current_week_streak']} (best {c['longest_week_streak']})",
            "Days since last": f"{c['days_since_last']}",
            "Total volume": f"{self.snap.df['volume'].sum():,.0f} lb",
        }

    def date_range_text(self) -> str:
        c = self.snap.consistency
        if not c:
            return "No workouts yet. Use File → Import Strong export…"
        return (f"{when(c['first'])} → {when(c['last'])}  ·  {c['avg_gap_days']:.1f} days between sessions "
                f"on average  ·  longest gap {c['longest_gap_days']} days")

    # --- workouts ----------------------------------------------------------------

    def session_rows(self) -> list[dict]:
        rows = []
        for i, s in enumerate(self.snap.sessions):
            flags = []
            if s.is_merged:
                flags.append(f"merged ×{len(s.members)}")
            if any(c.type == corr.DURATION_SUSPECT for c in s.applied):
                flags.append("duration excluded")
            if any(c.type == corr.VALUE_FIX for c in s.applied):
                flags.append("set fixes")
            if any(e.merged_from for e in s.exercises):
                flags.append("combined exercises")
            rows.append({
                "index": i,
                "date": s.started_at,
                "name": s.name,
                "duration": session_duration(s),
                "duration_s": s.duration.total_seconds() if s.duration is not None else -1,
                "exercises": len(s.exercises),
                "sets": s.set_count,
                "flags": ", ".join(flags),
            })
        return rows

    def session_corrections(self, session: Session) -> list[Correction]:
        by_id = {c.id: c for c in self.snap.corrections}
        applied = list(session.applied)
        merge_ids = sorted({e.merge_correction_id for e in session.exercises if e.merge_correction_id})
        applied += [by_id[i] for i in merge_ids if i in by_id]
        return applied

    def session_html(self, index: int) -> str:
        """Set-by-set detail of one session, as simple HTML for a QTextBrowser."""
        session = self.snap.sessions[index]
        e = html.escape
        out = [f"<h2 style='margin-bottom:2px'>{e(session.name)}</h2>",
               f"<p style='color:#52514e;margin-top:0'>{e(when(session.started_at))} · "
               f"{e(session_duration(session))} · {len(session.exercises)} exercises, {session.set_count} sets"]
        if session.is_merged:
            out.append(f" · <span style='color:#a15c00'>{len(session.members)} workouts merged</span>")
        out.append("</p>")
        if session.notes:
            out.append(f"<p><i>{e(session.notes)}</i></p>")
        for member in session.members:
            if session.is_merged:
                out.append(f"<p style='color:#52514e'>── {member.started_at:%H:%M} workout "
                           f"({e(format_duration(member.duration))}) ──</p>")
            for entry in member.exercises:
                heading = f"<b>{e(entry.name)}</b>"
                if entry.merged_from:
                    adj = f", {entry.merge_label}" if entry.merge_label else ""
                    heading += f" <span style='color:#52514e'>(recorded as {e(entry.merged_from)}{e(adj)})</span>"
                out.append(f"<p style='margin-top:10px;margin-bottom:2px'>{heading}</p>")
                out.append("<table cellspacing='0' cellpadding='2'>")
                for s in entry.sets:
                    if s.seconds and not s.weight and not s.reps:
                        weight, amount = "", f"{num(s.seconds)} s"
                    elif s.weight:
                        weight, amount = f"{num(s.weight)} lb", f"× {num(s.reps)}"
                    else:
                        weight, amount = "—", f"× {num(s.reps)}"
                    notes = [e(s.notes)] if s.notes else []
                    if s.pre_merge_weight is not None and s.pre_merge_weight != s.weight:
                        notes.append(f"<span style='color:#52514e'>recorded {num(s.pre_merge_weight)} lb</span>")
                    if s.corrected_from:
                        ids = ", ".join(f"#{i}" for i in s.correction_ids)
                        notes.append(f"<span style='color:#a15c00'>was {e(describe_set(s, s.corrected_from))} "
                                     f"(correction {ids})</span>")
                        amount += " *"
                    rpe = "" if s.rpe is None else f"RPE {num(s.rpe)}"
                    out.append(
                        f"<tr><td align='right' style='color:#6f6e69' width='28'>{set_label(s)}</td>"
                        f"<td align='right' width='70'>{weight}</td><td width='60'>{amount}</td>"
                        f"<td style='color:#6f6e69' width='60'>{rest(s.rest_seconds)}</td>"
                        f"<td width='50'>{rpe}</td><td>{'; '.join(notes)}</td></tr>")
                out.append("</table>")
        applied = self.session_corrections(session)
        if applied:
            out.append("<p style='margin-top:14px'><b style='color:#a15c00'>Corrections applied</b></p><ul>")
            out += [f"<li>#{c.id} {e(c.reason)}</li>" for c in applied]
            out.append("</ul>")
        return "".join(out)

    # --- exercises ---------------------------------------------------------------

    def exercise_rows(self) -> list[dict]:
        rows = []
        now = pd.Timestamp(self.today).normalize()
        for r in self.snap.summary.itertuples():
            if r.kind == TIMED:
                best, best_v = f"{r.max_seconds:g} s", r.max_seconds
            elif r.kind == UNLOADED:
                best, best_v = f"{r.max_reps:g} reps", r.max_reps
            else:
                best = "—" if math.isnan(r.e1rm) else f"{r.e1rm:,.0f}"
                best_v = -1 if math.isnan(r.e1rm) else r.e1rm
            rows.append({"exercise": r.exercise, "kind": r.kind, "sessions": r.sessions, "sets": r.sets,
                         "last": r.last, "days": (now - pd.Timestamp(r.last).normalize()).days,
                         "best": best, "best_v": best_v})
        return rows

    def history(self, exercise: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        """(exercise_history, pr_events) for one exercise, cached until the next reload."""
        if exercise not in self._history_cache:
            d = self.snap.df[self.snap.df["exercise"] == exercise]
            # pr_events works per exercise, so the full-history events can simply be filtered.
            events = self.snap.events
            events = events[events["exercise"] == exercise] if not events.empty else events
            self._history_cache[exercise] = (metrics.exercise_history(d, exercise, self.formula),
                                             events.reset_index(drop=True))
        return self._history_cache[exercise]

    def kind(self, exercise: str) -> str:
        d = self.snap.df.loc[self.snap.df["exercise"] == exercise, "kind"]
        return d.iloc[0] if not d.empty else LOADED

    def history_rows(self, exercise: str) -> list[dict]:
        hist, events = self.history(exercise)
        prs_by_session = events.groupby("session")["pr_type"].apply(list).to_dict() if not events.empty else {}
        rows = []
        for r in hist.itertuples():
            counts = Counter(prs_by_session.get(r.session, []))
            prs = ", ".join(f"{PR_LABELS[t]} ×{n}" if n > 1 else PR_LABELS[t] for t, n in counts.items())
            if r.kind == TIMED:
                top, top_v = f"{r.max_seconds:g} s", r.max_seconds
            elif r.kind == UNLOADED:
                top, top_v = f"{r.max_reps:g} reps", r.max_reps
            elif math.isnan(r.top_load):
                top, top_v = "—", -1
            elif r.kind == ASSISTED:
                top, top_v = f"{num(r.top_weight)} lb assist × {num(r.top_reps)} (= {num(r.top_load)} lb)", r.top_load
            else:
                top, top_v = f"{num(r.top_load)} lb × {num(r.top_reps)}", r.top_load
            notes = []
            if r.corrected:
                notes.append("corrected")
            if r.merged_from:
                notes.append(f"recorded as {r.merged_from}" + (f" ({r.merge_label})" if r.merge_label else ""))
            rows.append({"date": r.session, "top": top, "top_v": top_v, "e1rm": r.e1rm, "sets": r.sets,
                         "reps": r.reps, "volume": r.volume, "prs": prs, "notes": "; ".join(notes)})
        return rows

    # --- PRs ------------------------------------------------------------------------

    def bests_rows(self) -> list[dict]:
        rows = []
        for r in self.snap.bests.itertuples():
            if r.kind == TIMED:
                e1, e1set, e1date, heavy, hdate = "—", f"{r.max_seconds:g} s hold", "", "", ""
            elif r.kind == UNLOADED:
                e1, e1set, e1date, heavy, hdate = "—", f"{r.max_reps:g} reps", "", "", ""
            else:
                e1 = "—" if math.isnan(r.e1rm) else f"{r.e1rm:,.0f}"
                e1set, e1date, heavy, hdate = r.e1rm_set, date(r.e1rm_date), r.heaviest_set, date(r.heaviest_date)
            rows.append({"exercise": r.exercise, "e1rm": e1, "e1rm_v": -1 if math.isnan(r.e1rm) else r.e1rm,
                         "e1rm_set": e1set, "e1rm_date": e1date, "heaviest": heavy, "heaviest_date": hdate,
                         "sessions": r.sessions, "last": r.last})
        return rows

    def recent_pr_rows(self, days: int = RECENT_PR_DAYS) -> list[dict]:
        events = self.snap.events
        if events.empty:
            return []
        cutoff = pd.Timestamp(self.today) - pd.Timedelta(days=days)
        recent = events[events["session"] >= cutoff].sort_values("session", ascending=False, kind="stable")
        return [{"date": r.session, "exercise": r.exercise, "pr": PR_LABELS[r.pr_type], "detail": r.detail,
                 "new": pr_value(r.pr_type, r.value), "previous": pr_value(r.pr_type, r.previous),
                 "value": r.value}
                for r in recent.itertuples()]

    # --- corrections -------------------------------------------------------------

    def correction_rows(self, include_obsolete: bool = False) -> list[dict]:
        return [{"id": c.id, "type": TYPE_LABELS.get(c.type, c.type), "source": c.source, "status": c.status,
                 "reason": c.reason, "updated": c.updated_at, "correction": c}
                for c in self.snap.corrections if include_obsolete or c.status != corr.OBSOLETE]

    def set_correction_status(self, correction_id: int, status: str) -> str:
        """Revert or restore; returns a message including knock-on changes. Raises ValueError/KeyError."""
        current = self.store.get_correction(correction_id)
        if current is None:
            raise KeyError(f"no correction #{correction_id}")
        if current.status == status:
            return f"Correction #{correction_id} is already {status}."
        updated, knock_on = self.store.set_correction_status(correction_id, status, self.settings)
        verb = "Reverted" if status == corr.REVERTED else "Restored"
        lines = [f"{verb} #{updated.id}: {updated.reason}"]
        if knock_on.changed:
            ids = {c.key: c.id for c in self.store.corrections()}
            lines += ["", "Knock-on changes:"] + describe_reconcile(knock_on, ids)
        return "\n".join(lines)

    # --- data-entry flags ----------------------------------------------------------

    def flag_rows(self) -> list[dict]:
        return [{"key": f.key, "date": f.workout.started_at, "workout": f.workout.name,
                 "exercise": f.entry.name, "set": set_label(f.set), "recorded": describe_set(f.set),
                 "message": f.message, "swap": "yes" if f.swap_plausible else "",
                 "suggested": f"{f.set.reps:g} lb × {f.set.weight:g}" if f.swap_plausible else ""}
                for f in self.snap.flags]

    def flag(self, key: str) -> outliers.Flag:
        for f in self.snap.flags:
            if f.key == key:
                return f
        raise KeyError(f"no open flag {key!r}")

    def dismiss_flags(self, keys: list[str]) -> str:
        self.store.dismiss_flags(keys)
        return f"Dismissed {len(keys)} flag(s); they won't be raised again."

    def _fix_flags(self, keys: list[str], values) -> str:
        flags = [self.flag(k) for k in keys]
        lines = []
        for g in outliers.group(flags):
            changes = {f.set.position: values(f) for f in g.flags}
            c = self.store.add_value_fix(g.workout, g.entry.name, g.occurrence, changes)
            lines.append(f"Recorded correction #{c.id}: {c.reason}")
        return "\n".join(lines)

    def swap_flags(self, keys: list[str]) -> str:
        """Swap weight and reps on these flagged sets (one correction per exercise entry)."""
        return self._fix_flags(keys, lambda f: {"weight": f.set.reps, "reps": f.set.weight})

    def edit_flag(self, key: str, weight: float, reps: float) -> str:
        return self._fix_flags([key], lambda f: {"weight": float(weight), "reps": float(reps)})

    # --- similar exercises -----------------------------------------------------------

    def candidate_detail(self, index: int, kept: str | None = None) -> CandidateDetail:
        group = self.snap.candidates[index]
        members = [Member(n, self.store.top_sets(n)) for n in group.names]
        kept = kept if kept in group.names else default_kept(members)
        kept_m = next(m for m in members if m.name == kept)
        kind = self.snap.catalog.kind(kept)
        detail = CandidateDetail(group, members, kept, kind)
        for m in members:
            if m.name != kept:
                detail.suggestions[m.name] = similarity.suggest_offset(kept, kept_m.sets, m.name, m.sets, kind)
        return detail

    def candidate_rows(self) -> list[dict]:
        rows = []
        for i, g in enumerate(self.snap.candidates):
            rows.append({"index": i, "names": "  ·  ".join(g.names), "tier": g.tier, "count": len(g.names)})
        return rows

    def combine(self, index: int, kept: str, adjustments: dict[str, str] | None = None,
                include: list[str] | None = None) -> str:
        """Combine group members into ``kept`` (same calls as ``fitness merge review``).

        ``adjustments`` maps a member name to text like ``-50`` or ``x0.75``
        (missing or blank: combine as-is). Members not in ``include`` are
        recorded as separate from ``kept``. Raises ValueError for bad input.
        """
        detail = self.candidate_detail(index, kept)
        adjustments = adjustments or {}
        include = list(include) if include is not None else list(detail.group.names)
        if kept not in include:
            include.append(kept)
        if len(include) < 2:
            raise ValueError("pick at least two exercises to combine")
        parsed = {}
        for name in include:
            if name == kept:
                continue
            try:
                offset, factor = parse_adjustment(adjustments.get(name, ""))
            except ValueError:
                raise ValueError(f"adjustment for {name} must be lb like -50 or a factor like x0.75") from None
            if (offset is not None or factor is not None) and not detail.offset_allowed:
                raise ValueError(f"{kept} has no weights to adjust; combine as-is instead")
            if factor is not None and detail.kind != LOADED:
                raise ValueError("a factor only works for loaded exercises; use lb")
            parsed[name] = (offset, factor)
        lines = []
        for name, (offset, factor) in parsed.items():
            c = self.store.add_exercise_merge(name, kept, offset=offset, factor=factor)
            lines.append(f"Recorded correction #{c.id}: {c.reason}")
        for name in detail.group.names:
            if name not in include:
                self.store.keep_separate([name, kept])
                lines.append(f"Kept {name} separate from {kept}.")
        return "\n".join(lines)

    def keep_separate(self, index: int) -> str:
        names = self.snap.candidates[index].names
        self.store.keep_separate(names)
        return f"Keeping {', '.join(names)} separate; you won't be asked again."

    # --- importing -----------------------------------------------------------------------

    def plan_import(self, path: str | Path) -> ImportPlan:
        """Parse an export and work out what would change. Raises StrongParseError."""
        return self.plan_parsed(self.parse_export(path), path)

    @staticmethod
    def parse_export(path: str | Path):
        """Parse a Strong export without touching the database (safe on a worker thread)."""
        return parse_strong_csv(path)

    def plan_parsed(self, parsed, path: str | Path) -> ImportPlan:
        return self.store.plan_import(parsed, path)

    def apply_import(self, plan: ImportPlan, *, allow_removals: bool, force: bool) -> tuple[ImportReport, str]:
        report = self.store.apply_import(plan, self.settings, allow_removals=allow_removals, force=force)
        lines = [f"Imported. {self.store.workout_count():,} workouts stored."]
        if report.corrections.changed:
            ids = {c.key: c.id for c in self.store.corrections()}
            lines += ["", "Corrections changed by this import:"] + describe_reconcile(report.corrections, ids)
        return report, "\n".join(lines)

    # --- muscles -----------------------------------------------------------------------------

    def muscle_table(self, last: int = MUSCLE_WEEKS) -> tuple[list[pd.Period], list[dict]]:
        """(the last ``last`` weeks, one row per muscle) from ``metrics.weekly_muscle_sets``.

        Each row has ``muscle``, ``label``, ``avg`` (over the complete weeks shown) and, per
        period ``p``, ``row[p]`` = sets and ``row[("status", p)]`` = ``zero``, ``partial``
        (the week is still in progress), ``low`` (< 10), ``high`` (> 20) or ``ok``.
        """
        from ..catalog import MUSCLES

        wms = self.snap.muscle_sets
        if wms is None or wms.empty:
            return [], []
        grid = wms.pivot(index="muscle", columns="period", values="sets")
        order = [m for m in MUSCLES if m in grid.index] + [m for m in grid.index if m not in MUSCLES]
        grid = grid.loc[order]
        periods = list(grid.columns)[-last:] if last > 0 else list(grid.columns)
        today = pd.Timestamp(self.today)
        partial = {p for p in periods if p.start_time <= today <= p.end_time}
        complete = [p for p in periods if p not in partial] or periods
        rows = []
        for muscle, values in grid.iterrows():
            row = {"muscle": muscle, "label": muscle.replace("_", " "), "avg": float(values[complete].mean())}
            for p in periods:
                v = float(values[p])
                row[p] = v
                row[("status", p)] = ("zero" if v == 0 else "partial" if p in partial
                                      else "low" if v < LOW_SETS else "high" if v > HIGH_SETS else "ok")
            rows.append(row)
        return periods, rows

    def muscle_notes(self) -> list[str]:
        """Which exercises' muscles are guessed, and which aren't counted, as in ``fitness muscles``."""
        df = self.snap.df
        if df.empty:
            return []
        catalog = self.snap.catalog
        names = sorted(set(df.loc[df["set_type"].isin(metrics.HARD_SET_TYPES), "exercise"]))
        guessed = [n for n in names if catalog.is_guessed(n)]
        uncounted = [n for n in names if not catalog.has_mapping(n) and not catalog.is_guessed(n)]
        notes = []
        if guessed:
            notes.append(f"Muscles are guessed from the name for {len(guessed)} exercise(s) not in "
                         "config/exercises.toml (fitness exercises --unmapped lists them).")
        if uncounted:
            notes.append(f"Not counted (no muscles known): {', '.join(uncounted)}.")
        return notes

    # --- bodyweight ------------------------------------------------------------------------------

    def bodyweight_rows(self) -> list[dict]:
        days = self.snap.bodyweight_days
        rows = []
        for i, d in enumerate(days):
            change = None if i == 0 else d.lb - days[i - 1].lb
            rows.append({"date": pd.Timestamp(d.day), "lb": d.lb, "change": change, "source": d.source})
        return rows

    def bodyweight_summary(self) -> str:
        """Rich text for the Bodyweight tab, like ``fitness bodyweight``."""
        days = self.snap.bodyweight_days
        placeholder = self.settings.bodyweight_lb
        if not days:
            return (f"No bodyweight entries yet. Assisted-lift loads use the placeholder <b>{placeholder:g} lb</b> "
                    "(change it with <code>fitness config set bodyweight_lb N</code>).<br>"
                    "Import real weights with File → Import Apple Health export…")
        lo, hi, latest = min(days, key=lambda d: d.lb), max(days, key=lambda d: d.lb), days[-1]
        lines = [f"<b>{len(days):,} days</b> of bodyweight, {days[0].day} → {latest.day}. "
                 f"Latest <b>{latest.lb:,.1f} lb</b>. Lowest {lo.lb:,.1f} lb ({lo.day}), "
                 f"highest {hi.lb:,.1f} lb ({hi.day})."]
        trends = [f"{c:+.1f} lb vs {n} days earlier" for n in (30, 90, 365)
                  if (c := self.snap.bodyweight.change_over(n)) is not None]
        if trends:
            lines.append("Latest " + ", ".join(trends) + ".")
        note = (f"Assisted-lift loads use the latest entry on or before each workout, not the "
                f"{placeholder:g} lb placeholder.")
        info = self.snap.last_health_import
        if info:
            note += f" Last imported {info['imported_at'][:10]} from {html.escape(info['source_path'])}."
        lines.append(f"<span style='color:#52514e'>{note}</span>")
        return "<br>".join(lines)

    def plan_health_import(self, path: str | Path) -> tuple[HealthResult, BodyweightPlan]:
        """Parse an Apple Health export and compare it with stored bodyweight.

        Raises HealthParseError, zipfile.BadZipFile or OSError, and ValueError when the
        export has no usable bodyweight readings.
        """
        result = self.parse_health(path)
        return result, self.plan_health(result, path)

    @staticmethod
    def parse_health(path: str | Path) -> HealthResult:
        """Parse an Apple Health export without touching the database (safe on a worker thread)."""
        result = parse_apple_health(path)
        if not result.days:
            raise ValueError("no usable bodyweight readings in this export; nothing imported.")
        return result

    def plan_health(self, result: HealthResult, path: str | Path) -> BodyweightPlan:
        return self.store.plan_bodyweight_import(result.days, path)

    @staticmethod
    def describe_health_plan(result: HealthResult, plan: BodyweightPlan, limit: int = 10) -> str:
        """The same summary as ``fitness import-health``, as plain text."""
        s = result.stats
        units = ", ".join(f"{n:,} in {u}" for u, n in sorted(s.units.items()))
        line = f"{Path(plan.source_path).name}: {s.records:,} body-mass records"
        if units:
            line += f" ({units})"
        line += f" on {len(result.days):,} days, {s.first} → {s.last}; {s.other_records:,} other records ignored."
        lines = [line]
        if s.invalid:
            lines.append(f"{s.invalid:,} record(s) skipped:")
            lines += [f"  {w}" for w in result.warnings]
        multi = sum(1 for d in result.days if d.readings > 1)
        if multi:
            lines.append(f"{multi:,} day(s) with several readings use that day's average.")
        if not plan.has_changes:
            lines.append(f"No changes: this export matches the stored bodyweight ({plan.unchanged:,} days).")
            return "\n".join(lines)
        if plan.is_first_import:
            lines.append(f"First Health import: {len(plan.new):,} days of bodyweight.")
            return "\n".join(lines)
        lines.append(f"{len(plan.new)} new, {len(plan.changed)} changed, {len(plan.removed)} removed, "
                     f"{plan.unchanged:,} unchanged.")
        lines += [f"  new      {d.day}  {d.lb:,.1f} lb" for d in plan.new[-limit:]]
        lines += [f"  changed  {new.day}  {old.lb:,.1f} → {new.lb:,.1f} lb" for old, new in plan.changed[:limit]]
        lines += [f"  removed  {d.day}  {d.lb:,.1f} lb (deleted in Health?)" for d in plan.removed[:limit]]
        hidden = sum(max(len(xs) - limit, 0) for xs in (plan.new, plan.changed, plan.removed))
        if hidden:
            lines.append(f"  … and {hidden} more")
        return "\n".join(lines)

    def apply_health_import(self, plan: BodyweightPlan) -> str:
        if not self.store.apply_bodyweight_import(plan):
            return "No changes."
        first, latest = plan.incoming[0], plan.incoming[-1]
        return (f"Imported. {len(plan.incoming):,} days of bodyweight stored ({first.day} → {latest.day}, "
                f"latest {latest.lb:,.1f} lb). Assisted-lift loads now use these entries instead of the placeholder.")

    # --- misc ------------------------------------------------------------------------------------

    def exercise_names(self) -> list[str]:
        """Exercises, most sessions first (for pickers)."""
        summary = self.snap.summary
        if summary.empty:
            return []
        return list(summary.sort_values(["sessions", "exercise"], ascending=[False, True])["exercise"])

    def about_text(self) -> str:
        from .. import __version__

        snap = self.snapshot
        counts = ""
        if snap is not None:
            counts = (f"<p>{len(snap.sessions):,} sessions · {len(snap.summary):,} exercises · "
                      f"{len(snap.bodyweight_days):,} days of bodyweight</p>")
        return (f"<h3>Fitness Aggregator {html.escape(__version__)}</h3>"
                "<p>Imports Strong workout exports, and bodyweight from Apple Health, into a local SQLite "
                "database. Corrections are logged and reversible. Shows history, PRs, volume, sets per "
                "muscle and the same charts as the <code>fitness</code> command line.</p>"
                f"<p>Database: <code>{html.escape(str(self.store.path))}</code></p>{counts}"
                "<p>Your data stays on this computer.</p>")
