# fitness_aggregator

A command-line tool for importing, analyzing, and graphing weightlifting logs (Strong app CSV exports). See [PLAN.md](PLAN.md) for the design.

## Status

Milestones 1–4 are done: parsing Strong exports, SQLite storage, merging nearby workouts, the corrections log, per-lift history, PRs, training stats, data-entry checks, and combining similar exercises with weight offsets. See [PLAN.md](PLAN.md) §8 for what comes next.

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

## Plots

Every `fitness plot` command saves a PNG to `./plots/<name>_<date>.png` and prints the path. Use `--out FILE` for another name or format (`.png`, `.svg`, `.pdf`), or `--show` to open a window when you have a display. Charts note any corrections, combined exercises, or placeholder bodyweight they include.

```bash
fitness plot progress "leg extension (machine)" "hip abductor (machine)" --trend
fitness plot progress "pull up" --metric assistance   # e1rm | top | volume | reps | seconds | assistance
fitness plot progress squat --since 2026-09-01 --split --formula brzycki
fitness plot volume --by month --group none          # weekly bars stacked by workout by default
fitness plot calendar                                # training days, darker = more volume
fitness plot duration                                # timer-left-running sessions left out
fitness plot frequency --top 15                      # lifts you haven't done in 30 days are greyed
fitness plot rep-prs "leg extension (machine)"       # best load at 1–15 reps, now vs. a month ago
fitness plot dashboard --out summary.pdf
```

`fitness merge review` also saves a chart when you choose `[p] preview combined`.

![Dashboard](docs/images/plot_dashboard.png)

| | |
|---|---|
| ![Progress](docs/images/plot_progress.png) | ![Weekly volume](docs/images/plot_volume.png) |
| ![Rep maxes](docs/images/plot_rep_prs.png) | ![Merge preview](docs/images/plot_merge_preview.png) |

![Training calendar](docs/images/plot_calendar.png)

The chart functions live in `fitness_aggregator.plots` (`plot_progress`, `plot_volume`, … `plot_dashboard`). Each takes the DataFrames from `fitness_aggregator.metrics` and returns a `matplotlib.figure.Figure` without touching pyplot, so a GUI can embed the same charts.

Data is stored in `~/.fitness_aggregator/lifts.db` (override with `--db` or `$FITNESS_AGGREGATOR_DB`). Settings come from `config/settings.toml` when run from this folder (override with `--config`).

## Development setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
