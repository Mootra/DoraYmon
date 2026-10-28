import asyncio

import pytest

from doraymon.admin import AdminRequest, AdminService, parse_command, sender_from_message
from doraymon.admin_http import create_admin_app


def test_original_message_required_for_owner():
    assert sender_from_message({"user_id": "12345", "platform": "qq"}) == ""
    assert (
        sender_from_message({"platform": "qq", "message_info": {"user_info": {"user_id": "12345"}}})
        == "12345"
    )
    assert (
        sender_from_message(
            {"platform": "feishu", "message_info": {"user_info": {"user_id": "12345"}}}
        )
        == ""
    )


@pytest.mark.asyncio
async def test_other_user_cannot_send_or_query_contacts():
    calls = []

    async def call(name, params):
        calls.append((name, params))
        return {"status": "ok", "data": []}

    service = AdminService("12345", call)
    for action in ("private", "friends", "friend_request"):
        with pytest.raises(PermissionError):
            await service.execute("54321", AdminRequest(action=action, target="99999", text="test"))
    assert not calls


@pytest.mark.asyncio
async def test_duplicate_request_sends_once_and_conflicting_id_is_rejected():
    calls = []

    async def call(name, params):
        calls.append((name, params))
        await asyncio.sleep(0)
        return {"status": "ok", "retcode": 0, "data": {"message_id": "123"}}

    service = AdminService("12345", call)
    request = AdminRequest(action="private", target="99999", text="hello", request_id="test-id-1")
    await asyncio.gather(service.execute("12345", request), service.execute("12345", request))
    assert len(calls) == 1
    assert (await service.execute("12345", request))["message_id"] == "123"
    with pytest.raises(ValueError):
        await service.execute("12345", request.model_copy(update={"text": "changed"}))


@pytest.mark.asyncio
async def test_unknown_send_result_is_not_retried():
    calls = []

    async def call(name, params):
        calls.append(name)
        raise TimeoutError

    service = AdminService("12345", call)
    request = AdminRequest(action="private", target="99999", text="hello")
    assert (await service.execute("12345", request))["state"] == "uncertain"
    assert (await service.execute("12345", request))["state"] == "uncertain"
    assert len(calls) == 1
    assert "hello" not in str(service.history())


@pytest.mark.asyncio
async def test_backend_restart_does_not_resend_an_unconfirmed_action(tmp_path):
    calls = []

    async def call(name, params):
        calls.append(name)
        raise TimeoutError

    path = tmp_path / "journal.sqlite3"
    request = AdminRequest(action="private", target="99999", text="synthetic secret message")
    original = AdminService("12345", call, journal_path=path)
    assert (await original.execute("12345", request))["state"] == "uncertain"
    restarted = AdminService("12345", call, journal_path=path)
    assert (await restarted.execute("12345", request))["state"] == "uncertain"
    assert len(calls) == 1
    assert request.text not in str(restarted.history())
    assert request.text.encode() not in path.read_bytes()


@pytest.mark.asyncio
async def test_mentions_are_segments_and_unsupported_friend_request_does_not_call():
    calls = []

    async def call(name, params):
        calls.append((name, params))
        return {"status": "ok", "data": {"message_id": "456"}}

    service = AdminService("12345", call)
    request = parse_command("/群消息 77777 99999 test")
    assert request is not None
    assert (await service.execute("12345", request))["state"] == "confirmed"
    assert calls[0][1]["message"][0] == {"type": "at", "data": {"qq": "99999"}}
    assert (await service.execute("12345", AdminRequest(action="friend_request", target="99999")))[
        "state"
    ] == "unsupported"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_desktop_gateway_rejects_missing_token_origin_and_claimed_owner():
    from aiohttp.test_utils import TestClient, TestServer

    calls = []

    async def call(name, params):
        calls.append((name, params))
        return {"status": "ok", "data": []}

    service = AdminService("12345", call)
    token = "synthetic-token-for-offline-tests-123456"
    app = create_admin_app(service, token, lambda: {"shops": []})
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/health")).status == 401
        headers = {"Authorization": "Bearer " + token}
        assert (await client.get("/health", headers=headers)).status == 200
        assert (
            await client.get("/health", headers={**headers, "Origin": "https://example.com"})
        ).status == 403
        assert (
            await client.post(
                "/actions", headers=headers, json={"action": "friends", "owner": "12345"}
            )
        ).status == 400
        assert not calls
