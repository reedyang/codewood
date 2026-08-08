import socket
import struct
import threading
import time
import unittest

from cli.server.serve_app import _make_handler


class _AppStub:
    token = "test-token"


class AbortedRequestSilenceTests(unittest.TestCase):
    """A client aborting mid-request (AbortController, page unload) must be
    swallowed by the handler instead of leaking a socketserver traceback."""

    def test_client_reset_is_swallowed(self):
        handler_cls = _make_handler(_AppStub())
        server_sock, client_sock = socket.socketpair()
        errors = []

        def serve() -> None:
            try:
                handler_cls(server_sock, server_sock, ("127.0.0.1", 0))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        time.sleep(0.05)
        # Force an RST (not a clean FIN) so the server's recv raises
        # ConnectionAbortedError/ConnectionResetError instead of EOF.
        client_sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        client_sock.close()
        thread.join(3)
        server_sock.close()

        self.assertFalse(errors, f"handler leaked an exception: {errors}")
        self.assertFalse(thread.is_alive(), "handler thread did not exit")

    def test_clean_eof_still_closes(self):
        handler_cls = _make_handler(_AppStub())
        server_sock, client_sock = socket.socketpair()
        client_sock.close()  # clean EOF: handler should exit without errors
        try:
            handler_cls(server_sock, server_sock, ("127.0.0.1", 0))
        finally:
            server_sock.close()


if __name__ == "__main__":
    unittest.main()
