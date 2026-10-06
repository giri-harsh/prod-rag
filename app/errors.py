"""Typed exceptions mapped to HTTP statuses in app.api."""


class AppError(Exception):
    status_code = 500
    public_message = "Internal error"
    retry_after: int | None = None


class RetrievalUnavailable(AppError):
    status_code = 503
    public_message = "Knowledge base temporarily unavailable"
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
