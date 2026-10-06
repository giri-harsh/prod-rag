"""ASGI entrypoint:  gunicorn -c gunicorn.conf.py app.main:app"""
from app.api import create_app

app = create_app()
