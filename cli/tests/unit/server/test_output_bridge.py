import unittest

from cli.server.serve_app import _OutputBridge


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.published = []

    def publish(self, event, data) -> None:
        self.published.append((event, data))


class OutputBridgeTests(unittest.TestCase):
    def test_write_tagged_does_not_change_default_stream_tag(self):
        broadcaster = _FakeBroadcaster()
        bridge = _OutputBridge(
            broadcaster,
            chat_id_getter=lambda: "chat-1",
            workspace_id_getter=lambda: "ws-1",
        )

        bridge.set_tag("assistant")
        bridge.write_tagged("thinking", "plan")
        bridge.write("reply")

        self.assertEqual(
            broadcaster.published,
            [
                ("thinking", {"text": "plan", "chatId": "chat-1", "workspaceId": "ws-1"}),
                ("assistant", {"text": "reply", "chatId": "chat-1", "workspaceId": "ws-1"}),
            ],
        )

    def test_suppressed_bridge_still_swallows_tagged_writes(self):
        broadcaster = _FakeBroadcaster()
        bridge = _OutputBridge(broadcaster)

        bridge.suppressed = True
        written = bridge.write_tagged("thinking", "hidden")

        self.assertEqual(written, len("hidden"))
        self.assertEqual(broadcaster.published, [])

    def test_tool_envelope_forces_output_event_after_assistant_tag_leaks(self):
        broadcaster = _FakeBroadcaster()
        bridge = _OutputBridge(broadcaster)

        bridge.set_tag("assistant")
        bridge.write("\ue004\u001b[32m•\u001b[0m Read demo.py\ue005")

        self.assertEqual(broadcaster.published[0][0], "output")


if __name__ == "__main__":
    unittest.main()
