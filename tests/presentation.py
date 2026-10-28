"""Explicit successful presentation for tests calling a plugin without a channel."""
from services.pending_reply import commit_delivered_reply


async def present_reply(awaitable):
    reply = await awaitable
    commit_delivered_reply(reply, reply)
    return reply
