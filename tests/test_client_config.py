from __future__ import annotations

import unittest
from unittest.mock import patch

from doraymon.client import MyClient
from doraymon.config import Settings


class ClientConfigTest(unittest.TestCase):
    @patch("doraymon.client.botpy.Client.__init__", return_value=None)
    def test_sandbox_setting_is_forwarded_to_botpy_client(self, client_init_mock) -> None:
        for sandbox_enabled in (True, False):
            with self.subTest(sandbox_enabled=sandbox_enabled):
                MyClient(Settings(qqbot_sandbox=sandbox_enabled))

                self.assertEqual(
                    client_init_mock.call_args.kwargs["is_sandbox"],
                    sandbox_enabled,
                )

    @patch("doraymon.client.botpy.Client.__init__", return_value=None)
    def test_explicit_botpy_sandbox_argument_keeps_priority(self, client_init_mock) -> None:
        MyClient(Settings(qqbot_sandbox=True), is_sandbox=False)

        self.assertFalse(client_init_mock.call_args.kwargs["is_sandbox"])


if __name__ == "__main__":
    unittest.main()
