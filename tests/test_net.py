"""TLS trust store fallback for Python builds that ship without CA certificates."""

import builtins
import ssl
import unittest
from unittest import mock

from dispatchbox import net


def empty_context(*args, **kwargs):
    return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verifies, but trusts nothing


class SSLContextTest(unittest.TestCase):
    def setUp(self):
        net.ssl_context.cache_clear()
        self.addCleanup(net.ssl_context.cache_clear)

    def test_uses_certifi_when_the_system_store_is_empty(self):
        try:
            import certifi  # noqa: F401
        except ImportError:
            self.skipTest("certifi is not installed")
        with mock.patch.object(net.ssl, "create_default_context", empty_context):
            ctx = net.ssl_context()
        self.assertGreater(ctx.cert_store_stats()["x509_ca"], 0)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_warns_with_a_fix_when_nothing_is_available(self):
        real_import = builtins.__import__

        def no_certifi(name, *args, **kwargs):
            if name == "certifi":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with mock.patch.object(net.ssl, "create_default_context", empty_context), \
                mock.patch.object(builtins, "__import__", no_certifi), \
                self.assertLogs("dispatchbox.net", "WARNING") as logs:
            ctx = net.ssl_context()
        self.assertEqual(ctx.cert_store_stats()["x509_ca"], 0)
        self.assertIn("pip install certifi", logs.output[0])

    def test_keeps_a_working_system_store_as_is(self):
        ctx = ssl.create_default_context()
        if not ctx.cert_store_stats()["x509_ca"]:
            self.skipTest("this Python has no system CA certificates")
        with mock.patch.object(net.ssl, "create_default_context", lambda: ctx):
            self.assertIs(net.ssl_context(), ctx)


class ContextForTest(unittest.TestCase):
    def test_plain_http_needs_no_trust_store(self):
        with mock.patch.object(net, "ssl_context") as ctx:
            self.assertIsNone(net.context_for("http://127.0.0.1:8787/events"))
            ctx.assert_not_called()

    def test_https_uses_the_shared_context(self):
        with mock.patch.object(net, "ssl_context", return_value="ctx"):
            self.assertEqual(net.context_for("HTTPS://hub.example.com/claim"), "ctx")


if __name__ == "__main__":
    unittest.main()
