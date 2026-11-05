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
2. **Identify a workout by its `Date` timestamp, not by calendar day.** On 2026-06-02 and 2026-09-15 there are two workouts on the same day.
3. **`Duration` is a string** (`"1h 26m"`, `"47m"`, `"1h"`) and is repeated on every row. Two values are clearly a timer left running: `14h 7m` (2026-08-28) and `22h 43m` (2026-10-22). Flag any duration over about 4h as suspect and leave it out of duration stats.
4. **Timed exercises.** `Plank` (and `Dessert`) have weight 0 and reps 0, with the value in `Seconds`. Track these by hold time instead of load.
5. **Assisted exercises.** For `Pull Up (Assisted)` and `Chest Dip (Assisted)`, `Weight` is the *assistance*, so **less is better**. If bodyweight is known, effective load = bodyweight − assistance. If not, plot the assistance on an inverted axis.
6. **Bodyweight exercises at 0 lb** (`Supported Leg Raise`, `Decline Sit-ups`): track reps, not load.
7. **Unused columns.** `Distance` is always 0, `RPE` is always empty, and `Notes` has one value. Parse them anyway, because future exports may use them.
8. **Data-entry errors.** On 2026-06-04, `Preacher Curl (Machine)` has weight and reps swapped (8 lb × 80, 8 × 60, 6 × 50). Add a corrections mechanism and outlier detection (for example, reps > 30 on a weighted lift).
9. **Inconsistent exercise names.** Examples: `Tricep Pushdown` vs `Triceps Pushdown`, `Hip Abductor (Machine)` vs `Hip Abductor 2 (Machine)`, and `Crunch (Machine)` vs `Crunch (Machine 2)`. Equipment goes in parentheses. Handle this with an alias/metadata file (§4).
10. **Units are not stated.** Assume lb, and make it configurable.

---

## 2. Architecture

The rule for GUI-readiness: **only `cli.py` knows about the terminal.** Everything else takes and returns plain data: dataclasses, pandas DataFrames, and matplotlib `Figure` objects. A future Qt/Tk/web GUI then calls the same functions and embeds the figures with `FigureCanvas`.

```
fitness_aggregator/
├── pyproject.toml
├── PLAN.md
├── config/
│   └── exercises.toml        # aliases, muscle groups, flags (assisted, timed, bodyweight)
├── src/fitness_aggregator/
│   ├── __init__.py
│   ├── models.py             # dataclasses + SetType enum
│   ├── importers/
│   │   ├── __init__.py
│   │   └── strong_csv.py     # parse Strong export -> models
│   ├── store.py              # SQLite persistence, idempotent import
│   ├── catalog.py            # exercise normalization & metadata (loads exercises.toml)
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

Use SQLite (default `~/.fitness_aggregator/lifts.db`, overridable with `--db`) with three tables: `workouts`, `sets`, and `corrections`.

Why SQLite instead of re-reading the CSV on every run:
- **Overlapping exports.** Each Strong export contains the full history. Importing a newer export should be idempotent: dedupe on `(started_at, exercise, set_type, position)`.
- **Corrections persist.** For example, the fix for the swapped Preacher Curl survives re-imports.
- A future GUI or manual-entry feature can write to the same store.

Metrics read from the store into a single tidy DataFrame, one row per set, and work from there.

---

## 3. Metrics (`metrics.py`)

All metrics are pure functions of the form `DataFrame -> DataFrame`, which keeps them easy to test and reuse in a GUI. By default warm-ups are excluded and corrections are applied.

**Per exercise, per session**
- Top set: heaviest weight, ties broken by reps.
- Estimated 1RM from the best set. Epley by default (`w × (1 + r/30)`), with Brzycki as an option. Ignore sets over about 12 reps for e1RM because the estimate gets unreliable.
- Volume (Σ weight × reps), total reps, and set count.
- Assisted lifts use effective load when bodyweight is set. Timed lifts use max and total seconds.

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

## 4. Exercise catalog (`config/exercises.toml`)

```toml
[aliases]
"Tricep Pushdown (Cable - V Bar)" = "Triceps Pushdown (Cable - V Bar)"

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
- **Keep different machines separate by default** (`Hip Abductor` vs `Hip Abductor 2`). They load differently, so mixing them would hide real progression. Alias them only on request.
- `fitness exercises --unmapped` lists exercises that have no muscle-group entry yet, so the file can be filled in gradually.

---

## 5. CLI design (`cli.py`)

Entry point: `fitness`. Global options: `--db PATH`, `--units lb|kg`, `--include-warmups`, `--since DATE`, `--until DATE`.

```
fitness import weights_11_3_26.txt          # idempotent; prints a summary of what was new
fitness exercises [--search curl] [--sort count|recent|name] [--unmapped]
fitness workouts [--last 10]                # list sessions
fitness show [DATE|last]                    # one workout, set by set
fitness history "Bench Press (Barbell)"     # session-by-session table for one lift
fitness prs [EXERCISE] [--recent 30d]       # PR table / recent PRs
fitness stats [--by week|month]             # summary: sessions, volume, sets/muscle
fitness check                               # data-quality report: outliers, suspect durations, unmapped names
fitness fix --workout 2026-06-04T13:53:53 --exercise "Preacher Curl (Machine)" --swap-weight-reps
fitness config set bodyweight 180

fitness plot progress "Bench Press (Barbell)" ["Incline Bench Press (Dumbbell)" ...]
             [--metric e1rm|top|volume|reps|seconds] [--trend]
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

## 6. Graphs (matplotlib)

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

Every plot function has the signature `plot_x(df, **opts) -> matplotlib.figure.Figure`. The function never calls `plt.show()`; the caller (CLI now, GUI later) decides whether to show, save, or embed the figure.

---

## 7. Milestones

1. **Parser + models.** `strong_csv.py` handles every quirk in §1. Tests use small fixture files, plus a smoke test on the real file: 104 workouts, 1,962 sets, and the rest timers attached.
2. **Store + import.** SQLite schema, idempotent import, `import` / `workouts` / `show` commands.
3. **Metrics + text commands.** `history`, `prs`, `stats`, `exercises`, `check`, `fix`.
4. **Core plots.** `progress`, `volume`, `calendar`.
5. **Catalog.** `exercises.toml` with muscle groups and aliases, then `sets-per-muscle`, `rep-prs`, and `dashboard`.
6. **Polish.** Units, bodyweight setting, and remaining plots.

**Later / out of scope for v1:** GUI (PySide6 desktop or a Streamlit web dashboard, both reusing `metrics` and `plots`); manual set logging from the CLI; a bodyweight log; importers for other apps (Hevy, etc.) behind the same `importers/` interface.

---

## 8. Open questions

1. Are weights in **lb**?
2. Should `Hip Abductor 2 (Machine)`, `Crunch (Machine 2)`, and the other variants stay separate from the original machines (the recommendation) or be merged?
3. What bodyweight should be used for assisted pull-ups and dips? A single number, or tracked over time?
4. Will you export the full history from Strong each time (overlapping files)? That is what the dedupe logic assumes.
5. On 2026-06-02 there is a 3-minute workout followed by a 47-minute one. Should those count as one session or two?
