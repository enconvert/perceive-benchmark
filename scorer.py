"""8-point render quality scorer (Sprint F.7, V2 plan section 4.2).

Pure module: ``score()`` performs no I/O, touches no globals, and never
mutates its inputs. It reads three things — the rendered HTML, the
Phase-0 ``PageInstrumentation`` capture, and the heuristic structured
payload — and produces a ``RenderQuality`` verdict that downstream code
uses three ways (plan F.7 "Why"):

* ``perceive_flow`` gates Tier-3 LLM extraction on it (no Haiku spend
  on a page that never rendered);
* the score is surfaced in the ``/v2/perceive`` response and persisted
  to ``ch_perceive_operations.render_quality_score``;
* ``/v2/watch`` (Task I.3) will short-circuit a diff when the snapshot
  scored as blocked.

The eight deductions are exactly the section-4.2 list. Score is
``1.0 - sum(deductions)`` clamped to [0.0, 1.0].

Hardening (F.7.1): two classes of failed render used to slip past the
0.40 quality floor and be returned as if usable, so this scorer was
tightened to flag them:

* A near-empty render (title-only stub, blank challenge page, empty SPA
  shell) now deducts 0.7 instead of 0.5, landing at 0.3 — clearly below
  the floor — because a body under 20 visible words is a failed render,
  not thin content.
* Server / WAF block pages that carry no CAPTCHA vocabulary (CloudFront
  "The request could not be satisfied", Akamai "You don't have
  permission to access", PerimeterX "Access to this page has been
  denied", Bloomberg "Are you a robot?") now trip ``bot_detection`` via
  an expanded marker list plus a single-signature strong-marker tier.

Marker-matching semantics (deliberate, validated by the corpus):

* Anti-bot markers match case-insensitive SUBSTRINGS of the raw HTML —
  a Cloudflare interstitial's strongest fingerprints live in markup
  (``cf-browser-verification`` ids, ``challenge-platform`` script
  paths), not visible text.
* Bot-detection / WAF-block markers match the VISIBLE TEXT — a block
  page DISPLAYS its message ("Access denied", "Are you a robot?", "The
  request could not be satisfied"), whereas a real content page can
  carry the same words incidentally inside a script bundle or hidden
  error template and must not be mistaken for a block. A curated set of
  high-confidence block phrases needs only ONE visible occurrence, but
  only on a short page (< 200 words) so a real article that quotes the
  phrase is not misread as a block.
* Login markers match visible text only, on WORD BOUNDARIES, counting
  occurrences — prose like "design inspiration" contains the raw
  substring "sign in" and must not count.

The ``structured`` argument is part of the stable F.7 signature but is
not scored yet: the v2 scorer (plan section 7.4) will use JSON-LD
presence and semantic-tag counts as positive evidence. Accepting it now
keeps the call sites stable when that lands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from instrumentation import PageInstrumentation

# --- Section 4.2 deduction table -------------------------------------------

_ANTI_BOT_MARKERS: tuple[str, ...] = (
    "checking your browser",
    "just a moment",
    "ray id",
    "cf-browser-verification",
    "challenge-platform",
)
_ANTI_BOT_THRESHOLD = 2
_ANTI_BOT_DEDUCTION = 0.6

_BOT_DETECTION_MARKERS: tuple[str, ...] = (
    "access denied",
    "bot detected",
    "captcha",
    "hcaptcha",
    "unusual traffic",
    "unusual activity",  # Bloomberg / PerimeterX "detected unusual activity"
    "are you a robot",  # Bloomberg interstitial
    "request blocked",  # CloudFront / WAF "Request blocked"
    "you don't have permission to access",  # Akamai / Apache 403
    "access to this page has been denied",  # PerimeterX
    "pardon our interruption",  # PerimeterX / Imperva
)
_BOT_DETECTION_THRESHOLD = 2
_BOT_DETECTION_DEDUCTION = 0.5

# Single-signature block/error pages: phrases specific enough to a block or
# WAF error page that ONE is enough to call the render blocked. Guarded by a
# word ceiling (below) so the rare content page that quotes one of these in
# an article body is not mistaken for a block. These fire the same
# ``bot_detection`` deduction so ``is_blocked`` stays derived from one key.
_BOT_DETECTION_STRONG_MARKERS: tuple[str, ...] = (
    "the request could not be satisfied",  # CloudFront generic block
    "attention required! | cloudflare",  # Cloudflare 1020 / block title
    "checking if the site connection is secure",  # Cloudflare interstitial
    "why have i been blocked",  # Cloudflare block explainer
    "please enable cookies and reload the page",  # Cloudflare
)
_BOT_DETECTION_STRONG_MAX_WORDS = 200

_LOGIN_MARKERS: tuple[str, ...] = (
    "sign in",
    "log in",
    "create account",
    "authentication required",
)
_LOGIN_HIT_THRESHOLD = 3
_LOGIN_MAX_WORDS = 200
_LOGIN_DEDUCTION = 0.4

# A render with fewer than 20 visible words is not thin content — it is a
# failed render (an empty SPA shell, a title-only stub, a blank challenge
# page). It must land clearly below the 0.40 quality floor so downstream
# consumers never treat it as usable content, hence a 0.7 deduction (score
# 0.3) rather than the original 0.5 (score 0.5, which slipped past the floor).
_EMPTY_HARD_WORDS = 20
_EMPTY_HARD_DEDUCTION = 0.7
_EMPTY_SOFT_WORDS = 100
_EMPTY_SOFT_DEDUCTION = 0.15

_JS_ERRORS_HARD = 10
_JS_ERRORS_HARD_DEDUCTION = 0.2
_JS_ERRORS_SOFT = 3
_JS_ERRORS_SOFT_DEDUCTION = 0.1

_RESOURCE_FAILURES_HARD = 10
_RESOURCE_FAILURES_HARD_DEDUCTION = 0.15
_RESOURCE_FAILURES_SOFT = 3
_RESOURCE_FAILURES_SOFT_DEDUCTION = 0.05

_REDIRECT_DEDUCTION = 0.1

_SLOW_HARD_MS = 30_000
_SLOW_HARD_DEDUCTION = 0.1
_SLOW_SOFT_MS = 15_000
_SLOW_SOFT_DEDUCTION = 0.05

# Tags whose text content is invisible to a human looking at the render.
_INVISIBLE_TAGS = ("script", "style", "noscript", "template")

# Second-level labels under which a two-label suffix is NOT the
# registered domain (example.co.uk, example.ac.jp, ...). A deliberate
# approximation of the public-suffix list: good enough to keep
# www/accounts/consent subdomain hops from looking like redirects.
_SHARED_SLDS = frozenset({"ac", "co", "com", "edu", "gov", "net", "org"})


@dataclass(frozen=True)
class RenderQuality:
    """Verdict of the 8-point scorer for one render.

    ``deductions`` maps each fired deduction to the amount it removed
    (only fired deductions appear). ``score`` is 1.0 minus their sum,
    clamped to [0.0, 1.0].
    """

    score: float
    is_blocked: bool
    is_login_wall: bool
    deductions: dict[str, float]


def _visible_text(html: str) -> str:
    """Text a human would see: everything outside script/style blocks."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_INVISIBLE_TAGS):
        tag.decompose()
    return soup.get_text(" ")


def _distinct_marker_count(lowered_html: str, markers: tuple[str, ...]) -> int:
    return sum(1 for marker in markers if marker in lowered_html)


def _login_marker_hits(lowered_text: str) -> int:
    return sum(
        len(re.findall(rf"\b{re.escape(marker)}\b", lowered_text))
        for marker in _LOGIN_MARKERS
    )


def _registered_domain(url: Optional[str]) -> Optional[str]:
    """Approximate eTLD+1 so subdomain hops do not count as redirects."""
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in _SHARED_SLDS and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def score(
    html: str,
    instrumentation: PageInstrumentation,
    structured: Optional[dict[str, Any]],
) -> RenderQuality:
    """Score one render against the section-4.2 deduction table.

    Args:
        html: The rendered HTML (post-quality-chain DOM snapshot).
        instrumentation: The Phase-0 capture for this render. Console
            errors, resource failures, load time, and the requested /
            final URLs feed deductions 5-8; missing values (defaults)
            simply leave those deductions unfired.
        structured: Heuristic structured payload for this render.
            Accepted but not yet scored — reserved for the section-7.4
            scorer v2 (JSON-LD presence, semantic-tag counts).

    Returns:
        An immutable RenderQuality verdict.
    """
    del structured  # Reserved for scorer v2 (plan section 7.4).

    lowered_html = (html or "").lower()
    visible = _visible_text(html or "")
    lowered_text = visible.lower()
    word_count = len(visible.split())

    deductions: dict[str, float] = {}

    # 1. Cloudflare / anti-bot challenge.
    anti_bot_markers = _distinct_marker_count(lowered_html, _ANTI_BOT_MARKERS)
    if anti_bot_markers >= _ANTI_BOT_THRESHOLD:
        deductions["anti_bot_challenge"] = _ANTI_BOT_DEDUCTION

    # 2. Bot detection / CAPTCHA / WAF block page. Two generic markers, OR a
    # single high-confidence block signature on a short page (a full content
    # page that merely quotes one of these phrases stays under the ceiling).
    # Matched against VISIBLE TEXT, not raw HTML: a block page DISPLAYS its
    # message ("Access denied", "Are you a robot?"), whereas a real content
    # page can carry the same words incidentally in a script bundle or hidden
    # error template (e.g. a crypto dashboard shipping "captcha" / "unusual
    # traffic" strings) — those must not be mistaken for a block.
    bot_markers = _distinct_marker_count(lowered_text, _BOT_DETECTION_MARKERS)
    strong_block = word_count < _BOT_DETECTION_STRONG_MAX_WORDS and any(
        marker in lowered_text for marker in _BOT_DETECTION_STRONG_MARKERS
    )
    if bot_markers >= _BOT_DETECTION_THRESHOLD or strong_block:
        deductions["bot_detection"] = _BOT_DETECTION_DEDUCTION

    # 3. Login wall: repeated auth vocabulary on a near-empty page.
    if (
        _login_marker_hits(lowered_text) >= _LOGIN_HIT_THRESHOLD
        and word_count < _LOGIN_MAX_WORDS
    ):
        deductions["login_wall"] = _LOGIN_DEDUCTION

    # 4. Empty body.
    if word_count < _EMPTY_HARD_WORDS:
        deductions["empty_body"] = _EMPTY_HARD_DEDUCTION
    elif word_count < _EMPTY_SOFT_WORDS:
        deductions["empty_body"] = _EMPTY_SOFT_DEDUCTION

    # 5. JavaScript errors (Phase-0 console capture).
    if instrumentation.console_error_count > _JS_ERRORS_HARD:
        deductions["js_errors"] = _JS_ERRORS_HARD_DEDUCTION
    elif instrumentation.console_error_count > _JS_ERRORS_SOFT:
        deductions["js_errors"] = _JS_ERRORS_SOFT_DEDUCTION

    # 6. Failed subresource loads.
    if instrumentation.resource_failure_count > _RESOURCE_FAILURES_HARD:
        deductions["resource_failures"] = _RESOURCE_FAILURES_HARD_DEDUCTION
    elif instrumentation.resource_failure_count > _RESOURCE_FAILURES_SOFT:
        deductions["resource_failures"] = _RESOURCE_FAILURES_SOFT_DEDUCTION

    # 7. Cross-domain redirect (paywall / consent bounce).
    requested_domain = _registered_domain(instrumentation.requested_url)
    final_domain = _registered_domain(instrumentation.final_url)
    if (
        requested_domain is not None
        and final_domain is not None
        and requested_domain != final_domain
    ):
        deductions["domain_redirect"] = _REDIRECT_DEDUCTION

    # 8. Slow render.
    if instrumentation.page_load_time_ms > _SLOW_HARD_MS:
        deductions["slow_render"] = _SLOW_HARD_DEDUCTION
    elif instrumentation.page_load_time_ms > _SLOW_SOFT_MS:
        deductions["slow_render"] = _SLOW_SOFT_DEDUCTION

    total = round(1.0 - sum(deductions.values()), 4)
    return RenderQuality(
        score=max(0.0, min(1.0, total)),
        is_blocked=(
            "anti_bot_challenge" in deductions or "bot_detection" in deductions
        ),
        is_login_wall="login_wall" in deductions,
        deductions=deductions,
    )
