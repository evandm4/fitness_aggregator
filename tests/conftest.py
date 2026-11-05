import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Typer forces colored, boxed output when it sees GITHUB_ACTIONS / FORCE_COLOR, which
# breaks plain-text assertions on CI. Its own switch turns that off; it's read on
# import, so it has to be set before any test module imports typer.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
# The app's own Rich console honours FORCE_COLOR too; tests compare plain text.
for _var in ("FORCE_COLOR", "PY_COLORS"):
    os.environ.pop(_var, None)
