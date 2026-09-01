"""TLS helpers that trust the OS certificate store.

``requests`` verifies against the ``certifi`` Mozilla bundle only. On Windows
that misses enterprise root CAs already trusted by the OS (typical with HTTPS
inspection proxies), producing ``CERTIFICATE_VERIFY_FAILED: self-signed
certificate in certificate chain`` even though the same host works in a
browser. This module verifies against the OS trust store (and certifi as an
extra), and still rejects certificates the OS does not trust.
"""

from __future__ import annotations

import ssl
import sys
from typing import Any

import requests
from requests.adapters import HTTPAdapter


def create_ssl_context() -> ssl.SSLContext:
    """Build a client SSL context that trusts OS CAs plus certifi.

    Certificates in the system trust store (Windows ROOT/CA, macOS keychain,
    OpenSSL default paths) are accepted. Untrusted self-signed certificates
    are still rejected.

    On Python 3.10+ the optional ``truststore`` package is preferred when
    installed: it verifies with the native OS TLS stack (Schannel / Secure
    Transport), which matches browser trust more closely than OpenSSL's copy
    of the Windows store.
    """
    if sys.version_info >= (3, 10):
        try:
            import truststore  # type: ignore[import-untyped]

            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            return ctx
        except ImportError:
            pass

    # ``create_default_context()`` already calls ``load_default_certs()``,
    # which on Windows enumerates the CA and ROOT system stores.
    ctx = ssl.create_default_context()
    try:
        import certifi  # type: ignore[import-untyped]

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


class SSLContextAdapter(HTTPAdapter):
    """Requests adapter that uses a caller-supplied SSL context for TLS."""

    def __init__(self, ssl_context: ssl.SSLContext, **kwargs: Any) -> None:
        self._ssl_context = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["ssl_context"] = self._ssl_context
        return super().init_poolmanager(
            connections, maxsize, block=block, **pool_kwargs
        )

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self._ssl_context
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def https_session() -> requests.Session:
    """Return a ``requests.Session`` that verifies TLS with ``create_ssl_context()``."""
    session = requests.Session()
    adapter = SSLContextAdapter(create_ssl_context())
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
