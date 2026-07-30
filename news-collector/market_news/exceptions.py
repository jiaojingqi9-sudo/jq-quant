class CookieExpiredError(RuntimeError):
    """Raised when a cookie-backed source redirects to login or rejects the session."""
