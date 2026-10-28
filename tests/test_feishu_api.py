"""The Feishu sending half: token caching, call shapes, and failure honesty."""

from __future__ import annotations

import json
import unittest

from services.feishu_adapter import (
    ACTION_BOT_INFO,
    ACTION_CREATE_MESSAGE,
    ACTION_GET_MESSAGE,
    ACTION_REPLY_MESSAGE,
    FeishuAction,
)
from services.feishu_api import (
    FEISHU_BASE_URL,
    LARK_BASE_URL,
    FeishuApi,
    FeishuApiError,
    build_feishu_api,
)
from services.send_result import ERROR_HTTP_FORMAT, classify_send_error

TOKEN_PATH = "/auth/v3/tenant_access_token/internal"
SECRET = "s3cret-app-secret"


class FakeResponse:
    def __init__(self, payload=None, *, status: int = 200, json_error: bool = False) -> None:
        self.status_code = status
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


class FakeRequester:
    """Answers the token call by itself and queues responses for the rest."""

    def __init__(self, *, token=None, default=None) -> None:
        self.calls: list[dict] = []
        self.token_payload = (
            token
            if token is not None
            else {"code": 0, "msg": "ok", "tenant_access_token": "t-cached", "expire": 7200}
        )
        self.default = default or FakeResponse({"code": 0, "msg": "ok", "data": {}})
        self.queued: list[FakeResponse] = []

    def queue(self, response: FakeResponse) -> "FakeRequester":
        self.queued.append(response)
        return self

    def __call__(self, method, url, **kwargs):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": kwargs.get("headers") or {},
                "params": kwargs.get("params"),
                "json": kwargs.get("json"),
                "timeout": kwargs.get("timeout"),
            }
        )
        if url.endswith(TOKEN_PATH):
            if isinstance(self.token_payload, FakeResponse):
                return self.token_payload
            return FakeResponse(self.token_payload)
        if self.queued:
            return self.queued.pop(0)
        return self.default

    @property
    def token_calls(self) -> list[dict]:
        return [call for call in self.calls if call["url"].endswith(TOKEN_PATH)]

    @property
    def api_calls(self) -> list[dict]:
        return [call for call in self.calls if not call["url"].endswith(TOKEN_PATH)]


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _api(requester: FakeRequester, clock=None, **kwargs) -> FeishuApi:
    return FeishuApi(
        app_id="cli_example",
        app_secret=SECRET,
        request=requester,
        clock=clock or FakeClock(),
        **kwargs,
    )


class TokenTest(unittest.TestCase):
    def test_token_is_fetched_once_and_reused(self) -> None:
        requester = FakeRequester()
        api = _api(requester)
        for _ in range(3):
            self.assertEqual(api.tenant_token(), "t-cached")
        self.assertEqual(len(requester.token_calls), 1)
        self.assertEqual(api.token_refreshes, 1)

    def test_token_is_refreshed_before_it_expires(self) -> None:
        clock = FakeClock()
        requester = FakeRequester()
        api = _api(requester, clock)
        api.tenant_token()

        # 7200 s reported, 300 s margin => good for 6900 s.
        clock.advance(6899)
        self.assertEqual(api.cached_token, "t-cached")
        clock.advance(2)
        self.assertEqual(api.cached_token, "")
        api.tenant_token()
        self.assertEqual(len(requester.token_calls), 2)

    def test_missing_credentials_are_reported_without_a_call(self) -> None:
        requester = FakeRequester()
        api = FeishuApi(app_id="", app_secret="", request=requester)
        with self.assertRaises(FeishuApiError):
            api.tenant_token()
        self.assertEqual(requester.calls, [])

    def test_a_refused_credential_is_reported_with_its_code(self) -> None:
        requester = FakeRequester(token={"code": 10003, "msg": "app not found"})
        api = _api(requester)
        with self.assertRaises(FeishuApiError) as caught:
            api.tenant_token()
        self.assertEqual(caught.exception.code, 10003)
        self.assertIn("app not found", str(caught.exception))

    def test_a_missing_token_in_the_body_is_reported(self) -> None:
        requester = FakeRequester(token={"code": 0, "msg": "ok"})
        with self.assertRaises(FeishuApiError):
            _api(requester).tenant_token()

    def test_unparseable_expiry_falls_back_to_the_documented_lifetime(self) -> None:
        clock = FakeClock()
        requester = FakeRequester(
            token={
                "code": 0,
                "tenant_access_token": "t-x",
                "expire": "whenever",
            }
        )
        api = _api(requester, clock)
        api.tenant_token()
        clock.advance(6899)
        self.assertEqual(api.cached_token, "t-x")
        clock.advance(2)
        self.assertEqual(api.cached_token, "")

    def test_a_short_expiry_is_not_trusted_to_be_long(self) -> None:
        """A token with a tiny lifetime must not be cached past its floor."""
        clock = FakeClock()
        requester = FakeRequester(
            token={"code": 0, "tenant_access_token": "t-short", "expire": 10}
        )
        api = _api(requester, clock)
        api.tenant_token()
        clock.advance(59)
        self.assertEqual(api.cached_token, "t-short")
        clock.advance(2)
        self.assertEqual(api.cached_token, "")

    def test_the_secret_never_appears_in_an_error_message(self) -> None:
        requester = FakeRequester(
            token={"code": 10003, "msg": "app_id or app_secret invalid"}
        )
        api = _api(requester)
        with self.assertRaises(FeishuApiError) as caught:
            api.tenant_token()
        self.assertNotIn(SECRET, str(caught.exception))

    def test_the_secret_is_only_sent_to_the_token_endpoint(self) -> None:
        requester = FakeRequester()
        api = _api(requester)
        api.send_action(
            FeishuAction(
                name=ACTION_CREATE_MESSAGE,
                params={
                    "receive_id_type": "open_id",
                    "receive_id": "ou_x",
                    "msg_type": "text",
                    "content": '{"text":"hi"}',
                },
            )
        )
        for call in requester.api_calls:
            self.assertNotIn(SECRET, json.dumps(call, ensure_ascii=False))
        token_call = requester.token_calls[0]
        self.assertEqual(token_call["json"]["app_secret"], SECRET)


class SendActionTest(unittest.TestCase):
    def _sent(self, action: FeishuAction, **kwargs) -> tuple[FeishuApi, FakeRequester, dict]:
        requester = FakeRequester()
        api = _api(requester, **kwargs)
        api.send_action(action)
        return api, requester, requester.api_calls[0]

    def test_create_message_targets_a_person_by_open_id(self) -> None:
        _, _, call = self._sent(
            FeishuAction(
                name=ACTION_CREATE_MESSAGE,
                params={
                    "receive_id_type": "open_id",
                    "receive_id": "ou_alice",
                    "msg_type": "text",
                    "content": '{"text":"你好"}',
                },
            )
        )
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{FEISHU_BASE_URL}/im/v1/messages")
        self.assertEqual(call["params"], {"receive_id_type": "open_id"})
        self.assertEqual(call["json"]["receive_id"], "ou_alice")
        self.assertEqual(call["json"]["content"], '{"text":"你好"}')

    def test_create_message_targets_a_group_by_chat_id(self) -> None:
        _, _, call = self._sent(
            FeishuAction(
                name=ACTION_CREATE_MESSAGE,
                params={
                    "receive_id_type": "chat_id",
                    "receive_id": "oc_group",
                    "msg_type": "text",
                    "content": '{"text":"hi"}',
                },
            )
        )
        self.assertEqual(call["params"], {"receive_id_type": "chat_id"})
        self.assertEqual(call["json"]["receive_id"], "oc_group")

    def test_reply_targets_the_message(self) -> None:
        _, _, call = self._sent(
            FeishuAction(
                name=ACTION_REPLY_MESSAGE,
                params={"message_id": "om_1", "msg_type": "text", "content": '{"text":"hi"}'},
            )
        )
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{FEISHU_BASE_URL}/im/v1/messages/om_1/reply")
        self.assertNotIn("receive_id", call["json"])

    def test_get_message_is_a_get(self) -> None:
        _, _, call = self._sent(
            FeishuAction(name=ACTION_GET_MESSAGE, params={"message_id": "om_1"})
        )
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], f"{FEISHU_BASE_URL}/im/v1/messages/om_1")

    def test_bot_info_is_a_get(self) -> None:
        _, _, call = self._sent(FeishuAction(name=ACTION_BOT_INFO, params={}))
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], f"{FEISHU_BASE_URL}/bot/v3/info")

    def test_the_authorization_header_carries_the_token(self) -> None:
        _, _, call = self._sent(
            FeishuAction(name=ACTION_BOT_INFO, params={})
        )
        self.assertEqual(call["headers"]["Authorization"], "Bearer t-cached")

    def test_a_message_id_is_url_encoded(self) -> None:
        _, _, call = self._sent(
            FeishuAction(name=ACTION_GET_MESSAGE, params={"message_id": "om_x/../evil"})
        )
        self.assertIn("om_x%2F..%2Fevil", call["url"])
        self.assertNotIn("/../", call["url"])

    def test_unknown_action_is_reported(self) -> None:
        requester = FakeRequester()
        with self.assertRaises(FeishuApiError):
            _api(requester).send_action(FeishuAction(name="im.v9.nonsense", params={}))
        self.assertEqual(requester.api_calls, [])

    def test_an_unsupported_receive_id_type_is_rejected_before_the_call(self) -> None:
        requester = FakeRequester()
        with self.assertRaises(FeishuApiError):
            _api(requester).send_action(
                FeishuAction(
                    name=ACTION_CREATE_MESSAGE,
                    params={
                        "receive_id_type": "employee_no",
                        "receive_id": "x",
                        "msg_type": "text",
                        "content": "{}",
                    },
                )
            )
        self.assertEqual(requester.api_calls, [])

    def test_a_missing_receive_id_is_reported(self) -> None:
        requester = FakeRequester()
        with self.assertRaises(FeishuApiError):
            _api(requester).send_action(
                FeishuAction(
                    name=ACTION_CREATE_MESSAGE,
                    params={
                        "receive_id_type": "open_id",
                        "receive_id": "",
                        "msg_type": "text",
                        "content": "{}",
                    },
                )
            )
        self.assertEqual(requester.api_calls, [])

    def test_a_missing_message_id_is_reported(self) -> None:
        requester = FakeRequester()
        with self.assertRaises(FeishuApiError):
            _api(requester).send_action(
                FeishuAction(name=ACTION_REPLY_MESSAGE, params={"message_id": "  "})
            )
        self.assertEqual(requester.api_calls, [])

    def test_the_configured_timeout_is_used(self) -> None:
        _, _, call = self._sent(FeishuAction(name=ACTION_BOT_INFO, params={}), timeout=3.5)
        self.assertEqual(call["timeout"], 3.5)


class FailureShapeTest(unittest.TestCase):
    def test_a_business_refusal_is_returned_not_raised(self) -> None:
        """The adapter decides what a non-zero ``code`` means, not this layer."""
        requester = FakeRequester().queue(
            FakeResponse({"code": 230020, "msg": "rate limited"})
        )
        body = _api(requester).send_action(FeishuAction(name=ACTION_BOT_INFO, params={}))
        self.assertEqual(body["code"], 230020)
        self.assertEqual(body["msg"], "rate limited")

    def test_http_failure_raises_with_a_status(self) -> None:
        requester = FakeRequester().queue(
            FakeResponse({"code": 0, "msg": "ok"}, status=503)
        )
        with self.assertRaises(FeishuApiError) as caught:
            _api(requester).send_action(FeishuAction(name=ACTION_BOT_INFO, params={}))
        self.assertEqual(caught.exception.status, 503)

    def test_a_raised_status_is_classified_by_the_send_log(self) -> None:
        requester = FakeRequester().queue(FakeResponse({}, status=503))
        try:
            _api(requester).send_action(FeishuAction(name=ACTION_BOT_INFO, params={}))
        except FeishuApiError as error:
            self.assertEqual(classify_send_error(error), ERROR_HTTP_FORMAT.format(status=503))
        else:  # pragma: no cover - the call must have raised
            self.fail("an HTTP failure must raise")

    def test_non_json_response_raises(self) -> None:
        requester = FakeRequester().queue(FakeResponse(None, status=502, json_error=True))
        with self.assertRaises(FeishuApiError):
            _api(requester).send_action(FeishuAction(name=ACTION_BOT_INFO, params={}))

    def test_non_object_json_raises(self) -> None:
        requester = FakeRequester().queue(FakeResponse(["not", "an", "object"]))
        with self.assertRaises(FeishuApiError):
            _api(requester).send_action(FeishuAction(name=ACTION_BOT_INFO, params={}))

    def test_a_transport_error_propagates_unchanged(self) -> None:
        """A dead network is not an API refusal and must not be disguised as one."""
        requester = FakeRequester()

        def explode(method, url, **kwargs):
            if url.endswith(TOKEN_PATH):
                return FakeResponse(
                    {"code": 0, "tenant_access_token": "t-cached", "expire": 7200}
                )
            raise ConnectionError("link lost")

        api = FeishuApi(
            app_id="cli_example",
            app_secret=SECRET,
            request=explode,
            clock=FakeClock(),
        )
        with self.assertRaises(ConnectionError):
            api.send_action(FeishuAction(name=ACTION_BOT_INFO, params={}))
        self.assertEqual(requester.calls, [])


class BuildTest(unittest.TestCase):
    class _Settings:
        def __init__(self, **kwargs) -> None:
            self.feishu_app_id = kwargs.get("feishu_app_id", "cli_x")
            self.feishu_app_secret = kwargs.get("feishu_app_secret", "s")
            self.feishu_domain = kwargs.get("feishu_domain", "feishu")
            self.feishu_timeout_seconds = kwargs.get("feishu_timeout_seconds", 10.0)

    def test_feishu_domain_uses_the_feishu_host(self) -> None:
        api = build_feishu_api(self._Settings())
        self.assertEqual(api.base_url, FEISHU_BASE_URL)

    def test_lark_domain_uses_the_lark_host(self) -> None:
        api = build_feishu_api(self._Settings(feishu_domain="LARK"))
        self.assertEqual(api.base_url, LARK_BASE_URL)

    def test_a_zero_timeout_falls_back_to_the_default(self) -> None:
        api = build_feishu_api(self._Settings(feishu_timeout_seconds=0))
        self.assertEqual(api.timeout, 10.0)


if __name__ == "__main__":
    unittest.main()
