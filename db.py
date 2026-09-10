import time
import os
import libsql_client

_client = None


def get_client():
    """Lazily create the Turso client using env vars.

    Some hosts (bot-hosting.net included) block or mangle raw outbound
    WebSocket connections, which is what the default `libsql://` scheme
    uses. We force the HTTP-based hrana transport instead by rewriting
    the scheme to `https://` — it works over plain HTTPS and is far more
    firewall-friendly, at the cost of (negligible, for this bot) slightly
    higher per-request latency vs. a persistent websocket.
    """
    global _client
    if _client is None:
        url = os.environ.get("TURSO_DATABASE_URL")
        token = os.environ.get("TURSO_AUTH_TOKEN")
        if not url:
            raise RuntimeError(
                "TURSO_DATABASE_URL is not set. Add it as an environment "
                "variable on bot-hosting.net."
            )

        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        elif url.startswith("ws://"):
            url = "http://" + url[len("ws://"):]
        elif url.startswith("wss://"):
            url = "https://" + url[len("wss://"):]

        _client = libsql_client.create_client(url=url, auth_token=token)
    return _client


def today_str() -> str:
    """Current UTC calendar date as YYYY-MM-DD — the day boundary used for
    both vanity's 'today' total and quest assignment/claiming."""
    return time.strftime("%Y-%m-%d", time.gmtime())


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
            user_id        TEXT PRIMARY KEY,
            active         INTEGER NOT NULL DEFAULT 0,
            session_start  REAL,
            total_seconds  REAL NOT NULL DEFAULT 0,
            cycle_seconds  REAL NOT NULL DEFAULT 0
        )
        """
    )
    await client.execute(
        """
        CREATE TABLE IF NOT EXISTS quest_data (
            user_id        TEXT PRIMARY KEY,
            quest_id       TEXT NOT NULL,
            baseline       INTEGER NOT NULL DEFAULT 0,
            assigned_date  TEXT NOT NULL,
            completed      INTEGER NOT NULL DEFAULT 0,
            claimed        INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await client.execute(
        """
        CREATE TABLE IF NOT EXISTS quest_counters (
            user_id   TEXT PRIMARY KEY,
            messages  INTEGER NOT NULL DEFAULT 0,
            bumps     INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await client.execute(
        """
        CREATE TABLE IF NOT EXISTS box_drops (
            user_id    TEXT PRIMARY KEY,
            last_drop  REAL NOT NULL DEFAULT 0
        )
        """
    )

    # Migrations: vanity_data existed before these columns were added.
    # CREATE TABLE IF NOT EXISTS above is a no-op on an existing table, so
    # older deployments are still missing them — add if needed. SQLite/libSQL
    # has no "ADD COLUMN IF NOT EXISTS", so we just try and swallow the
    # "duplicate column" error on databases that already have it.
    for ddl in (
        "ALTER TABLE vanity_data ADD COLUMN cycle_seconds REAL NOT NULL DEFAULT 0",
        "ALTER TABLE vanity_data ADD COLUMN day_date TEXT",
        "ALTER TABLE vanity_data ADD COLUMN day_seconds REAL NOT NULL DEFAULT 0",
    ):
        try:
            await client.execute(ddl)
            print(f"[db] Migrated vanity_data: {ddl}")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"[db] migration check failed ({ddl}): {e}")


# ---------------------------------------------------------------------------
# Config (vanity text, role id, log channel id, guild id, quest log channel)
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
        "SELECT active, session_start, total_seconds, cycle_seconds, day_date, day_seconds "
        "FROM vanity_data WHERE user_id = ?",
        [str(user_id)],
    )
    if rs.rows:
        row = rs.rows[0]
        return {
            "active": bool(row[0]),
            "session_start": row[1],
            "total_seconds": row[2] or 0,
            "cycle_seconds": row[3] or 0,
            "day_date": row[4],
            "day_seconds": row[5] or 0,
        }
    return {
        "active": False,
        "session_start": None,
        "total_seconds": 0,
        "cycle_seconds": 0,
        "day_date": today_str(),
        "day_seconds": 0,
    }


async def upsert_user(
    user_id: int,
    active: bool,
    session_start,
    total_seconds: float,
    cycle_seconds: float,
    day_date: str,
    day_seconds: float,
) -> None:
    client = get_client()
    await client.execute(
        "INSERT INTO vanity_data (user_id, active, session_start, total_seconds, cycle_seconds, day_date, day_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "active = excluded.active, "
        "session_start = excluded.session_start, "
        "total_seconds = excluded.total_seconds, "
        "cycle_seconds = excluded.cycle_seconds, "
        "day_date = excluded.day_date, "
        "day_seconds = excluded.day_seconds",
        [str(user_id), int(active), session_start, total_seconds, cycle_seconds, day_date, day_seconds],
    )


async def get_active_users() -> list[dict]:
    """All users currently tracked as active (vanity present). Used by the
    periodic cycle-progress check so rewards (and today's vanity total)
    stay fresh without waiting for someone to remove their vanity."""
    client = get_client()
    rs = await client.execute(
        "SELECT user_id, session_start, total_seconds, cycle_seconds, day_date, day_seconds "
        "FROM vanity_data WHERE active = 1"
    )
    return [
        {
            "user_id": int(row[0]),
            "session_start": row[1],
            "total_seconds": row[2] or 0,
            "cycle_seconds": row[3] or 0,
            "day_date": row[4],
            "day_seconds": row[5] or 0,
        }
        for row in rs.rows
    ]


async def reset_all_user_data() -> int:
    """Wipe every row from vanity_data (active status, session timers,
    lifetime totals, cycle progress, today's total) for ALL users. Config
    (vanity text, role, log channel, guild lock) is untouched — a separate
    table. Returns the number of rows deleted."""
    client = get_client()
    rs = await client.execute("SELECT COUNT(*) FROM vanity_data")
    count = rs.rows[0][0] if rs.rows else 0
    await client.execute("DELETE FROM vanity_data")
    return count


# ---------------------------------------------------------------------------
# Quest progress counters (lifetime, monotonically increasing — quest
# progress is measured as counter - baseline, baseline snapshotted when a
# quest is assigned)
# ---------------------------------------------------------------------------

async def get_quest_counters(user_id: int) -> dict:
    client = get_client()
    rs = await client.execute(
        "SELECT messages, bumps FROM quest_counters WHERE user_id = ?",
        [str(user_id)],
    )
    if rs.rows:
        row = rs.rows[0]
        return {"messages": row[0] or 0, "bumps": row[1] or 0}
    return {"messages": 0, "bumps": 0}


async def increment_quest_message_count(user_id: int) -> None:
    client = get_client()
    await client.execute(
        "INSERT INTO quest_counters (user_id, messages, bumps) VALUES (?, 1, 0) "
        "ON CONFLICT(user_id) DO UPDATE SET messages = messages + 1",
        [str(user_id)],
    )


async def increment_quest_bump_count(user_id: int) -> None:
    client = get_client()
    await client.execute(
        "INSERT INTO quest_counters (user_id, messages, bumps) VALUES (?, 0, 1) "
        "ON CONFLICT(user_id) DO UPDATE SET bumps = bumps + 1",
        [str(user_id)],
    )


# ---------------------------------------------------------------------------
# Per-user daily quest (assigned, tracked, completed, then claimed)
# ---------------------------------------------------------------------------

async def get_quest(user_id: int):
    client = get_client()
    rs = await client.execute(
        "SELECT quest_id, baseline, assigned_date, completed, claimed "
        "FROM quest_data WHERE user_id = ?",
        [str(user_id)],
    )
    if not rs.rows:
        return None
    row = rs.rows[0]
    return {
        "quest_id": row[0],
        "baseline": row[1] or 0,
        "assigned_date": row[2],
        "completed": bool(row[3]),
        "claimed": bool(row[4]),
    }


async def assign_quest(user_id: int, quest_id: str, baseline: int, assigned_date: str) -> dict:
    client = get_client()
    await client.execute(
        "INSERT INTO quest_data (user_id, quest_id, baseline, assigned_date, completed, claimed) "
        "VALUES (?, ?, ?, ?, 0, 0) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "quest_id = excluded.quest_id, "
        "baseline = excluded.baseline, "
        "assigned_date = excluded.assigned_date, "
        "completed = 0, "
        "claimed = 0",
        [str(user_id), quest_id, baseline, assigned_date],
    )
    return {
        "quest_id": quest_id,
        "baseline": baseline,
        "assigned_date": assigned_date,
        "completed": False,
        "claimed": False,
    }


async def mark_quest_completed(user_id: int) -> None:
    client = get_client()
    await client.execute(
        "UPDATE quest_data SET completed = 1 WHERE user_id = ?",
        [str(user_id)],
    )


async def mark_quest_claimed(user_id: int) -> None:
    client = get_client()
    await client.execute(
        "UPDATE quest_data SET claimed = 1 WHERE user_id = ?",
        [str(user_id)],
    )


# ---------------------------------------------------------------------------
# Random box drops (chat-triggered, cooldown-gated)
# ---------------------------------------------------------------------------

async def get_last_box_drop(user_id: int) -> float:
    client = get_client()
    rs = await client.execute(
        "SELECT last_drop FROM box_drops WHERE user_id = ?",
        [str(user_id)],
    )
    if rs.rows:
        return rs.rows[0][0] or 0
    return 0


async def set_last_box_drop(user_id: int, timestamp: float) -> None:
    client = get_client()
    await client.execute(
        "INSERT INTO box_drops (user_id, last_drop) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET last_drop = excluded.last_drop",
        [str(user_id), timestamp],
    )


async def close():
    global _client
    if _client is not None:
        await _client.close()
        _client = None
