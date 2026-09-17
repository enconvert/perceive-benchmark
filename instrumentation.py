"""V2 Phase 0 instrumentation for Playwright-backed URL conversions.

Captures the per-render signals that downstream V2 work depends on:

1. The fully rendered HTML (after `wait_for_load_state("networkidle")`)
   as input for extraction (Perceive), change detection (Watch), and as
   a deduplication / cache key (`content_hash`).
2. A SHA-256 of that HTML for cheap equality checks.
3. A console error count, sourced from `page.on("console")` +
   `page.on("pageerror")`, used by the V2 quality scorer.
4. Time from navigation start to networkidle (`page_load_time_ms`).
5. A failed-subresource count (`resource_failure_count`): image / CSS /
   JS / font / media requests that either failed at the network layer
   or answered >= 400. Main-document failures are not counted here —
   those surface as conversion errors upstream. (F.7 scorer input.)
6. The requested and final URLs (`requested_url` / `final_url`) so the
   F.7 scorer can detect cross-domain redirects (paywall / consent
   bounces). `requested_url` is set by the orchestrating caller;
   `final_url` is recorded at capture() time.

Design notes:

* Listeners must be attached BEFORE `page.goto()` or the early console
  output from the page is lost. The orchestrating caller is responsible
  for that ordering — see the converter wiring.
* Capture is opt-out via the `X-Enconvert-No-Capture: true` HTTP header
  on the API request. The caller decides whether to instantiate at all;
  this module does not parse HTTP headers itself.
* The class is single-use — one `PageInstrumentation` per Playwright
  page. It carries mutable state and is intentionally not thread-safe;
  each converter call gets its own.
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
# Documented in the Privacy policy alongside the 90-day retention window.
OPT_OUT_HEADER = "x-enconvert-no-capture"

# Bounded wait so a chronically chatty page (analytics beacons, polling
# WebSockets) does not stall conversion. networkidle is best-effort; the
# converter has already done its own readiness work by the time this
# fires.
_NETWORKIDLE_TIMEOUT_MS = 10_000

# Console levels treated as errors for V2 quality scoring. `warning`
# triggers too aggressively on real-world pages, so it's excluded.
_ERROR_LEVELS = frozenset({"error"})

# Subresource types whose load failures count toward
# `resource_failure_count`. Documents are excluded on purpose (a failed
# navigation is a conversion error, not a quality signal) and so are
# xhr/fetch (API polling that 404s is routine on real pages and would
# swamp the signal).
_RESOURCE_FAILURE_TYPES = frozenset(
    {"image", "stylesheet", "script", "font", "media"}
)


def header_opts_out(headers: Optional[dict]) -> bool:
    """Return True iff the caller asked us to skip rendered-HTML capture.

    Accepts either a Starlette/FastAPI Headers object or a plain dict.
    Header names are matched case-insensitively. Any non-true value is
    treated as opt-in (the default).
    """
    if not headers:
        return False
    # Starlette Headers and dicts both support `.get`. Lowercase key
    # works for Starlette (it normalises), and we fall back to scanning
    # the items for a dict the caller forgot to normalise.
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

    # Populated by the caller.
    skip: bool = False
    """When True, attach() and capture() are no-ops. Set via
    `PageInstrumentation.from_headers(headers)` when the consumer opted out."""

    requested_url: Optional[str] = None
    """The URL the caller asked to render. Set by the orchestrating
    converter (not by attach(): the page has not navigated yet there)."""

    # Populated by attach() and the listener callbacks.
    nav_start: Optional[float] = None
    console_error_count: int = 0
    resource_failure_count: int = 0

    # Populated by capture().
    rendered_html: Optional[str] = None
    content_hash: Optional[str] = None
    page_load_time_ms: int = 0
    final_url: Optional[str] = None

    # HTTP status of the final MAIN-DOCUMENT response (D1, QA report
    # 2026-08-06). Set by the orchestrating engine: the Chromium flow
    # reads it off the ``after_goto`` Response, the TLS engine off the
    # final hop. None when the engine could not observe it. Feeds the
    # scorer's http_error deduction and the API's ``status_code`` field.
    http_status: Optional[int] = None

    # Internal: stash listener refs so we can detach on capture(). Kept
    # private; not relied on outside this module.
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

        Safe to call when `skip` is True — this becomes a no-op. Must
        run BEFORE `page.goto()` to catch the page's earliest output.
        """
        if self.skip:
            return

        def on_console(msg: ConsoleMessage) -> None:
            # ConsoleMessage.type is "log" | "warning" | "error" | ...
            try:
                if msg.type in _ERROR_LEVELS:
                    self.console_error_count += 1
            except Exception:
                # A misbehaving page that throws inside type access
                # should never break the parent conversion.
                pass

        def on_pageerror(err: PlaywrightError) -> None:
            # Uncaught JS exceptions are unambiguously errors.
            self.console_error_count += 1
            logger.debug("pageerror captured: %s", err)

        def on_requestfailed(request: Request) -> None:
            # Network-layer subresource failures (DNS, abort, reset).
            try:
                if request.resource_type in _RESOURCE_FAILURE_TYPES:
                    self.resource_failure_count += 1
            except Exception:
                # Never let a misbehaving event break the conversion.
                pass

        def on_response(response: Response) -> None:
            # HTTP-layer subresource failures (4xx/5xx). Disjoint from
            # requestfailed: a response means the network layer worked.
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

        Failures here must never propagate — instrumentation is observability,
        not a load-bearing path. A render that succeeded with missing
        instrumentation is still a success.
        """
        if self.skip:
            return
        try:
            # Record the post-navigation URL first: it is a cheap sync
            # read and stays useful even if the HTML snapshot fails.
            self.final_url = page.url
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=_NETWORKIDLE_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                # Some pages never reach networkidle (long-poll, SSE,
                # analytics heartbeats). Capture anyway — the converter
                # has its own readiness logic above this layer.
                logger.debug("networkidle not reached within %dms; capturing anyway",
                             _NETWORKIDLE_TIMEOUT_MS)

            html = await page.content()
            self.rendered_html = html
            self.content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
            if self.nav_start is not None:
                self.page_load_time_ms = int(
                    (time.monotonic() - self.nav_start) * 1000
                )
        except Exception as exc:
            # Log and swallow — see docstring.
            logger.warning("PageInstrumentation.capture failed: %s", exc)
