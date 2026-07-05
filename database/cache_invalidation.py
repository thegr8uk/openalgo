# database/cache_invalidation.py
"""
ZeroMQ-based cache invalidation for cross-component delivery.

When auth tokens are updated or revoked from a Flask request handler,
an invalidation message is published on ZMQ_CACHE_PORT so the WebSocket
proxy's existing SUB listener picks it up via the `CACHE_INVALIDATE_*`
topic prefix and clears its local auth caches.

Port separation — Docker/standalone mode:
  In Docker/standalone mode (start.sh) the WS server and Flask/gunicorn run
  as SEPARATE OS processes in the same container.  The WS subprocess's broker
  adapters own ZMQ_PORT (default 5555) for broker tick data.  If Flask's cache
  publisher tried to bind the SAME port it would fail (EADDRINUSE) and fall
  back to a different port that the WS server's SUB socket has no knowledge of
  — silently dropping all cache invalidations AND tick data (GitHub issue #XXXX).

  Fix: cache invalidation always publishes on ZMQ_CACHE_PORT (default 5556),
  a dedicated port that never conflicts with the broker-tick publisher.  The WS
  server's SUB socket connects to BOTH ZMQ_PORT and ZMQ_CACHE_PORT at startup.
"""

import json
import os
import threading

import zmq

from utils.logging import get_logger

logger = get_logger(__name__)

# Cache invalidation message types
CACHE_INVALIDATION_PREFIX = "CACHE_INVALIDATE"
AUTH_CACHE_TYPE = "AUTH"
FEED_CACHE_TYPE = "FEED"
ALL_CACHE_TYPE = "ALL"

# Module-level dedicated publisher for cache invalidation.
# Separate from SharedZmqPublisher (broker tick data on ZMQ_PORT) so that in
# Docker/standalone mode both the Flask process and the WS subprocess can bind
# their respective ZMQ PUB sockets without port collision.
_cache_pub_context: zmq.Context | None = None
_cache_pub_socket: zmq.Socket | None = None
_cache_pub_lock = threading.Lock()


def _get_cache_pub_socket() -> zmq.Socket:
    """Return the module-level ZMQ PUB socket bound to ZMQ_CACHE_PORT.

    Lazily created and cached; thread-safe via _cache_pub_lock.
    """
    global _cache_pub_context, _cache_pub_socket
    if _cache_pub_socket is not None:
        return _cache_pub_socket
    with _cache_pub_lock:
        if _cache_pub_socket is not None:
            return _cache_pub_socket
        host = os.getenv("ZMQ_HOST", "127.0.0.1")
        port = int(os.getenv("ZMQ_CACHE_PORT", "5556"))
        _cache_pub_context = zmq.Context()
        sock = _cache_pub_context.socket(zmq.PUB)
        sock.setsockopt(zmq.LINGER, 1000)
        sock.setsockopt(zmq.SNDHWM, 100)
        sock.bind(f"tcp://{host}:{port}")
        logger.info(f"Cache invalidation publisher bound to {host}:{port}")
        _cache_pub_socket = sock
        return sock


# Singleton publisher instance (kept for backward-compat imports)
_publisher_instance = None
_publisher_lock = threading.Lock()


class CacheInvalidationPublisher:
    """Publishes cache-invalidation events on the dedicated ZMQ_CACHE_PORT.

    Uses its own ZMQ PUB socket (not SharedZmqPublisher) so Flask and the
    WS subprocess can co-exist in Docker/standalone mode without port fights.
    """

    def publish_invalidation(self, user_id: str, cache_type: str = ALL_CACHE_TYPE) -> bool:
        """Publish a cache invalidation message for a specific user.

        Args:
            user_id: The user whose cache should be invalidated
            cache_type: Type of cache to invalidate (AUTH, FEED, or ALL)
        """
        if not user_id:
            logger.warning("Cache invalidation skipped — no user_id supplied")
            return False

        try:
            socket = _get_cache_pub_socket()
            topic = f"{CACHE_INVALIDATION_PREFIX}_{cache_type}_{user_id}"
            message = {
                "action": "invalidate",
                "user_id": user_id,
                "cache_type": cache_type,
            }
            socket.send_multipart(
                [topic.encode("utf-8"), json.dumps(message).encode("utf-8")]
            )
            logger.info(f"Published cache invalidation for user: {user_id}, type: {cache_type}")
            return True

        except Exception as e:
            logger.exception(f"Failed to publish cache invalidation for user {user_id}: {e}")
            return False


    def close(self) -> None:
        """No-op kept for backward compatibility — this class no longer
        owns the ZMQ socket. The shared publisher is cleaned up by
        `ConnectionPool.disconnect` / `SharedZmqPublisher.cleanup`.
        """
        return None


def get_cache_invalidation_publisher() -> CacheInvalidationPublisher:
    """Return the singleton cache invalidation publisher."""
    global _publisher_instance

    if _publisher_instance is None:
        with _publisher_lock:
            if _publisher_instance is None:
                _publisher_instance = CacheInvalidationPublisher()

    return _publisher_instance


def publish_auth_cache_invalidation(user_id: str) -> bool:
    """Convenience function to publish an AUTH-cache invalidation."""
    return get_cache_invalidation_publisher().publish_invalidation(user_id, AUTH_CACHE_TYPE)


def publish_feed_cache_invalidation(user_id: str) -> bool:
    """Convenience function to publish a FEED-cache invalidation."""
    return get_cache_invalidation_publisher().publish_invalidation(user_id, FEED_CACHE_TYPE)


def publish_all_cache_invalidation(user_id: str) -> bool:
    """Convenience function to publish an ALL-cache invalidation."""
    return get_cache_invalidation_publisher().publish_invalidation(user_id, ALL_CACHE_TYPE)
