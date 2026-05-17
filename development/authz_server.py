"""Dummy authz server. Allows everything, logs every request.

Run: python development/authz_server.py
Listens on 127.0.0.1:9200, POST /check.
"""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s authz %(message)s")
log = logging.getLogger("authz")

app = FastAPI()


@app.post("/check")
async def check(req: Request) -> dict[str, bool]:
    body = await req.json()
    log.info(
        "ALLOW sub=%s jti=%s resource=%s action=%s",
        body.get("sub"),
        body.get("jti"),
        body.get("resource"),
        body.get("action"),
    )
    return {"allowed": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9200, log_level="warning")
