"""Small helpers every module uses: an HTTP refusal, a stored time and a carrier error body."""

from datetime import datetime

from fastapi import HTTPException


def fail(status, message):
    raise HTTPException(status, message)


def dt(value):
    return datetime.fromisoformat(value) if value else None


def body_of(response):
    try:
        return response.json()
    except ValueError:
        return response.text
