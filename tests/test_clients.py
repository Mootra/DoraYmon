from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from doraymon.clients import Clients


class Response:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


def completion(text="answer", reason="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": reason}]}


async def test_model_payload_and_truncation_marker(settings):
    http = Mock()
    http.post.return_value = Response(completion("answer", "length"))
    client = Clients(settings, http)
    result = await client.complete([{"role": "user", "content": "question"}])
    assert "回答未完成" in result
    assert http.post.call_args.kwargs["json"]["messages"] == [
        {"role": "user", "content": "question"}
    ]


@pytest.mark.parametrize("payload", [completion(" "), {"choices": []}, {"choices": [{}]}])
async def test_invalid_model_reply_is_failure(settings, payload):
    http = Mock()
    http.post.return_value = Response(payload)
    with pytest.raises((ValidationError, ValueError)):
        await Clients(settings, http).complete([{"role": "user", "content": "question"}])
    assert http.post.call_count == 1


async def test_model_transport_failure_is_not_retried(settings):
    http = Mock()
    http.post.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await Clients(settings, http).complete([{"role": "user", "content": "question"}])
    assert http.post.call_count == 1


async def test_map_preserves_zero_distance_and_keys_cache_by_radius(settings):
    geo = Response({"status": "1", "geocodes": [{"location": "115,28"}]})
    around = Response(
        {
            "status": "1",
            "pois": [
                {"id": "one", "name": "合成店", "address": [], "location": "115,28", "distance": 0},
                {
                    "id": "far",
                    "name": "远店",
                    "address": "测试街",
                    "location": "115,28",
                    "distance": 2000,
                },
            ],
        }
    )
    http = Mock()
    http.get.side_effect = [geo, around, geo, around]
    client = Clients(settings, http)
    found = await client.nearby("合成地点", 1500)
    assert [(p.id, p.distance, p.address) for p in found] == [("one", 0, "")]
    assert await client.nearby("合成地点", 1500) == found
    assert http.get.call_count == 2
    await client.nearby("合成地点", 1000)
    assert http.get.call_count == 4


async def test_ambiguous_geocode_requires_refinement(settings):
    http = Mock()
    http.get.return_value = Response(
        {"status": "1", "geocodes": [{"location": "115,28"}, {"location": "116,29"}]}
    )
    with pytest.raises(ValueError, match="location_ambiguous"):
        await Clients(settings, http).nearby("合成地点", 1500)
    assert http.get.call_count == 1


async def test_map_service_error_does_not_masquerade_as_empty_result(settings):
    http = Mock()
    http.get.return_value = Response({"status": "0", "info": "synthetic failure"})
    client = Clients(settings, http)
    with pytest.raises(ValueError, match="amap_request_failed"):
        await client.nearby("合成地点", 1500)
    assert not client.places
