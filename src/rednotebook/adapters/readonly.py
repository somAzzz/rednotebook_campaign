"""Bounded GET-only adapter transport. No cookies, scripts, login or write endpoints."""

import time
from urllib.parse import urlsplit

import httpx

from rednotebook.errors import DomainError


class ReadOnlyAdapter:
    def __init__(self, db, source_id, origin, *, transport=None, budget=10, sleep=time.sleep):
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise DomainError("adapter_origin_invalid")
        if parsed.path not in ("", "/") or parsed.hostname != "www.xiaohongshu.com":
            raise DomainError("adapter_origin_not_allowed")
        if not 1 <= budget <= 50:
            raise DomainError("adapter_budget_invalid")
        self.db, self.source_id = db, source_id
        self.origin, self.budget, self.requests = origin.rstrip("/"), budget, 0
        self.paused, self.cache, self.sleep = False, {}, sleep
        self.client = httpx.Client(
            transport=transport, timeout=15, follow_redirects=False, trust_env=False
        )

    def close(self):
        self.client.close()

    def get_note(self, path):
        import re

        if not re.fullmatch(r"/explore/[0-9a-f]{24}", path):
            raise DomainError("adapter_read_path_invalid")
        self.db.require_source(self.source_id, "automated_access")
        self.db.require_source(self.source_id, "local_analysis")
        if self.paused:
            raise DomainError("adapter_paused")
        if path in self.cache:
            return self.cache[path] | {"cached": True}
        for attempt in range(3):
            if self.requests >= self.budget:
                raise DomainError("adapter_budget_exhausted")
            self.requests += 1
            try:
                with self.client.stream("GET", self.origin + path) as response:
                    if response.status_code in (401, 403):
                        self.paused = True
                        raise DomainError(
                            "adapter_auth_required"
                            if response.status_code == 401
                            else "adapter_access_blocked"
                        )
                    if response.status_code == 429:
                        if attempt == 2:
                            raise DomainError("adapter_rate_limited")
                        retry = response.headers.get("retry-after", "1")
                        if not retry.isdigit() or int(retry) > 30:
                            self.paused = True
                            raise DomainError("adapter_retry_later")
                        self.sleep(int(retry))
                        continue
                    if response.status_code != 200:
                        raise DomainError("adapter_http_error")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 5 * 1024 * 1024:
                            raise DomainError("adapter_response_too_large")
                    html = bytes(body).decode("utf-8")
            except httpx.TimeoutException:
                raise DomainError("adapter_timeout") from None
            except httpx.HTTPError:
                raise DomainError("adapter_connection_failed") from None
            if any(token in html.lower() for token in ("captcha", "滑动验证", "安全验证")):
                self.paused = True
                raise DomainError("adapter_verification_required")
            # Never execute embedded platform scripts or interpret arbitrary state blobs.
            from html.parser import HTMLParser

            class Metadata(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.items = {}

                def handle_starttag(self, tag, attrs):
                    attrs = dict(attrs)
                    if tag == "meta" and "content" in attrs:
                        self.items[attrs.get("name", attrs.get("property", ""))] = attrs["content"]

            parser = Metadata()
            parser.feed(html)
            title = parser.items.get("og:title")
            if not title:
                raise DomainError("adapter_schema_changed_or_login_required")
            result = {
                "url": self.origin + path,
                "title": title,
                "description": parser.items.get("description"),
                "state": "partial",
                "cached": False,
                "missing": ["complete_text", "images", "video", "comments", "metrics"],
                "requests": self.requests,
            }
            self.cache[path] = result
            return result
        raise DomainError("adapter_rate_limited")
