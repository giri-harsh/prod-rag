"""ASGI entrypoint:  gunicorn -c gunicorn.conf.py app.main:app"""
import asyncio
import sys

if sys.platform == "win32":
    # uvicorn picks the ProactorEventLoop by default on Windows, and psycopg's async pool refuses to run on
    # it ("Psycopg cannot use the 'ProactorEventLoop'"). Every connection attempt then fails, the pool never
    # fills, and /readyz blocks on it - so running the app directly on Windows was broken while every other
    # entrypoint (tests, evals, loadtest harnesses) set this policy for itself. No effect on the Linux
    # container, which is what gunicorn.conf.py serves.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.api import create_app  # noqa: E402 - must come after the event-loop policy is set

app = create_app()
