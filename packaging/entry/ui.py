"""PyInstaller entry point of ``pagewatch-ui`` (see packaging/pagewatch.spec).

``launch`` calls ``multiprocessing.freeze_support()`` before anything else, so keep this file
free of other imports and statements.
"""

from pagewatch.engine.frozen import launch

if __name__ == "__main__":
    raise SystemExit(launch("ui"))
