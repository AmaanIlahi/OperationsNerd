"""
Request body size limit, as a plain ASGI middleware.

A request whose Content-Length is over the limit is refused immediately;
one that streams more than the limit without declaring it is cut off with
413 as soon as it crosses the line, before the app sees the rest.
"""

import json

from agentic import config


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = config.max_body_bytes()

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = True
            if too_big:
                return await self._refuse(send, limit)

        seen = 0
        started = False        # the app has begun its own response
        refused = False        # we already answered 413

        async def counting_receive():
            nonlocal seen, refused
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit and not started:
                    # Answer now and tell the app the client went away. Raising
                    # here would not work: frameworks catch body-read errors
                    # and turn them into their own 400/500.
                    refused = True
                    await self._refuse(send, limit)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal started
            if refused:
                return                      # anything the app says after our 413 is dropped
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, counting_receive, guarded_send)

    @staticmethod
    async def _refuse(send, limit: int):
        body = json.dumps({"detail": f"Request body is too large (limit {limit} bytes)"}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
