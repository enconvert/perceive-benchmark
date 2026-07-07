"""V2 Phase 0 instrumentation for Playwright-backed URL conversions.

Vendored VERBATIM from the Enconvert gateway
(`services/page_quality/instrumentation.py`). This module attaches the
console / page-error / failed-subresource listeners to a Playwright page
and, on capture(), snapshots the rendered HTML plus the timing and error
counts that the F.7 scorer (see scorer.py) consumes.

Captures the per-render signals that downstream V2 work depends on:

1. The fully rendered HTML (after `wait_for_load_state("networkidle")`).
2. A SHA-256 of that HTML for cheap equality checks.
3. A console error count, sourced from `page.on("console")` +
   `page.on("pageerror")`, used by the V2 quality scorer.
4. Time from navigation start to networkidle (`page_load_time_ms`).
5. A failed-subresource count (`resource_failure_count`).
6. The requested and final URLs (`requested_url` / `final_url`).
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import (
    ConsoleMessage,
    Error as PlaywrightError,
    Page,
    Request,
    Response,
    TimeoutError as PlaywrightTimeoutError,
)

logger = logging.getLogger(__name__)

# Opt-out request header consumers can send to skip rendered-HTML capture.
OPT_OUT_HEADER = "x-enconvert-no-capture"

# Bounded wait so a chronically chatty page (analytics beacons, polling
# WebSockets) does not stall conversion.
_NETWORKIDLE_TIMEOUT_MS = 10_000

# Console levels treated as errors for V2 quality scoring. `warning`
# triggers too aggressively on real-world pages, so it's excluded.
_ERROR_LEVELS = frozenset({"error"})

# Subresource types whose load failures count toward
# `resource_failure_count`. Documents are excluded on purpose (a failed
# navigation is a conversion error, not a quality signal) and so are
# xhr/fetch (API polling that 404s is routine on real pages).
_RESOURCE_FAILURE_TYPES = frozenset(
    {"image", "stylesheet", "script", "font", "media"}
)


def header_opts_out(headers: Optional[dict]) -> bool:
    """Return True iff the caller asked us to skip rendered-HTML capture."""
    if not headers:
        return False
    raw = headers.get(OPT_OUT_HEADER) if hasattr(headers, "get") else None
    if raw is None and isinstance(headers, dict):
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == OPT_OUT_HEADER:
                raw = value
                break
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class PageInstrumentation:
    """One-shot recorder for a single Playwright page render.

    The orchestrating converter calls `attach(page)` before `page.goto`,
    then `capture(page)` once the page has reached load. Results live on
    the instance for the caller to persist after upload succeeds.
    """

    skip: bool = False
    requested_url: Optional[str] = None

    nav_start: Optional[float] = None
    console_error_count: int = 0
    resource_failure_count: int = 0

    rendered_html: Optional[str] = None
    content_hash: Optional[str] = None
    page_load_time_ms: int = 0
    final_url: Optional[str] = None

    _console_listener: Optional[object] = field(default=None, repr=False)
    _error_listener: Optional[object] = field(default=None, repr=False)
    _requestfailed_listener: Optional[object] = field(default=None, repr=False)
    _response_listener: Optional[object] = field(default=None, repr=False)

    @classmethod
    def from_headers(cls, headers: Optional[dict]) -> "PageInstrumentation":
        """Build an instance respecting the opt-out header."""
        return cls(skip=header_opts_out(headers))

    def attach(self, page: Page) -> None:
        """Register console/error listeners and start the nav-time clock.

        Must run BEFORE `page.goto()` to catch the page's earliest output.
        """
        if self.skip:
            return

        def on_console(msg: ConsoleMessage) -> None:
            try:
                if msg.type in _ERROR_LEVELS:
                    self.console_error_count += 1
            except Exception:
                pass

        def on_pageerror(err: PlaywrightError) -> None:
            self.console_error_count += 1
            logger.debug("pageerror captured: %s", err)

        def on_requestfailed(request: Request) -> None:
            try:
                if request.resource_type in _RESOURCE_FAILURE_TYPES:
                    self.resource_failure_count += 1
            except Exception:
                pass

        def on_response(response: Response) -> None:
            try:
                if (
                    response.status >= 400
                    and response.request.resource_type
                    in _RESOURCE_FAILURE_TYPES
                ):
                    self.resource_failure_count += 1
            except Exception:
                pass

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        page.on("requestfailed", on_requestfailed)
        page.on("response", on_response)
        self._console_listener = on_console
        self._error_listener = on_pageerror
        self._requestfailed_listener = on_requestfailed
        self._response_listener = on_response
        self.nav_start = time.monotonic()

    async def capture(self, page: Page) -> None:
        """Wait for networkidle (bounded), then snapshot HTML + hash + timing.

        Failures here must never propagate — instrumentation is
        observability, not a load-bearing path.
        """
        if self.skip:
            return
        try:
            self.final_url = page.url
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=_NETWORKIDLE_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                logger.debug(
                    "networkidle not reached within %dms; capturing anyway",
                    _NETWORKIDLE_TIMEOUT_MS,
                )

            html = await page.content()
            self.rendered_html = html
            self.content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
            if self.nav_start is not None:
                self.page_load_time_ms = int(
                    (time.monotonic() - self.nav_start) * 1000
                )
        except Exception as exc:
            logger.warning("PageInstrumentation.capture failed: %s", exc)
