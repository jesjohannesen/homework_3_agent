"""Tiny HTTP layer: one transport function, one error type, one retry helper."""
import random
import socket
import time
import urllib.error
import urllib.request


class NetError(Exception):
    """retryable: safe to retry automatically (request definitely not applied, or a read).
    ambiguous: a write may or may not have been applied -- caller must reconcile, not retry blindly."""

    def __init__(self, msg, status=None, retryable=False, ambiguous=False):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable
        self.ambiguous = ambiguous


def http(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise TransportError(type(e).__name__) from None


class TransportError(Exception):
    pass


def retry(fn, *, attempts=4, base=1.0, cap=30.0, sleep=time.sleep, rng=random.random, on_retry=None):
    """Exponential backoff with jitter; only retries NetError(retryable=True)."""
    for i in range(attempts):
        try:
            return fn()
        except NetError as e:
            if not e.retryable or i == attempts - 1:
                raise
            delay = min(cap, base * 2 ** i) * (0.5 + rng() / 2)
            if on_retry:
                on_retry(i + 1, delay, e)
            sleep(delay)
