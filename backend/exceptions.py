class ScraperStructureError(Exception):
    """
    Raised when an HTML scraper receives a 200 response but the expected
    page structure is absent — indicating the site layout has changed.

    This is distinct from connection-level failures (network timeout, HTTP
    errors), which are handled by the circuit breaker. ScraperStructureError
    bypasses the circuit breaker and logs at CRITICAL level so the structural
    change is immediately visible.
    """

    def __init__(self, source: str, url: str, detail: str):
        self.source = source
        self.url = url
        self.detail = detail
        super().__init__(f"[{source}] Scraper structure failure at {url}: {detail}")
