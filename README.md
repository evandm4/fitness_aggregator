# fitness_aggregator

A command-line tool for importing, analyzing, and graphing weightlifting logs (Strong app CSV exports). See [PLAN.md](PLAN.md) for the design.

## Status

Milestone 1 (parser + data model) is done. See [PLAN.md](PLAN.md) §8 for what comes next.

## Development setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Parse an export from Python:

```python
from fitness_aggregator.importers import parse_strong_csv

result = parse_strong_csv("weights_11_3_26.txt")
print(result.stats)
```
