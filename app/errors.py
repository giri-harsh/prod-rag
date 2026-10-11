"""Typed exceptions mapped to HTTP statuses in app.api."""


class AppError(Exception):
    status_code = 500
    public_message = "Internal error"
    retry_after: int | None = None


class RetrievalUnavailable(AppError):
    status_code = 503
    public_message = "Knowledge base temporarily unavailable"
    retry_after = 5


class DatabaseUnavailable(AppError):
    """Postgres is unreachable or the pool cannot hand out a connection.

    Without this, a Postgres outage escaped as a bare psycopg_pool.PoolTimeout and the client got a generic
    500 "internal server error" after waiting the pool's full queue timeout - 30 s, measured by the chaos
    drill - while a Qdrant outage returned a clean 503 + Retry-After in under a second. Same class of event,
    so the same contract: retryable, and labelled as such.
    """

    status_code = 503
    public_message = "Service temporarily unavailable"
    retry_after = 5


class GuardrailUnavailable(AppError):
    status_code = 503
    public_message = "Safety checks unavailable; request refused"
    retry_after = 10


class UpstreamUnavailable(AppError):
    status_code = 503
    public_message = "Upstream model provider unavailable"
    retry_after = 10


class CircuitOpen(UpstreamUnavailable):
    pass


class EmbeddingConfigMismatch(RuntimeError):
    """Raised at startup if the index and configured embedding model differ."""
