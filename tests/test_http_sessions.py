from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from services.http_sessions import ThreadLocalSessions
from services.feishu_api import FeishuApi
from tests.test_feishu_api import FakeRequester


class HttpSessionsTest(unittest.TestCase):
    def test_connections_are_reused_per_thread_and_not_shared_between_threads(self):
        sessions = ThreadLocalSessions()
        barrier = threading.Barrier(2)

        def get():
            first = sessions.get()
            barrier.wait(timeout=2)
            self.assertIs(first, sessions.get())
            return first

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                a, b = executor.submit(get), executor.submit(get)
                self.assertIsNot(a.result(), b.result())
        finally:
            sessions.reset()

    def test_concurrent_feishu_calls_refresh_the_token_only_once(self):
        barrier = threading.Barrier(4)
        requester = FakeRequester()
        api = FeishuApi(app_id="synthetic", app_secret="synthetic", request=requester)

        def token():
            barrier.wait(timeout=2)
            return api.tenant_token()

        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(list(executor.map(lambda _: token(), range(4))), ["t-cached"] * 4)
        self.assertEqual(api.token_refreshes, 1)
        self.assertEqual(len(requester.calls), 1)
