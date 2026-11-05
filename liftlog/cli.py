import argparse
from datetime import date

from . import db, metrics


def fmt(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="liftlog", description="Track weightlifting metrics.")
    p.add_argument("--db", default=str(db.DEFAULT_DB), help="SQLite file (default ~/.liftlog.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="log a set")
    a.add_argument("exercise")
    a.add_argument("weight", type=float)
    a.add_argument("reps", type=int)
    a.add_argument("--rpe", type=float)
    a.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD (default today)")

    ls = sub.add_parser("list", help="list logged sets")
    ls.add_argument("exercise", nargs="?")
    ls.add_argument("--since")

    d = sub.add_parser("delete", help="delete a set by id")
    d.add_argument("id", type=int)

    sub.add_parser("prs", help="personal records per exercise")
    pr = sub.add_parser("progress", help="best estimated 1RM per day for an exercise")
    pr.add_argument("exercise")
    sub.add_parser("volume", help="total volume per week")

    ex = sub.add_parser("export", help="write all sets to CSV")
    ex.add_argument("file")
    im = sub.add_parser("import", help="read sets from CSV (date,exercise,weight,reps,rpe)")
    im.add_argument("file")

    args = p.parse_args(argv)
    conn = db.connect(args.db)

    if args.cmd == "add":
        try:
            sid = db.add_set(conn, db.LiftSet(args.date, args.exercise, args.weight, args.reps, args.rpe))
        except ValueError as e:
            p.error(str(e))
        except Exception as e:  # CHECK constraint violations etc.
            p.error(f"invalid set: {e}")
        print(f"logged #{sid}: {db.norm_exercise(args.exercise)} {fmt(args.weight)} x {args.reps}")
    elif args.cmd == "list":
        for s in db.get_sets(conn, args.exercise, args.since):
            rpe = f" @{fmt(s.rpe)}" if s.rpe is not None else ""
            print(f"#{s.id:<4} {s.day}  {s.exercise:<20} {fmt(s.weight):>6} x {s.reps}{rpe}")
    elif args.cmd == "delete":
        print("deleted" if db.delete_set(conn, args.id) else "no such set")
    elif args.cmd == "prs":
        for ex_name, r in sorted(metrics.personal_records(db.get_sets(conn)).items()):
            h, e, v = r["max_weight"], r["best_e1rm"], r["best_volume"]
            print(f"{ex_name}\n  heaviest: {fmt(h.weight)} x {h.reps} ({h.day})"
                  f"\n  best e1RM: {fmt(metrics.e1rm(e.weight, e.reps))} ({e.day})"
                  f"\n  best set volume: {fmt(v.weight * v.reps)} ({v.day})")
    elif args.cmd == "progress":
        for day, val in metrics.progression(db.get_sets(conn, args.exercise)):
            print(f"{day}  e1RM {fmt(val)}")
    elif args.cmd == "volume":
        for wk, vol in metrics.weekly_volume(db.get_sets(conn)):
            print(f"{wk}  {fmt(vol)}")
    elif args.cmd == "export":
        print(f"exported {db.export_csv(conn, args.file)} sets")
    elif args.cmd == "import":
        print(f"imported {db.import_csv(conn, args.file)} sets")
    return 0
