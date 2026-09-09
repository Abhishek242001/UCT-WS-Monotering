"""
Thread-safe pub/sub bridge between StreamWorker (a background thread) and
WebSocket clients (running in the asyncio event loop).

This is the piece that makes "live" analysis actually live: without it,
a client would have to poll /workstations/identity_status repeatedly to
see new results. With it, every event StreamWorker writes to the database
-- for an RTSP camera or an uploaded video file, identically, since both
run through the exact same StreamWorker code path -- is also pushed to
any WebSocket clients subscribed to that stream_id the instant it happens.

The threading subtlety this solves: StreamWorker.run() executes in a
plain background thread, not a coroutine, so it cannot directly `await`
anything. asyncio.Queue.put_nowait() is not thread-safe to call directly
from another thread. The correct, standard fix -- the same pattern used
in the reference WebSocket handler this feature was modeled on -- is to
capture the running event loop in the async context that started the
worker, then use loop.call_soon_threadsafe() to safely hand the message
back to the event loop from any thread.

A second, real race worth naming explicitly: a worker can start
publishing (and even finish) before a WebSocket client has subscribed --
this actually happened in this project's own test suite with a very
short synthetic video and an already-warm model, and would happen for
real with a fast/small upload or a slow client connection. The fix is
that BOTH register() and publish() lazily create the same queue for a
given stream_id, whichever side calls first -- so early messages are
buffered, never silently dropped, regardless of subscribe timing.
"""
import asyncio
import threading

_queues: dict[str, asyncio.Queue] = {}
_lock = threading.Lock()


def _get_or_create_queue(stream_id: str) -> asyncio.Queue:
    with _lock:
        queue = _queues.get(stream_id)
        if queue is None:
            queue = asyncio.Queue()
            _queues[stream_id] = queue
        return queue


def register(stream_id: str) -> asyncio.Queue:
    """Call from the async context (e.g. the WebSocket handler) to start
    receiving events for a stream_id. Safe to call before OR after the
    worker has started publishing -- either order works correctly."""
    return _get_or_create_queue(stream_id)


def unregister(stream_id: str) -> None:
    with _lock:
        _queues.pop(stream_id, None)


def publish(stream_id: str, message: dict, loop: asyncio.AbstractEventLoop) -> None:
    """Call from ANY thread (this is the whole point) to push a message to
    whoever is subscribed to stream_id -- or to a buffer waiting for a
    future subscriber, since this creates the queue if it doesn't exist
    yet rather than dropping the message when nobody has registered."""
    queue = _get_or_create_queue(stream_id)
    try:
        loop.call_soon_threadsafe(queue.put_nowait, message)
    except RuntimeError:
        pass  # event loop already closed (e.g. server shutting down) -- safe to drop
