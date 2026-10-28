"""Runtime profiles and probes use synthetic accounts and never load .env."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from scripts.napcat_runtime import probe, redact, runtime_environment


class NapCatRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = {"account": "10001", "wsUrl": "ws://127.0.0.1:3001", "wsToken": "synthetic-token", "webuiToken": "synthetic-webui"}
        self.profile = {"accessMode": "allowlist", "friends": [20002], "groups": [], "dataDir": "/tmp/synthetic/data", "logDir": "/tmp/synthetic/logs", "leasePath": "/tmp/synthetic/lease.json"}

    def test_preserves_explicit_access_scope_and_storage(self):
        environment = runtime_environment(self.config, self.profile)
        self.assertEqual(environment["BOT_QQ_ACCOUNT_ACCESS_MODE"], "allowlist")
        self.assertEqual(environment["BOT_QQ_ACCOUNT_FRIENDS"], "20002")
        self.assertEqual(environment["BOT_QQ_ACCOUNT_GROUPS"], "")
        self.assertEqual(environment["DATA_DIR"], "/tmp/synthetic/data")

    def test_empty_or_invalid_allowlist_does_not_open_access(self):
        for friends in ([], ["not-a-qq-number"]):
            with self.subTest(friends=friends), self.assertRaises(ValueError):
                runtime_environment(self.config, dict(self.profile, friends=friends))

    def test_invalid_access_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            runtime_environment(self.config, dict(self.profile, accessMode="typo"))

    def test_authentication_is_required(self):
        with self.assertRaises(ValueError):
            runtime_environment(dict(self.config, wsToken=""), self.profile)

    def test_both_management_and_onebot_credentials_are_redacted(self):
        self.assertEqual(redact("synthetic-token synthetic-webui", self.config), "<redacted> <redacted>")

    def test_probe_checks_identity_and_online_state_without_sending_messages(self):
        for account, online in ((10001, True), (10001, False), (99999, True)):
            with self.subTest(account=account, online=online):
                connection = AsyncMock()
                connection.receive_json.side_effect = [
                    {"echo": action, "status": "ok", "retcode": 0, "data": data}
                    for action, data in (
                        ("get_login_info", {"user_id": account}),
                        ("get_status", {"online": online}),
                        ("get_version_info", {"app_version": "synthetic-version"}),
                    )
                ]
                with patch("aiohttp.ClientSession") as session_factory:
                    session = MagicMock()
                    session_factory.return_value.__aenter__.return_value = session
                    session.ws_connect.return_value.__aenter__.return_value = connection
                    if account != 10001:
                        with self.assertRaisesRegex(RuntimeError, "does not match"):
                            asyncio.run(probe(self.config))
                    else:
                        result = asyncio.run(probe(self.config))
                        self.assertIs(result["online"], online)
                        self.assertEqual(result["qqMessagesSent"], 0)
                self.assertEqual([call.args[0]["action"] for call in connection.send_json.call_args_list], ["get_login_info", "get_status", "get_version_info"])


if __name__ == "__main__":
    unittest.main()
