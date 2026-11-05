# fitness_aggregator

A command-line tool for importing, analyzing, and graphing weightlifting logs (Strong app CSV exports). See [PLAN.md](PLAN.md) for the design.

## Status

Milestones 1–4 and 6 are done: parsing Strong exports, SQLite storage, merging nearby workouts, the corrections log, per-lift history, PRs, training stats, data-entry checks, combining similar exercises with weight offsets, muscle groups, and bodyweight from Apple Health. See [PLAN.md](PLAN.md) §8 for what comes next.

## Usage

```bash
fitness import weights_11_3_26.txt   # each export is your full history; re-import any time
fitness workouts                     # recent sessions (--all, --since 2026-09-01, --raw)
fitness show last                    # one session set by set (or a date: fitness show 2026-06-02)
fitness corrections                  # every change applied on top of your raw data
fitness corrections revert 3         # undo one (fitness corrections restore 3 re-applies it)
fitness workouts unmerge 2026-06-02T14:06

fitness history "bench press (barbell)"   # one lift, session by session (a unique part of the name works)
fitness prs                          # current bests for every exercise
fitness prs squat                    # rep maxes + PR timeline for one lift
fitness prs --recent 30d             # PRs set in the last 30 days
fitness stats                        # frequency, streaks, weekly volume (--by month)
fitness exercises                    # everything you've logged (--sort recent, --search curl)
fitness check                        # review likely data-entry errors: swap, edit, or dismiss
fitness fix 2026-06-04T13:53 "preacher curl" --set 1 --swap-weight-reps

fitness merge review                 # similar exercises: combine (with a suggested offset) or keep separate
fitness merge list                   # what's combined and what's kept separate
fitness merge add "hip abductor 2" --into "hip abductor (machine)" --offset -50
fitness merge undo "hip abductor 2"
fitness config show
fitness config set merge_gap_minutes 45
```

## Muscle groups

`config/exercises.toml` maps every exercise to the muscles it trains, from a fixed list (chest, front_delts, side_delts, rear_delts, triceps, biceps, forearms, lats, upper_back, lower_back, abs, obliques, quads, hamstrings, glutes, adductors, abductors, calves):

```toml
[exercise."Bench Press (Barbell)"]
primary = ["chest"]                     # each hard set counts 1
secondary = ["triceps", "front_delts"]  # ... and 0.5 here
```

```bash
fitness muscles                      # hard sets per muscle per week (--by month, --last N, --all, --since DATE)
fitness exercises --unmapped         # exercises with no entry yet, with a suggested mapping
```

Hard sets are normal, failure, and drop sets. Weeks under 10 or over 20 sets for a muscle are highlighted (a common hypertrophy range). Exercises missing from the file get a keyword guess (a "curl" trains biceps), which is always labelled as a guess. `fitness exercises` shows each exercise's primary muscles.

## Bodyweight from Apple Health

Bodyweight only affects assisted lifts (load = bodyweight − assistance). Until you import real weights, a placeholder from settings is used (`bodyweight_lb`, 180 by default).

```bash
fitness import-health export.zip     # Health app → profile → Export All Health Data (or the export.xml inside)
fitness bodyweight                   # recent entries, lowest/highest, trend (--last N)
```

Readings are converted to lb and averaged per day. Like Strong exports, each Health export is your full history and replaces the stored entries; the import lists new, changed, and removed days. After importing, every analysis uses the latest entry on or before each workout.

Data is stored in `~/.fitness_aggregator/lifts.db` (override with `--db` or `$FITNESS_AGGREGATOR_DB`). Settings come from `config/settings.toml` when run from this folder (override with `--config`).

## Development setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
