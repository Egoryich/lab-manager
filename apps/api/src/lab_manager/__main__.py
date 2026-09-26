import argparse
import asyncio

import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    config = uvicorn.Config(
        "lab_manager.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=arguments.port,
        proxy_headers=False,
        access_log=False,
    )
    # Server.run() chooses Proactor on Windows in recent Uvicorn versions.
    # Use the application event-loop policy required by psycopg instead.
    asyncio.run(uvicorn.Server(config).serve())
