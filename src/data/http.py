"""Tiny JSON-over-HTTPS helper using only the standard library."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "trading-bot-paper/0.2 (+https://github.com/AdamEthan/trading-bot)"


class DataError(RuntimeError):
    """A feed could not be reached or returned something unusable."""


def get_json(url: str, params: dict | None = None, timeout: float = 10.0):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        hint = " (rate limited - try again in a few minutes)" if exc.code == 429 else ""
        raise DataError(f"HTTP {exc.code} from {url}{hint}") from exc
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise DataError(f"Could not reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DataError(f"Bad JSON from {url}") from exc
