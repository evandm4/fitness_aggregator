"""Optional desktop app (PySide6 + matplotlib).

Install with ``pip install 'fitness_aggregator[gui]'`` and launch with
``fitness-gui`` or ``fitness gui``.

Layout:

* ``controller`` loads everything from ``Store``/``metrics`` once per refresh
  and runs the same Store calls as the CLI commands. No Qt imports.
* ``models`` holds Qt table models over plain rows.
* ``charts`` builds matplotlib figures from metrics DataFrames.
* ``window`` is the main window and its tabs.
"""
