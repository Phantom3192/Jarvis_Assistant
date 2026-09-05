import os
import libsql_client

_client = None


def get_client():
    """Lazily create the Turso client using env vars."""
    global _client
    if _client is None:
        url = os.environ.get("TURSO_DATABASE_URL")
        token = os.environ.get("TURSO_AUTH_TOKEN")
        if not url:
            raise RuntimeError(
                "TURSO_DATABASE_URL is not set. Add it as an environment "
                "variable on bot-hosting.net."
            )
        _client = libsql_client.create_client(url=url, auth_token=token)
    return _client


async def init_db():
    client = get_client()
    await client.execute(
        """
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    await client.execute(
        """
        CREATE TABLE IF NOT EXISTS vanity_data (
            user_id TEXT PRIMARY KEY,
            active INTEGER NOT NULL DEFAULT 0,
            session_start REAL,
            total_seconds REAL NOT NULL DEFAULT 0
        )
        """
    )


# ---------------------------------------------------------------------------
# Config (vanity text, role id, log channel id, guild id)
# ---------------------------------------------------------------------------

async def get_all_config() -> dict:
    client = get_client()
    rs = await client.execute("SELECT key, value FROM config")
    return {row[0]: row[1] for row in rs.rows}


async def set_config(key: str, value) -> None:
    client = get_client()
    await client.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [key, str(value)],
    )


# ---------------------------------------------------------------------------
# Per-user vanity tracking
# ---------------------------------------------------------------------------

async def get_user(user_id: int) -> dict:
    client = get_client()
    rs = await client.execute(
        "SELECT active, session_start, total_seconds FROM vanity_data WHERE user_id = ?",
        [str(user_id)],
    )
    if rs.rows:
        row = rs.rows[0]
        return {
            "active": bool(row[0]),
            "session_start": row[1],
            "total_seconds": row[2] or 0,
        }
    return {"active": False, "session_start": None, "total_seconds": 0}


async def upsert_user(user_id: int, active: bool, session_start, total_seconds: float) -> None:
    client = get_client()
    await client.execute(
        "INSERT INTO vanity_data (user_id, active, session_start, total_seconds) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "active = excluded.active, "
        "session_start = excluded.session_start, "
        "total_seconds = excluded.total_seconds",
        [str(user_id), int(active), session_start, total_seconds],
    )


async def close():
    global _client
    if _client is not None:
        await _client.close()
        _client = None
