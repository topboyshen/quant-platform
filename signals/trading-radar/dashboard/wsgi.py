"""WSGI entrypoint for running the Dashboard with gunicorn.

Gunicorn imports this module instead of executing dashboard/app.py as __main__, so
background refresh must be started explicitly here.
"""

from __future__ import annotations

from dashboard.app import app, start_background_refresh

start_background_refresh()
