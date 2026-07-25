"""Enable ``python -m docloom`` — the same entry point as the ``docloom`` script.

The studio's ``local`` target drives the CLI through this, so it works in a dev
checkout (``PYTHONPATH=src python -m docloom …``) exactly as it does once the
console script is installed.
"""
from docloom.cli import app

if __name__ == "__main__":
    app()
