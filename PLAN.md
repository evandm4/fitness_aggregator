# Weightlifting Tracker: Plan

A Python program that imports weightlifting logs, stores them, works out progression stats, and draws graphs. Version 1 is CLI-only with matplotlib graphs. The code is split into layers so a GUI can be added later without rewriting the core.

---

## 1. What the sample data looks like (`weights_11_3_26.txt`)

The file is CSV in the **Strong app export format**, with these columns:

```
Date, Workout Name, Duration, Exercise Name, Set Order, Weight, Reps, Distance, Seconds, Notes, Workout Notes, RPE
```

| Fact | Value |
|---|---|
| Rows | 3,816 (1,962 real sets + 1,854 `Rest Timer` rows) |
| Workouts | 104, from 2026-06-02 to 2026-11-01 |
| Distinct exercise names | 103 |
| Workout names | Push 1, Pull 1, Legs (main split), plus older "Midday/Afternoon/Evening Workout" |

### Quirks the parser has to handle

1. **`Set Order` is overloaded.**
   - `1`, `2`, `3`, … are normal working sets.
   - `W` is a warm-up set (15 of them). Leave these out of PRs and volume by default.
   - `D` is a drop set (50). Count it toward volume but not as a "top set".
   - `F` is a set to failure (2).
   - `Rest Timer` is not a set. Its `Seconds` value is the rest after the previous set. Attach it to that set as `rest_seconds` rather than throwing it away.
2. **Identify a workout by its `Date` timestamp, not by calendar day, then merge workouts that are close together** (§2a). On 2026-06-02 a 3-minute workout is followed 3 minutes later by a 47-minute one, so these become one session. On 2026-09-15, Pull and Legs are 73 minutes apart, so they stay separate.
3. **`Duration` is a string** (`"1h 26m"`, `"47m"`, `"1h"`) and is repeated on every row. Two values are clearly a timer left running: `14h 7m` (2026-08-28) and `22h 43m` (2026-10-22). Flag any duration over 4h as suspect and leave it out of duration stats. The 2026-08-28 timer ran until 20 seconds before the next morning's workout started, so a suspect duration must never be used to decide whether two workouts are close enough to merge (§2a).
4. **Timed exercises.** `Plank` (and `Dessert`) have weight 0 and reps 0, with the value in `Seconds`. Track these by hold time instead of load.
5. **Assisted exercises.** For `Pull Up (Assisted)` and `Chest Dip (Assisted)`, `Weight` is the *assistance*, so **less is better**. Effective load = bodyweight − assistance, using the bodyweight on that date (§2d). Until real weight data is imported, that's a placeholder you set (180 lb).
6. **Exercises with no external load** (`Supported Leg Raise`, `Decline Sit-ups`, weight 0): track reps, not load.
7. **Unused columns.** `Distance` is always 0, `RPE` is always empty, and `Notes` has one value. Parse them anyway, because future exports may use them.
8. **Data-entry errors.** On 2026-06-04, `Preacher Curl (Machine)` has weight and reps swapped (8 lb × 80, 8 × 60, 6 × 50). Detect likely errors (for example, reps > 30 on a weighted lift) and ask you how to fix each one. Every fix is logged (§2b).
9. **Similar exercise names.** Examples: `Tricep Pushdown` vs `Triceps Pushdown`, `Hip Abductor (Machine)` vs `Hip Abductor 2 (Machine)`, and `Crunch (Machine)` vs `Crunch (Machine 2)`. The program never merges these on its own. It shows you each similar group, and you choose whether to combine them, optionally with a weight offset (§4).
10. **Units.** All weights are lb. Store lb internally. A `--units kg` display option can come later.

---

## 2. Architecture

The rule for GUI-readiness: **only `cli.py` knows about the terminal.** Everything else takes and returns plain data: dataclasses, pandas DataFrames, and matplotlib `Figure` objects. A future Qt/Tk/web GUI then calls the same functions and embeds the figures with `FigureCanvas`.

```
fitness_aggregator/
├── pyproject.toml
├── PLAN.md
├── config/
│   ├── settings.toml         # bodyweight placeholder, merge gap, thresholds
│   └── exercises.toml        # muscle groups, flags (assisted, timed, unloaded); no merging
├── src/fitness_aggregator/
│   ├── __init__.py
│   ├── models.py             # dataclasses + SetType enum
│   ├── importers/
│   │   ├── __init__.py
│   │   ├── strong_csv.py     # parse Strong export -> models
│   │   └── apple_health.py   # (later) bodyweight history from Apple Health export
│   ├── store.py              # SQLite persistence, idempotent import
│   ├── sessions.py           # merge workouts that are close together in time
│   ├── bodyweight.py         # bodyweight_at(date): logged value or placeholder
│   ├── corrections.py        # corrections log: record, list, revert
│   ├── similarity.py         # find similar exercise names, suggest weight offsets
│   ├── catalog.py            # exercise metadata (loads exercises.toml)
│   ├── metrics.py            # pure functions -> DataFrames (e1RM, volume, PRs, ...)
│   ├── plots/
│   │   ├── __init__.py
│   │   ├── style.py          # shared matplotlib style
│   │   ├── progress.py
│   │   ├── volume.py
│   │   ├── calendar.py
│   │   └── dashboard.py
│   └── cli.py                # Typer app; thin wrapper over the above
└── tests/
    ├── fixtures/             # small hand-made CSVs covering each quirk
    ├── test_strong_csv.py
    ├── test_metrics.py
    └── test_store.py
```

### Dependencies

- **pandas**: grouping and resampling by week or month
- **matplotlib**: graphs
- **typer** + **rich**: CLI subcommands and readable tables
- **sqlite3** (stdlib): storage
- **pytest**: tests

### Data model (`models.py`)

```python
class SetType(Enum):  NORMAL, WARMUP, DROP, FAILURE

@dataclass
class WorkoutSet:
    index: int | None          # 1,2,3… (None for W/D/F)
    set_type: SetType
    weight: float
    reps: float
    seconds: float             # hold time for timed exercises
    distance: float
    rpe: float | None
    rest_seconds: float | None # from the following Rest Timer row
    notes: str

@dataclass
class ExerciseEntry:
    name: str                  # raw name as exported
    sets: list[WorkoutSet]

@dataclass
class Workout:
    started_at: datetime       # unique key
    name: str
    duration: timedelta | None
    duration_suspect: bool
    notes: str
    exercises: list[ExerciseEntry]
```

### Storage (`store.py`)

Use SQLite (default `~/.fitness_aggregator/lifts.db`, overridable with `--db`) with these tables:

| Table | Contents |
|---|---|
| `workouts`, `sets` | Raw imported data, exactly as exported. Never modified after import. |
| `corrections` | Every change applied on top of the raw data (§2b) |
| `exercise_links` | Your merge decisions for similar exercises: combined, combined with offset, or kept separate (§4) |
| `bodyweight` | Dated bodyweight entries (empty until an Apple Health import exists, §2d) |

Settings are kept in `config/settings.toml`, which you can edit by hand or change with `fitness config set`.

Why SQLite instead of re-reading the CSV on every run:
- **Full-history exports.** Each Strong export contains your entire history, so importing never appends. The newest export replaces the stored raw data, and corrections and merge decisions are reapplied on top (§2c).
- **Corrections and merge decisions persist.** For example, the fix for the swapped Preacher Curl survives re-imports, and you are not asked about Hip Abductor again.
- A future GUI or manual-entry feature can write to the same store.

Because raw data is never edited, every correction and merge can be undone. Metrics build a tidy DataFrame (one row per set) by reading the raw sets and applying, in order: workout merges, then data fixes, then exercise merges and offsets. Each row keeps its original values and records which corrections touched it.

### 2a. Merging workouts that are close together (`sessions.py`)

The rule: merge workout B into the preceding workout A when **B starts within 60 minutes of A ending** (end = start + duration). The gap can be changed with `fitness config set merge_gap_minutes 60`.

- If A's duration is suspect (over 4h), don't trust its end time. Estimate it as start + your median workout duration (about 1h15m in this data). Without this, the 2026-08-28 Legs workout, whose timer ran 14h until 20 seconds before the next morning's Pull workout, would be merged into it.
- A merged session keeps the earliest start time and the longer workout's name. Its duration runs from the first start to the last end. Exercises keep their order.
- Effect on the sample data: 2026-06-02 (3m and 47m workouts, 3 minutes apart) is merged into one session. 2026-09-15 (Pull and Legs, 73 minutes apart) and 2026-08-28 (Legs with the runaway timer) stay separate.
- Every merge is written to the corrections log and can be reverted individually.

### 2b. Corrections log (`corrections.py`)

**Every change the system makes to your data, automatic or chosen by you, is recorded and reported.** Nothing is changed silently.

Each entry records: id, timestamp, type, source (`auto` or `user`), the affected workout/exercise/sets, values before and after, and a reason in plain English.

| Type | Source | Example reason |
|---|---|---|
| `workout_merge` | auto | "Merged 2026-06-02 14:06 workout into 14:00 workout: started 3 min after it ended (threshold 60 min)" |
| `duration_suspect` | auto | "Duration 14h 7m excluded from duration stats: over 4h, probably a timer left running" |
| `value_fix` | user | "Swapped weight and reps on 3 Preacher Curl (Machine) sets, 2026-06-04" |
| `exercise_merge` | user | "Hip Abductor 2 (Machine) combined into Hip Abductor (Machine) with −55 lb offset" |

How corrections are reported:
- **On import:** a summary of what was new and every correction applied, for example "1 workout merged, 2 durations excluded", plus how many sets are flagged as possible data-entry errors.
- **Flags are not corrections.** A flagged set changes nothing, so flags aren't log entries. `fitness check` lists them; each is either fixed (which creates a `value_fix` entry) or dismissed (remembered in a `dismissed_flags` table so it isn't raised again). Flag rules: over 30 reps on a weighted set (swap suggested when the swapped weight fits your usual range), a working set over 2.5× your usual top set, and weight with 0 reps on a normal or drop set (failure sets are exempt, since 0 reps there is a missed attempt).
- **`fitness corrections`:** the full log, filterable by `--type`, `--auto`/`--user`, `--exercise`, or `--since`. `fitness corrections revert ID` undoes one entry.
- **In output:** `history` and `show` mark corrected rows (for example with `*`) and list the reasons beneath the table. Plots built from corrected data get a small footnote such as "Includes 1 exercise merge (−55 lb offset); see `fitness corrections`."

Parsing steps that change no values, such as attaching rest timers to sets or excluding warm-ups, are reported as counts on import, not as individual log entries.

### 2c. Importing full-history exports

Every Strong export contains your entire history, so the newest export is the source of truth.

- **Replace, don't append.** Importing a newer export replaces the stored raw workouts and sets. Corrections and merge decisions are stored separately and reapplied afterwards. They point at stable keys (workout start time, exercise name, set position), not database row ids, so they survive the replacement.
- **Changes since the last import.** The import summary compares the new export with the stored data:
  - new workouts
  - workouts edited in Strong (sets added, removed, or changed)
  - workouts removed in Strong (deleted there); these are listed and must be confirmed before they're dropped
- **Corrections whose target changed.** If you later fix the swapped Preacher Curl in Strong itself, the stored correction no longer matches the data. It's marked as stale and reported rather than silently applied or dropped. You choose to keep or remove it.
- **Guard against importing an older export.** If the file's latest workout is older than what's stored, the import stops with a warning (override with `--force`). Without this, importing an older file would look like workouts had been deleted.
- **Re-importing the same file** changes nothing and says so.

### 2d. Bodyweight (`bodyweight.py`)

All bodyweight lookups go through one function, `bodyweight_at(date)`:

1. If the `bodyweight` table has entries, use the most recent entry on or before that date. Before the first entry, use the first entry.
2. Otherwise, use the placeholder from `config/settings.toml`, which starts at **`bodyweight_lb = 180`**.

What bodyweight affects: effective load for assisted lifts, which feeds their e1RM, volume, and PRs. Nothing else uses it.

**It's always clear when the placeholder is used.** Output and plots that rely on it say so, for example "Assisted-lift loads use placeholder bodyweight 180 lb". This is a label, not an entry in the corrections log, since no logged data is changed.

**Future Apple Health import.** `fitness import-health export.zip` would read the body-mass records from the Health app's "Export All Health Data" file into the `bodyweight` table. It would follow the same full-history rules as Strong imports (§2c). Derived numbers are never stored, so every effective load and e1RM switches from the placeholder to real dated weights as soon as the import runs. No other code changes are needed.

---

## 3. Metrics (`metrics.py`)

All metrics are pure functions of the form `DataFrame -> DataFrame`, which keeps them easy to test and reuse in a GUI. By default warm-ups are excluded and corrections are applied.

**Per exercise, per session**
- Top set: heaviest weight, ties broken by reps.
- Estimated 1RM from the best set. Epley by default (`w × (1 + r/30)`), with Brzycki as an option. Ignore sets over about 12 reps for e1RM because the estimate gets unreliable.
- Volume (Σ weight × reps), total reps, and set count.
- **Assisted lifts** use effective load (bodyweight − assistance, §2d) for top set, e1RM, volume, and PRs, so they behave like any other lift. Because that depends on bodyweight, there's also an assistance-only view (`--metric assistance`) that doesn't. It plots assistance on an inverted y-axis labelled "Assistance (lb, lower is better)". PRs in that view are the lowest assistance at each rep count.
- **Timed lifts** use max and total seconds. **Unloaded lifts** use max and total reps.

**PRs**
- e1RM PRs, heaviest-weight PRs, and **rep PRs** (best weight at each rep count from 1 to 15).
- PR timeline: the date each PR was set, for the "PRs this month" view.

**Per workout**
- Duration (excluding suspect values), total volume, set count, exercise count, and density (volume per minute).
- Average rest per exercise, taken from the rest-timer data.

**Over time (weekly/monthly)**
- Sessions per week, plus streaks and gaps between sessions.
- Volume and **hard sets per muscle group per week**. This is the standard hypertrophy volume metric and needs the muscle-group map from §4.
- Split adherence: how many Push, Pull, and Legs sessions per week.
- Exercise frequency and "last performed" date, to spot neglected lifts.

---

## 4. Combining similar exercises (interactive)

The program does not decide which exercises are "the same". It finds candidates and you decide, because only you know that, for example, Hip Abductor 2 is a machine at another gym that is much easier at the same weight.

### Finding candidates (`similarity.py`)

Exercise names are grouped as similar when they match after normalizing:
- `Tricep` and `Triceps` treated as the same word
- trailing machine numbers removed (`Hip Abductor 2 (Machine)` → `Hip Abductor (Machine)`, `Crunch (Machine 2)` → `Crunch (Machine)`)
- case, punctuation, and word order ignored, with a fuzzy-match fallback using `difflib` (stdlib)

Groups you have already decided on are not shown again. New exercise names that appear in later imports are offered for review automatically.

### Review flow: `fitness merge review`

For each candidate group, the program shows the exercises side by side, then asks what to do:

The example below uses the real numbers from your data:

```
Similar exercises (group 1 of N):

                                 Sessions  Date range                Last/first top sets
  [1] Hip Abductor (Machine)          12   2026-06-02 → 2026-08-12   last 3: 145–160 lb × 14
  [2] Hip Abductor 2 (Machine)         9   2026-08-22 → 2026-11-01   first 3: 205 lb × 12–14

  You switched from [1] to [2] on 2026-08-22 (no overlap).
  Around the switch, [2] sets were about 55 lb heavier at similar reps.
  Suggested: −55 lb on Hip Abductor 2 (or ×0.74).

  (c) combine as-is
  (o) combine with weight offset
  (k) keep separate (don't ask again)
  (s) skip for now
  (p) plot both on one chart first
>
```

- **Combine as-is:** pick which name to keep. The other exercise's sets are counted under that name.
- **Combine with offset:** pick which name to keep and which side to adjust, then accept the suggested offset or enter your own. The offset is an amount in lb added to or subtracted from every set of one exercise (for example −55 lb on every Hip Abductor 2 set). A percentage factor (for example ×0.74) is also available, since machine differences are often proportional.
  - **How the suggested offset is calculated:** compare top sets at similar rep counts from the sessions **on either side of the switch**: the last 3 sessions of one exercise and the first 3 of the other, or the sessions closest in time if you used both during the same period. Comparing whole-history averages would count your real progress as machine difference. The suggestion is rounded to 5 lb. It's only a starting point; you confirm or override it.
- **Plot both first:** shows the progress chart of both exercises together, plus a preview of the combined line with the offset applied, before you decide.
- **The original value is kept for every adjusted set.** `history` shows both, for example `150 lb (205 − 55)`. Offsets apply to PRs, e1RM, and volume.

Every decision becomes an `exercise_merge` entry in the corrections log and can be undone with `fitness merge undo` or `fitness corrections revert`. Non-interactive commands (`fitness merge add …`) do the same thing for scripting, and a future GUI can call the same functions with a dialog in place of the prompt.

## 5. Exercise metadata (`config/exercises.toml`)

This file is only for metadata. It never merges exercises.

```toml
[exercise."Pull Up (Assisted)"]
muscles = ["back", "biceps"]
assisted = true

[exercise."Plank"]
muscles = ["core"]
timed = true

[exercise."Bench Press (Barbell)"]
muscles = ["chest", "triceps", "front_delts"]
```

- Parse names as `Base Movement (Equipment)`. This allows grouping either by base movement ("all bench press variants") or by exact exercise.
- An exercise combined with another (§4) uses the metadata of the name you kept.
- `fitness exercises --unmapped` lists exercises that have no muscle-group entry yet, so the file can be filled in gradually.

---

## 6. CLI design (`cli.py`)

Entry point: `fitness`. Global options: `--db PATH`, `--include-warmups`, `--since DATE`, `--until DATE`, `--raw` (ignore all corrections and merges).

```
fitness import weights_11_3_26.txt          # idempotent; prints new data + corrections applied
fitness exercises [--search curl] [--sort count|recent|name] [--unmapped (milestone 6)]
fitness workouts [--last 10]                # list sessions
fitness show [DATE|last]                    # one workout, set by set
fitness history "Bench Press (Barbell)"     # session-by-session table for one lift
fitness prs [EXERCISE] [--recent 30d]       # PR table / recent PRs
fitness stats [--by week|month]             # summary: sessions, volume, sets/muscle
fitness check                               # interactive review of flagged outliers (swap / edit / ignore)
fitness fix --workout 2026-06-04T13:53:53 --exercise "Preacher Curl (Machine)" --swap-weight-reps

fitness corrections [--type T] [--auto|--user] [--exercise E] [--since D]   # full corrections log
fitness corrections revert ID

fitness merge review                        # interactive similar-exercise review (§4)
fitness merge list                          # current merge decisions and offsets
fitness merge add "Hip Abductor 2 (Machine)" --into "Hip Abductor (Machine)" [--offset -55 | --factor 0.74]
fitness merge separate "Crunch (Machine)" "Crunch (Machine 2)"
fitness merge undo "Hip Abductor 2 (Machine)"

fitness workouts unmerge 2026-06-02T14:06:32   # undo an automatic workout merge
fitness config set merge_gap_minutes 60     # also suspect_duration_hours
fitness config set bodyweight_lb 180        # placeholder used until real weight data exists
fitness import-health export.zip            # (later) bodyweight history from Apple Health

fitness plot progress "Bench Press (Barbell)" ["Incline Bench Press (Dumbbell)" ...]
             [--metric e1rm|top|volume|reps|seconds|assistance] [--trend]
fitness plot volume   [--by week|month] [--group muscle|workout|none]
fitness plot sets-per-muscle [--by week]
fitness plot rep-prs  "Squat (Barbell)"
fitness plot calendar
fitness plot duration
fitness plot frequency [--top 20]
fitness plot dashboard
```

Plot output: by default, save a PNG to `./plots/<name>_<date>.png` and print the path. `--show` opens an interactive window instead, and `--out FILE` picks the filename (`.png`, `.svg`, or `.pdf`). Saving by default means the tool also works over SSH or in headless environments.

---

## 7. Graphs (matplotlib)

| Command | Chart | Notes |
|---|---|---|
| `progress` | Line over time: e1RM and top-set weight per session | PRs marked, optional rolling trend, several exercises overlaid on one axis or as small multiples |
| `volume` | Weekly/monthly bars, stacked by muscle group or workout | Shows training load trends |
| `sets-per-muscle` | Lines or heatmap of weekly hard sets per muscle | Optional shaded "target" band (e.g. 10–20 sets) |
| `rep-prs` | Best weight at each rep count from 1 to 15 | Strength-endurance curve, with the previous best shown faintly for comparison |
| `calendar` | GitHub-style training-day heatmap | Color by volume or by workout type |
| `duration` | Session duration over time | Suspect outliers left out |
| `frequency` | Horizontal bars: sessions per exercise | Spot neglected lifts |
| `dashboard` | Multi-panel figure combining the above | One-glance summary PNG |

Every plot function has the signature `plot_x(df, **opts) -> matplotlib.figure.Figure`. The function never calls `plt.show()`; the caller (CLI now, GUI later) decides whether to show, save, or embed the figure. A plot that uses corrected or merged data gets a footnote saying so (§2b). In `progress`, sets adjusted by an offset are drawn with a different marker.

---

## 8. Milestones

1. ✅ **Parser + models.** `strong_csv.py` handles every quirk in §1. Tests use small fixture files, plus a smoke test on the real file: 104 workouts, 1,962 sets, and the rest timers attached.
2. ✅ **Store, import, corrections log.** SQLite schema, full-history import with change summary (§2c), automatic workout merging and suspect-duration flags, all recorded in the log. Commands: `import`, `workouts`, `show`, `corrections`. The test for this step checks that the sample data ends up with 103 sessions and exactly one workout merge, on 2026-06-02.
3. ✅ **Metrics + text commands.** `history`, `prs`, `stats`, `exercises`, plus `check` / `fix` for flagged outliers.
4. **Similar-exercise merging.** Candidate detection, offset suggestion, `merge review` and the other `merge` commands.
5. **Core plots.** `progress` (including the merge preview), `volume`, `calendar`.
6. **Metadata + remaining plots.** `exercises.toml` muscle groups, then `sets-per-muscle`, `rep-prs`, `duration`, `frequency`, and `dashboard`. `bodyweight_at()` with the placeholder.

**Later / out of scope for v1:** GUI (PySide6 desktop or a Streamlit web dashboard, both reusing `metrics` and `plots`); Apple Health bodyweight import (§2d); manual set logging from the CLI; importers for other apps (Hevy, etc.) behind the same `importers/` interface.

---

## 9. Decisions and open questions

**Decided**
- Weights are lb.
- Similar exercises are combined only when you choose to, through the interactive review, with optional weight offsets (§4).
- Workouts that start within 60 minutes of the previous one ending are merged automatically (§2a).
- Every correction the system makes is logged, reported, and reversible (§2b).
- Bodyweight comes from a placeholder you set (180 lb) until an Apple Health import provides real dated weights. It's used only for assisted-lift effective load, and output says when the placeholder is in use (§2d).
- Every Strong export contains the full history. The newest import replaces the stored raw data (§2c).

**Still open:** none right now.
