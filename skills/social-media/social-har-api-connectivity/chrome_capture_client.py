#!/usr/bin/env python3
"""Chrome CDP HAR capture — drive Chrome to login, capture session tokens.

Usage:
  python3 chrome_capture_client.py --url https://example.com/login

Opens Chrome in visible mode, captures all network traffic, extracts
session cookies and API endpoints.
"""

import argparse
import asyncio
import json
import os
import ssl
from pathlib import Path

try:
    import websockets
except ImportError:
    websockets = None  # optional dep

HAR_TEMPLATE = {
    "log": {
        "version": "1.2",
        "creator": {"name": "chrome-capture-client", "version": "1.0.0"},
        "entries": [],
    }
}


async def capture_har(cdp_url: str, output: str, timeout: int = 120) -> dict:
    """Connect to Chrome DevTools, capture network, write HAR."""
    har = dict(HAR_TEMPLATE)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    async with websockets.connect(cdp_url, ssl=ctx) as ws:
        # Enable network tracking
        await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        await ws.recv()  # result

        entries = []

        async def listener():
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("method") == "Network.requestWillBeSent":
                    req = msg["params"]["request"]
                    entries.append({
                        "request": {
                            "url": req.get("url"),
                            "method": req.get("method"),
                            "headers": dict(req.get("headers", {})),
                        }
                    })
                elif msg.get("method") == "Network.responseReceived":
                    resp = msg["params"]["response"]
                    if entries:
                        entries[-1]["response"] = {
                            "status": resp.get("status"),
                            "headers": dict(resp.get("headers", {})),
                        }

        await asyncio.wait_for(listener(), timeout=timeout)

    har["log"]["entries"] = entries
    Path(output).write_text(json.dumps(har, indent=2))
    print(f"HAR saved to {output} ({len(entries)} requests)")
    return har


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chrome CDP HAR capture")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222",
                        help="Chrome DevTools URL")
    parser.add_argument("--output", "-o", default="capture.har",
                        help="Output HAR file")
    parser.add_argument("--url", help="URL to navigate to first")
    args = parser.parse_args()

    asyncio.run(capture_har(args.cdp, args.output))
