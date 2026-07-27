"""Enable ``python -m docsynth`` — the same entry point as the ``docsynth`` script.

The studio's ``local`` target drives the CLI through this, so it works in a dev
checkout (``PYTHONPATH=src python -m docsynth …``) exactly as it does once the
console script is installed.
"""
from docsynth.cli import app

if __name__ == "__main__":
    app()
