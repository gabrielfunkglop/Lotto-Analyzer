"""HTTP layer: Sucuri WAF challenge solving, retries, polite rate limiting, on-disk cache."""
from __future__ import annotations

import base64
import hashlib
import logging
import random
import re
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"


# --------------------------------------------------------------------------
# Sucuri CloudProxy javascript challenge
# --------------------------------------------------------------------------
def _js_concat(expr: str) -> str:
    """Evaluate a JS expression built only from string literals and String.fromCharCode(n)."""
    out = []
    for tok in re.finditer(r"'([^']*)'|\"([^\"]*)\"|String\.fromCharCode\((\d+)\)", expr):
        if tok.group(3) is not None:
            out.append(chr(int(tok.group(3))))
        else:
            out.append(tok.group(1) if tok.group(1) is not None else tok.group(2))
    return "".join(out)


def solve_sucuri(html: str):
    """Return (cookie_name, cookie_value) for a Sucuri JS challenge page, or None."""
    m = re.search(r"S='([A-Za-z0-9+/=]+)'", html)
    if not m:
        return None
    try:
        js = base64.b64decode(m.group(1)).decode(errors="replace")
    except Exception:
        return None
    mv = re.match(r"\s*(\w+)\s*=\s*(.*?);document\.cookie=(.*?);\s*location", js, re.S)
    if not mv:
        return None
    value = _js_concat(mv.group(2))
    name = _js_concat(mv.group(3).split('"="')[0])
    if not name or not value:
        return None
    return name, value


class Fetcher:
    """requests.Session wrapper with challenge solving, retry/backoff and a file cache."""

    def __init__(self, delay=1.2, jitter=0.6, retries=4, cache=True, cache_dir=None,
                 burst=None, cooldown=75.0, block_sleep=120.0, connect_timeout=12.0):
        self.s = None
        self._reset_session()
        self.delay = delay
        self.jitter = jitter
        self.retries = retries
        self.cache = cache
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last = 0.0
        # The archive mirror silently drops connections after a short burst and
        # recovers about a minute later, so pace in bursts instead of fighting it.
        self.burst = burst
        self.cooldown = cooldown
        self.block_sleep = block_sleep
        self.connect_timeout = connect_timeout
        self._in_burst = 0
        self.on_reset = None          # hook so callers can drop per-session tokens

    def _reset_session(self):
        """Build a fresh session.

        A long-lived session eventually starts getting its connections dropped by
        the archive mirror even though a brand-new client from the same address
        connects fine, so throwing the pool and cookies away is the cure.
        """
        old = self.s
        if old is not None:
            try:
                old.close()
            except Exception:                            # noqa: BLE001
                pass
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if getattr(self, "on_reset", None):
            self.on_reset()

    # -- internals ---------------------------------------------------------
    def _wait(self):
        if self.burst and self._in_burst >= self.burst:
            log.info("burst limit reached, recycling session and cooling down %.0fs",
                     self.cooldown)
            self._reset_session()
            time.sleep(self.cooldown)
            self._in_burst = 0
        gap = time.time() - self._last
        want = self.delay + random.random() * self.jitter
        if gap < want:
            time.sleep(want - gap)
        self._last = time.time()
        self._in_burst += 1

    def _cache_path(self, method, url, data):
        key = hashlib.sha256(
            f"{method}|{url}|{sorted((data or {}).items())}".encode()
        ).hexdigest()[:32]
        host = re.sub(r"[^a-z0-9]+", "-", url.split("//", 1)[-1].split("/", 1)[0].lower())
        d = self.cache_dir / host
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{key}.html"

    def _challenge(self, resp):
        nv = solve_sucuri(resp.text)
        if not nv:
            return False
        domain = "." + ".".join(resp.url.split("//", 1)[-1].split("/", 1)[0].split(".")[-2:])
        self.s.cookies.set(nv[0], nv[1], domain=domain, path="/")
        log.debug("solved sucuri challenge for %s", domain)
        return True

    # -- public ------------------------------------------------------------
    def request(self, method, url, data=None, use_cache=None, timeout=45):
        use_cache = self.cache if use_cache is None else use_cache
        cp = self._cache_path(method, url, data)
        if use_cache and cp.exists() and cp.stat().st_size > 0:
            return cp.read_text(encoding="utf-8"), None

        last_exc = None
        for attempt in range(self.retries):
            try:
                self._wait()
                tmo = (self.connect_timeout, timeout)
                r = self.s.request(method, url, data=data, timeout=tmo)
                if "sucuri_cloudproxy_js" in r.text and self._challenge(r):
                    self._wait()
                    r = self.s.request(method, url, data=data, timeout=tmo)
                if r.status_code >= 500 or r.status_code == 429:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                if use_cache and r.status_code == 200:
                    cp.write_text(r.text, encoding="utf-8")
                return r.text, r
            except Exception as e:                      # noqa: BLE001 - retry anything transient
                last_exc = e
                msg = str(e).lower()
                blocked = ("timed out" in msg or "aborted" in msg
                           or "connection" in msg)
                if blocked:
                    # throttled at the connection level: drop the poisoned pool
                    # and wait out the ban window before trying again
                    self._reset_session()
                    back = min(self.block_sleep * (1 + attempt * 0.5), 240)
                    self._in_burst = 0
                else:
                    back = min(300, 3 ** attempt * 2) + random.random() * 3
                log.warning("fetch failed (%s/%s) %s %s: %s - sleeping %.0fs",
                            attempt + 1, self.retries, method, url,
                            str(e)[:120], back)
                time.sleep(back)
        raise RuntimeError(f"giving up on {method} {url}: {last_exc}")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, data, **kw):
        return self.request("POST", url, data=data, **kw)

    def get_json(self, url, **kw):
        import json
        text, resp = self.request("GET", url, **kw)
        return json.loads(text), resp
