"""
Uniform failure logging for conditions fetchers.

Every fetcher's public entry point catches its own failures, logs exactly one
`fetch_failed` WARNING through here, and returns None. Callers (scheduler jobs,
the chat context builder) treat None as "no fresh data, serve stale".

Before this, fetchers only caught pybreaker.CircuitBreakerError. The breakers
never trip on async functions, so every real failure (DNS, 403, timeout,
malformed payload) escaped raw: it aborted per-water-body job loops, or was
logged by the caller with no source or cause.
"""

import logging

import httpx

log = logging.getLogger("conditions.fetch")

_BODY_SNIPPET = 300


def log_fetch_failure(source: str, exc: BaseException, **context) -> None:
    extra = {"source": source, "error_type": type(exc).__name__, **context}
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        reason = resp.reason_phrase or httpx.codes.get_reason_phrase(resp.status_code)
        extra["status_code"] = resp.status_code
        extra["error"] = f"HTTP {resp.status_code} {reason}".strip()
        # The body often says why (e.g. AirNow's 410: "web service has been retired").
        try:
            body = " ".join(resp.text.split())[:_BODY_SNIPPET]
        except Exception:
            body = ""
        if body:
            extra["response_snippet"] = body
    else:
        # str() of some httpx errors (ReadTimeout, ...) is empty; fall back to repr.
        extra["error"] = str(exc) or repr(exc)
    request = getattr(exc, "_request", None)  # httpx.HTTPError.request raises if unset
    if request is not None:
        # Path only: query strings can carry API keys.
        extra["url"] = str(request.url.copy_with(query=None))
    log.warning("fetch_failed", extra=extra)
