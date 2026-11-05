"""Run the hub and web app against a simulated TV, for development and demos.

  python -m tvremote.devserver            # http://localhost:8080, access key "dev"
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn

from .fake import FakeTV
from .hub import create_app
from .samsung import SamsungTV

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


async def main() -> None:
    fake = await FakeTV().start()
    tv = SamsungTV("127.0.0.1", port=fake.ws_port, info_port=fake.info_port, secure=False, mac=fake.mac)
    key = os.environ.get("TVREMOTE_HUB_KEY", "dev")
    app = create_app(tv=tv, access_key=key, config_path=Path("/nonexistent/tv.json"), web_dir=str(WEB_DIR))
    port = int(os.environ.get("TVREMOTE_PORT", "8080"))
    print(f"Dev hub on http://localhost:{port}  (access key: {key!r}, simulated TV)")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    try:
        await server.serve()
    finally:
        await fake.stop()


if __name__ == "__main__":
    asyncio.run(main())
