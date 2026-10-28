"""The HTTP seam: one reused session, an explicit timeout, honest failures."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from services.amap_http import (
    DEFAULT_TIMEOUT_SECONDS,
    AmapError,
    make_http_get,
    reset_shared_session,
    shared_session,
)


class SharedSessionTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_shared_session()

    def test_the_session_is_created_once(self) -> None:
        self.assertIs(shared_session(), shared_session())

    def test_reset_closes_and_replaces_the_session(self) -> None:
        session = shared_session()
        with patch.object(session, "close") as close:
            reset_shared_session()

        close.assert_called_once()
        self.assertIsNot(shared_session(), session)

    def test_reset_is_safe_without_a_session(self) -> None:
        reset_shared_session()

        reset_shared_session()

    def test_the_timeout_leaves_room_for_a_fresh_handshake(self) -> None:
        # 实测本机新建连接 3.6-5.1 秒，5 秒超时会偶发读超时；复用连接约 0.4 秒。
        self.assertGreaterEqual(DEFAULT_TIMEOUT_SECONDS, 10.0)


class MakeHttpGetTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_shared_session()

    def test_the_default_getter_goes_through_the_shared_session(self) -> None:
        session = Mock()
        session.get.return_value = SimpleNamespace(
            status_code=200, json=lambda: {"status": "1"}
        )

        with patch("services.amap_http.shared_session", return_value=session):
            payload = make_http_get()("https://example.invalid/place/around")

        self.assertEqual(payload, {"status": "1"})
        self.assertEqual(session.get.call_args.kwargs["timeout"], DEFAULT_TIMEOUT_SECONDS)

    def test_the_session_is_resolved_at_call_time_not_at_construction(self) -> None:
        """A lookup built earlier must follow a later session reset."""
        http_get = make_http_get()
        session = Mock()
        session.get.return_value = SimpleNamespace(
            status_code=200, json=lambda: {"status": "1"}
        )

        with patch("services.amap_http.shared_session", return_value=session) as factory:
            http_get("https://example.invalid/geocode/geo")

        factory.assert_called_once()

    def test_an_injected_getter_wins_and_needs_no_session(self) -> None:
        requester = Mock(
            return_value=SimpleNamespace(status_code=200, json=lambda: {"status": "1"})
        )

        with patch(
            "services.amap_http.shared_session", side_effect=AssertionError("不该建连接")
        ):
            payload = make_http_get(get=requester)("https://example.invalid")

        self.assertEqual(payload, {"status": "1"})
        requester.assert_called_once()

    def test_a_non_200_response_raises_amap_error(self) -> None:
        session = Mock()
        session.get.return_value = SimpleNamespace(status_code=500, json=lambda: {})

        with patch("services.amap_http.shared_session", return_value=session):
            with self.assertRaises(AmapError):
                make_http_get()("https://example.invalid")

    def test_a_non_json_response_raises_amap_error(self) -> None:
        def explode():
            raise ValueError("not json")

        session = Mock()
        session.get.return_value = SimpleNamespace(status_code=200, json=explode)

        with patch("services.amap_http.shared_session", return_value=session):
            with self.assertRaises(AmapError):
                make_http_get()("https://example.invalid")

    def test_a_connection_error_propagates_for_the_caller_to_degrade(self) -> None:
        session = Mock()
        session.get.side_effect = requests.ConnectionError("down")

        with patch("services.amap_http.shared_session", return_value=session):
            with self.assertRaises(requests.ConnectionError):
                make_http_get()("https://example.invalid")


if __name__ == "__main__":
    unittest.main()