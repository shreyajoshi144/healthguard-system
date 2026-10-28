
from __future__ import annotations

import json

_BODY = json.dumps({"detail": "Request body is too large."}).encode()


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        received = 0
        exceeded = False
        started = False

        async def limited_receive():
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    if not started:
                        await send({"type": "http.response.start", "status": 413, "headers": [
                            (b"content-type", b"application/json"), (b"content-length", str(len(_BODY)).encode()), (b"connection", b"close")]})
                        await send({"type": "http.response.body", "body": _BODY})
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal started
            if exceeded:
                return                      # response already sent; discard the application's follow-up error response
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise                       # unrelated failure: let the normal error handling see it
