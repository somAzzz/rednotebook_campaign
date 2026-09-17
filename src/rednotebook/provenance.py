"""Deterministic, display-safe source locators."""

from urllib.parse import quote, urlsplit, urlunsplit


def public_http_url(value):
    """Return a query/fragment-free public HTTP locator, or ``None``.

    Source locators are evidence, not instructions. In particular, signed query
    parameters must not leak into user-facing reports.
    """
    try:
        parsed = urlsplit(str(value or ""))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{parsed.port}" if parsed.port is not None else "")
        path = quote(parsed.path or "/", safe="/%:@-._~!$&'*+,;=")
        return urlunsplit((parsed.scheme, netloc, path, "", ""))
    except (UnicodeError, ValueError):
        return None
