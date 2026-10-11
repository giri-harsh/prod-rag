"""ASGI entrypoint:  gunicorn -c gunicorn.conf.py app.main:app   (Linux / the container)

On Windows, start it as a script instead:  python -m app.main

Why the difference: psycopg's async pool refuses to run on the ProactorEventLoop, and on Windows that is the
loop uvicorn builds. Every Postgres operation then fails, the pool never fills, and /readyz reports postgres
down while the app happily serves traffic.

The fix is NOT the event-loop policy. uvicorn 0.36 replaced `Config.setup_event_loop()` with
`Config.get_loop_factory()` and now constructs its loop from an explicit factory
(`uvicorn.loops.asyncio:asyncio_loop_factory` hard-returns `asyncio.ProactorEventLoop` on win32), so
`asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())` is simply ignored by the server - which is
how `python -m app.main` still came up on a Proactor loop after this module set the policy. uvicorn does accept
a dotted path in `loop=` and uses it as the factory directly, so the loop is selected explicitly below.

The policy is still set at import time for anything else that runs in this process and calls
`asyncio.new_event_loop()` itself. No effect on the Linux container, which is what gunicorn.conf.py serves.
"""
import asyncio
import os
import sys

# uvicorn ignores this (see above); it covers other in-process consumers of the default policy.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.api import create_app  # noqa: E402 - must come after the event-loop policy is set

app = create_app()

# Passed to uvicorn's `loop=`. A value that is not one of its own presets is imported and used as the loop
# factory as-is, which is the only supported way to refuse the Proactor loop on Windows. Elsewhere "auto" keeps
# uvloop when it is installed.
LOOP = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")),
                log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"), loop=LOOP)


if __name__ == "__main__":
    main()
