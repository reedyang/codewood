import os
import ssl
import tempfile
import unittest
from unittest import mock

from cli.config import tls
from cli.tools import embedding


class CreateSslContextTests(unittest.TestCase):
    def test_requires_verified_peer_and_hostname(self):
        ctx = tls.create_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_loads_certifi_when_truststore_is_unavailable(self):
        fake_ctx = ssl.create_default_context()
        fake_ctx.load_verify_locations = mock.Mock(wraps=fake_ctx.load_verify_locations)
        with mock.patch.dict("sys.modules", {"truststore": None}):
            with mock.patch.object(tls.ssl, "create_default_context", return_value=fake_ctx):
                with mock.patch.object(tls.sys, "version_info", (3, 9, 0)):
                    ctx = tls.create_ssl_context()
        self.assertIs(ctx, fake_ctx)
        fake_ctx.load_verify_locations.assert_called()
        kwargs = fake_ctx.load_verify_locations.call_args.kwargs
        self.assertTrue(kwargs.get("cafile"))

    def test_prefers_truststore_on_python_310(self):
        truststore_ctx = mock.Mock()
        fake_truststore = mock.Mock()
        fake_truststore.SSLContext.return_value = truststore_ctx
        with mock.patch.dict("sys.modules", {"truststore": fake_truststore}):
            with mock.patch.object(tls.sys, "version_info", (3, 10, 0)):
                ctx = tls.create_ssl_context()
        self.assertIs(ctx, truststore_ctx)
        self.assertTrue(truststore_ctx.check_hostname)
        self.assertEqual(truststore_ctx.verify_mode, ssl.CERT_REQUIRED)
        fake_truststore.SSLContext.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)


class HttpsSessionTests(unittest.TestCase):
    def test_https_adapter_uses_os_trust_ssl_context(self):
        session = tls.https_session()
        try:
            adapter = session.adapters["https://"]
            self.assertIsInstance(adapter, tls.SSLContextAdapter)
            self.assertEqual(adapter._ssl_context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(adapter._ssl_context.check_hostname)
        finally:
            session.close()


class HfDownloadTlsTests(unittest.TestCase):
    def test_hf_download_uses_https_session_not_certifi_only_requests(self):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=1):
                yield b"onnx-bytes"

        class FakeSession:
            def __init__(self):
                self.got = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, **kwargs):
                self.got = (url, kwargs)
                return FakeResp()

        fake = FakeSession()
        with tempfile.TemporaryDirectory() as td:
            dest = os.path.join(td, "model.onnx")
            with mock.patch("cli.config.tls.https_session", return_value=fake):
                ok = embedding._hf_download("onnx/model.onnx", dest)
            self.assertTrue(ok)
            self.assertTrue(os.path.isfile(dest))
            with open(dest, "rb") as fh:
                self.assertEqual(fh.read(), b"onnx-bytes")
            self.assertIsNotNone(fake.got)
            self.assertIn("huggingface.co", fake.got[0])
            self.assertTrue(fake.got[0].endswith("onnx/model.onnx"))
            self.assertTrue(fake.got[1].get("allow_redirects"))
            self.assertNotEqual(fake.got[1].get("verify"), False)


if __name__ == "__main__":
    unittest.main()
