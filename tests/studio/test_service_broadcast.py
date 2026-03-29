from __future__ import annotations

import asyncio

from src.studio.service import StudioService


def test_broadcast_drops_message_when_subscriber_queue_is_full_without_loop_error() -> None:
    studio = StudioService()
    loop = asyncio.new_event_loop()
    errors: list[dict[str, object]] = []

    try:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
        loop.run_until_complete(queue.put({"type": "existing"}))
        loop.set_exception_handler(lambda _loop, context: errors.append(context))

        with studio._sub_lock:
            studio._subscribers.append((loop, queue))

        studio._broadcast({"type": "event_append", "payload": {"id": 1}})
        loop.run_until_complete(asyncio.sleep(0))

        assert queue.qsize() == 1
        assert errors == []
    finally:
        loop.close()
