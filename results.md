# Benchmark results

- Generated: 2026-07-07T17:50:28Z
- URLs tested (N): **50**
- **Naive fetch** silently returned a block page as content: **52.0%** (26/50) (+1 loud connection errors)
- **Enconvert recovery** — of those silent failures, rendered clean usable content: **65.4%** (17/26)
- **Enconvert** across all 50: clean 39 (78.0%), flagged (is_blocked or q<0.40) 10 (20.0%), render errors 1, **silently returned a block page: 0**

| # | URL | Category | Naive silent-fail (why) | Enconvert q | is_blocked | Outcome |
|---|-----|----------|-------------------------|-------------|------------|---------|
| 1 | https://www.g2.com/ | bot-gated | yes: empty_shell,http_403 | 0.30 | no | flagged |
| 2 | https://www.indeed.com/ | bot-gated | yes: empty_shell,bot_challenge,http_403 | 1.00 | no | clean |
| 3 | https://www.glassdoor.com/ | bot-gated | yes: http_403 | 0.70 | no | clean |
| 4 | https://www.yelp.com/ | bot-gated | yes: empty_shell,http_403 | 0.30 | no | flagged |
| 5 | https://www.tripadvisor.com/ | bot-gated | yes: empty_shell,http_403 | 0.30 | no | flagged |
| 6 | https://www.etsy.com/ | bot-gated | yes: empty_shell,http_403 | 0.30 | no | flagged |
| 7 | https://seatgeek.com/ | bot-gated | yes: empty_shell,http_403 | 0.30 | no | flagged |
| 8 | https://www.leboncoin.fr/ | bot-gated | ok | 0.30 | no | flagged |
| 9 | https://www.coinbase.com/ | bot-gated | ok | 1.00 | no | clean |
| 10 | https://kick.com/ | bot-gated | ok | 0.75 | no | clean |
| 11 | https://www.zillow.com/ | bot-gated | yes: empty_shell,http_403 | 0.35 | yes | flagged |
| 12 | https://stockx.com/ | bot-gated | yes: http_403 | 0.60 | no | clean |
| 13 | https://www.nike.com/ | bot-gated | yes: empty_shell,bot_challenge,http_403 | 0.90 | no | clean |
| 14 | https://www.ticketmaster.com/ | bot-gated | ok | 1.00 | no | clean |
| 15 | https://www.crunchbase.com/ | bot-gated | ok | 1.00 | no | clean |
| 16 | https://www.walmart.com/ | bot-gated | ok | 0.80 | no | clean |
| 17 | https://www.similarweb.com/ | bot-gated | ok | 0.90 | no | clean |
| 18 | https://www.booking.com/ | bot-gated | yes: empty_shell | 1.00 | no | clean |
| 19 | https://www.wsj.com/ | paywalled | yes: empty_shell,http_401 | 0.30 | no | flagged |
| 20 | https://www.barrons.com/ | paywalled | yes: empty_shell,http_401 | 0.30 | no | flagged |
| 21 | https://www.ft.com/ | paywalled | yes: http_403 | 1.00 | no | clean |
| 22 | https://www.economist.com/ | paywalled | yes: empty_shell,bot_challenge,http_403 | 1.00 | no | clean |
| 23 | https://www.bloomberg.com/ | paywalled | ok | 0.50 | yes | flagged |
| 24 | https://www.theinformation.com/ | paywalled | yes: empty_shell,bot_challenge,http_403 | 0.75 | no | clean |
| 25 | https://www.washingtonpost.com/ | paywalled | conn-error | 0.70 | no | clean |
| 26 | https://www.thetimes.com/ | paywalled | yes: empty_shell | err |  | error |
| 27 | https://seekingalpha.com/ | paywalled | ok | 1.00 | no | clean |
| 28 | https://hbr.org/ | paywalled | ok | 1.00 | no | clean |
| 29 | https://www.newyorker.com/ | paywalled | ok | 0.55 | no | clean |
| 30 | https://www.wired.com/ | paywalled | ok | 0.85 | no | clean |
| 31 | https://www.theatlantic.com/ | paywalled | ok | 0.70 | no | clean |
| 32 | https://www.businessinsider.com/ | paywalled | ok | 0.85 | no | clean |
| 33 | https://www.nytimes.com/ | paywalled | ok | 1.00 | no | clean |
| 34 | https://web.telegram.org/ | js-heavy | yes: empty_shell | 0.45 | no | clean |
| 35 | https://www.messenger.com/ | js-heavy | ok | 1.00 | no | clean |
| 36 | https://app.diagrams.net/ | js-heavy | ok | 1.00 | no | clean |
| 37 | https://app.element.io/ | js-heavy | yes: empty_shell | 0.75 | no | clean |
| 38 | https://app.aave.com/ | js-heavy | yes: empty_shell | 1.00 | no | clean |
| 39 | https://open.spotify.com/ | js-heavy | yes: empty_shell | 1.00 | no | clean |
| 40 | https://excalidraw.com/ | js-heavy | yes: empty_shell | 0.85 | no | clean |
| 41 | https://pancakeswap.finance/ | js-heavy | yes: empty_shell | 0.90 | no | clean |
| 42 | https://app.uniswap.org/ | js-heavy | yes: empty_shell | 0.90 | no | clean |
| 43 | https://www.photopea.com/ | js-heavy | ok | 1.00 | no | clean |
| 44 | https://jup.ag/ | js-heavy | ok | 0.90 | no | clean |
| 45 | https://x.com/ | js-heavy | ok | 0.85 | no | clean |
| 46 | https://dexscreener.com/ | js-heavy | yes: empty_shell,bot_challenge,http_403 | 1.00 | no | clean |
| 47 | https://defillama.com/ | js-heavy | ok | 0.90 | no | clean |
| 48 | https://tldraw.com/ | js-heavy | yes: empty_shell | 0.85 | no | clean |
| 49 | https://coinmarketcap.com/ | js-heavy | ok | 1.00 | no | clean |
| 50 | https://www.coingecko.com/ | js-heavy | ok | 0.75 | no | clean |
