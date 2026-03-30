from __future__ import annotations

import unittest

from agent_broker_v2.protocol import (
    ProtocolError,
    build_data_message,
    build_stopped_message,
    build_start_message,
    validate_broker_message,
    validate_client_message,
)


class V2ProtocolTests(unittest.TestCase):
    def test_start_requires_non_null_cwd(self) -> None:
        message = build_start_message(
            tool="echo",
            argv=[],
            cwd="",
            timeout_ms=1000,
            env_delta={},
        )

        with self.assertRaises(ProtocolError) as ctx:
            validate_client_message(message)

        self.assertEqual(ctx.exception.code, "BAD_CWD")

    def test_start_allows_zero_timeout_for_disabled_client_timeout(self) -> None:
        message = build_start_message(
            tool="echo",
            argv=[],
            cwd="/tmp",
            timeout_ms=0,
            env_delta={},
        )

        validated = validate_client_message(message)
        self.assertEqual(validated["timeout_ms"], 0)

    def test_signal_channel_rejects_stdio_fields(self) -> None:
        message = build_data_message(
            "req-1",
            channel="signal",
            signal_name="SIGTERM",
        )
        message["data_b64"] = "abc"

        with self.assertRaises(ProtocolError) as ctx:
            validate_client_message(message)

        self.assertEqual(ctx.exception.code, "BAD_SIGNAL")

    def test_broker_stopped_exit_variant_requires_exit_code(self) -> None:
        message = build_stopped_message("req-1", reason="exit")

        with self.assertRaises(ProtocolError) as ctx:
            validate_broker_message(message)

        self.assertEqual(ctx.exception.code, "BAD_EXIT_CODE")

    def test_broker_stopped_signal_variant_rejects_exit_code(self) -> None:
        message = build_stopped_message(
            "req-1",
            reason="signal",
            signal="SIGTERM",
            exit_code=143,
        )

        with self.assertRaises(ProtocolError) as ctx:
            validate_broker_message(message)

        self.assertEqual(ctx.exception.code, "BAD_FIELDS")


if __name__ == "__main__":
    unittest.main()
