"""Lab Manager control plane."""

import asyncio
import sys

# psycopg async sockets require Selector on Windows (deployment uses Linux).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
