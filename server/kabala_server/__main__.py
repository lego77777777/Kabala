import os

import uvicorn

from kabala_server.app import create_app

if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        # connection-level limits (slowloris, connection floods); request-level limits are in shield.py
        limit_concurrency=int(os.environ.get("KABALA_MAX_CONNECTIONS", "200")),
        timeout_keep_alive=5,
        h11_max_incomplete_event_size=16 * 1024,
        server_header=False,
    )
