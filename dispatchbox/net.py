"""TLS settings shared by every outgoing HTTPS call (Slack, Feishu, webhooks, worker to hub)."""

from __future__ import annotations

import functools
import logging
import ssl

log = logging.getLogger("dispatchbox.net")


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """The system trust store, plus certifi's bundle when the system one is empty.

    python.org builds of Python on macOS ship without CA certificates until "Install Certificates.command"
    is run, and then every HTTPS call fails with CERTIFICATE_VERIFY_FAILED."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats()["x509_ca"]:
        return ctx
    try:
        import certifi
    except ImportError:
        log.warning("this Python has no CA certificates, so HTTPS calls will fail. Fix: pip install certifi, "
                    "or on macOS run \"/Applications/Python 3.x/Install Certificates.command\"")
        return ctx
    ctx.load_verify_locations(certifi.where())
    return ctx


def context_for(url: str) -> ssl.SSLContext | None:
    """TLS settings for url, or None for plain HTTP, so a local hub never triggers the certificate warning."""
    return ssl_context() if url.lower().startswith("https:") else None
