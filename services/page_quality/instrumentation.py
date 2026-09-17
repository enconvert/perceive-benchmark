"""Re-export of the root-level vendored ``instrumentation.py``.

``scorer.py`` is copied byte-for-byte from the gateway and imports
``services.page_quality.instrumentation``; this module keeps that import
resolving to the same vendored file so both copies can be hash-checked.
"""

from instrumentation import *  # noqa: F401,F403
