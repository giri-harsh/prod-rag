"""ASGI entrypoint:  gunicorn -c gunicorn.conf.py app.main:app   (Linux / the container)

On Windows, start it as a script instead:  python -m app.main

Why the difference: psycopg's async pool refuses to run on the ProactorEventLoop, which is what uvicorn picks
by default on Windows. Importing this module sets the selector policy, but `uvicorn app.main:app` imports the
app from INSIDE a loop it has already created, so the policy change comes too late and every Postgres
operation fails - the pool never fills and /readyz reports postgres down (it used to hang on it). Running
`python -m app.main` sets the policy first and then hands uvicorn a loop it can use. No effect on the Linux
container, which is what gunicorn.conf.py serves.
"""
import asyncio
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.api import create_app  # noqa: E402 - must come after the event-loop policy is set

app = create_app()


def main() -> None:
    import uvicorn

    # uvicorn.run() builds its loop with the policy set above, which is the whole point of this entrypoint.
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")),
                log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
