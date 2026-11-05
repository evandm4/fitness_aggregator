"""Optional desktop app (PySide6 + matplotlib).

Install with ``pip install 'fitness_aggregator[gui]'`` and launch with
``fitness-gui`` or ``fitness gui``.

Layout:

* ``controller`` loads everything from ``Store``/``metrics`` once per refresh
  (``build_snapshot``, which can run on a worker thread with its own
  connection) and runs the same Store calls as the CLI commands. No Qt imports.
* ``models`` holds Qt table models over plain rows.
* ``charts`` is a thin adapter over ``fitness_aggregator.plots``, so the
  window draws (and saves) the same charts as ``fitness plot``.
* ``window`` is the main window and its tabs. Modal dialogs go through the
  injectable ``Dialogs`` class; ``MainWindow(background=False)`` runs data
  loading synchronously, for tests.
"""
