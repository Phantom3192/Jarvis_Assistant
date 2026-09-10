"""
Shared mutable state across cogs — backed by Turso (LibSQL).
Stores bans, seen users, stats, guild prompts, and rate limits.
All data persists across restarts via Turso.
Bot runs fine in memory-only mode if TURSO_URL/TOKEN are not set.

OPTIMISATIONS vs original:
- _debounced_save: replaced if/elif chain with a lookup dict → O(1) dispatch
- get_ai_usage / increment_ai_usage: merged duplicate reset logic into one helper
- _today_utc: cached at module-level with a 1-second TTL to avoid repeated
  datetime calls on every message (cheap but adds up at scale)
- _BanProxy / _SeenProxy: added missing dunder methods (__repr__, update) so
  they behave more like the built-in types they proxy
- Type annotations tightened throughout (no bare `dict` / `set` on proxies)
- Removed unused `json` import alias (already imported at top)
"""
import os
import time
import asyncio
import json
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any

from cogs.turso_db import TursoConnection
from cogs.quest_hooks import fire_quest_hook

_db: TursoConnection | None = None  # set in init_db()

# ── In-memory mirrors ─────────────────────────────────────────────────────────

_data: dict[str, Any] = {
    "bans":           {},    # str(user_id) → {"reason": str, "expires": float|None}
    "seen":           set(), # set of int user_ids
    "first_interaction": {}, # str(user_id) → epoch float of the FIRST ever interaction with Jarvis (set once, in mark_seen)
    "stats":          {},    # str(user_id) → {"messages", "tokens_est", "first_seen", "last_seen"}
    "prompts":        {},    # str(guild_id) → prompt string
    "rate_limits":    {},    # str(user_id)  → {"count": int, "day": "YYYY-MM-DD"}
    "settings":       {},    # arbitrary bot settings persisted (e.g. cooldowns)
    "preferred_names": {},   # str(user_id) → preferred display name
    "reminders":      {},    # str(user_id) → list of reminder objects
    "playlists":      {},    # str(user_id) -> {playlist_name: [track_info, ...]}
    "playlist_shares": {},   # str(owner_id) -> {playlist_name: {str(target_id): "read"|"write"}}
    "playlist_inbox": {},    # str(target_id) -> [{"owner_id": int, "name": str, "permission": str, "ts": float}, ...]
    "song_history":   {},    # str(user_id) -> [track_info, ...]
    "guild_bans":     {},    # str(guild_id) → {"reason": str, "banned_at": float}
    "credits":        {},    # str(user_id) → int balance of Jarvis Credits (JC)
    "credit_meta":    {},    # str(user_id) → {"last_daily": "YYYY-MM-DD", "chat_day": "YYYY-MM-DD", "chat_count": int, "streak": int, "last_streak_day": "YYYY-MM-DD", "streak_milestones": [int, ...]}
    "guild_logs":     {},    # str(guild_id) → {"name": str, "joined_at": float, "member_count": int, "owner_id": int}
    "referral_codes": {},    # str(user_id) → str code (each user's own stable invite code)
    "referred_by":    {},    # str(user_id) → referrer's user_id (int). Presence = "already redeemed a code".
    "dnd_users":      {},    # str(user_id) → True (presence = DND enabled)
    "game_stats":     {},    # str(user_id) → {"chess_wins","chess_losses","mafia_wins","mafia_losses","hangman_wins"}
    "songs_played":   {},    # str(user_id) → int, lifetime count of songs played (not trimmed like song_history)
    "badges":         {},    # str(user_id) → list[str] of unlocked achievement badge ids
    "titles":         {},    # str(user_id) → {"owned": [title_id,...], "equipped": title_id|None}
    "banners":        {},    # str(user_id) → {"owned": [banner_id,...], "equipped": banner_id|None}
    "title_subscriptions": {}, # str(user_id) → {title_id: next_charge_epoch}
    "title_autorenew": {},  # str(user_id) → {title_id: bool} — missing entry defaults to True
    "profile_privacy": {}, # str(user_id) → list[str] of field keys hidden from OTHER viewers (owner always sees all)
    "tos_accepted":    {}, # str(user_id) → {"version": int, "accepted_at": float}
    "lifetime_earned": {}, # str(user_id) → int, cumulative JC ever gained via add_credits (never decreases)
    "lifetime_spent":  {}, # str(user_id) → int, cumulative JC ever deducted via spend_credits (never decreases)
    # ── System Breach Event Badges (persist after event) ───────────────────
    "system_breach_badges": {},  # str(user_id) → [badge_id, ...]
    "votes": {},  # str(user_id) → {"total_votes": int, "streak": int, "last_vote_ts": float, "streak_milestones": [int,...]}
    "guild_prefixes": {},  # str(guild_id) → custom command prefix string
    "locked_messages": {},  # lock_id (str) → {"author": int, "target": int, "content": str, "opened": bool, "created": float}
    "user_cards":      {},  # str(user_id) → {card_id (str): quantity (int)} — beast card collection inventory
    "card_meta":       {},  # str(user_id) → {"last_daily_card": "YYYY-MM-DD"} — daily card pull cooldown
    "card_quests":     {},  # str(user_id) → {"date": "YYYY-MM-DD", "quest_ids": [str,...], "baselines": {quest_id: int}, "claimed": [quest_id,...]}
    "card_set_claims": {},  # str(user_id) → [set_id (str), ...] — beast card sets already rewarded
    "api_metrics":     {},  # {"inference_count","inference_latest","db_count","db_latest","command_count","command_latest"} — !api dashboard's all-time counters, see cogs/api_metrics.py
    "auto_quests":     {},  # str(user_id) → {"period": "YYYY-MM-DD", "quest_id": str, "baseline": int, "rewarded": bool} — see cogs/auto_quests.py
    "bump_counts":     {},  # str(user_id) → int, lifetime count of successful /bump server bumps detected
    "server_message_counts": {},  # str(user_id) → int, lifetime count of ALL messages sent anywhere in any guild (not just AI chat) — see cogs/auto_quests.py
}

# Serialisers for each key (avoids if/elif chain in _debounced_save)
_SERIALISE: dict[str, Any] = {
    "bans":            lambda: _data["bans"],
    "seen":            lambda: [str(uid) for uid in _data["seen"]],
    "first_interaction": lambda: _data["first_interaction"],
    "stats":           lambda: _data["stats"],
    "prompts":         lambda: _data["prompts"],
    "rate_limits":     lambda: _data["rate_limits"],
    "settings":        lambda: _data["settings"],
    "preferred_names": lambda: _data["preferred_names"],
    "reminders":       lambda: _data["reminders"],
    "playlists":       lambda: _data["playlists"],
    "playlist_shares": lambda: _data["playlist_shares"],
    "playlist_inbox":  lambda: _data["playlist_inbox"],
    "song_history":    lambda: _data["song_history"],
    "guild_bans":      lambda: _data["guild_bans"],
    "credits":         lambda: _data["credits"],
    "credit_meta":     lambda: _data["credit_meta"],
    "guild_logs":      lambda: _data["guild_logs"],
    "referral_codes":  lambda: _data["referral_codes"],
    "referred_by":     lambda: _data["referred_by"],
    "dnd_users":       lambda: _data["dnd_users"],
    "game_stats":      lambda: _data["game_stats"],
    "songs_played":    lambda: _data["songs_played"],
    "badges":          lambda: _data["badges"],
    "titles":          lambda: _data["titles"],
    "banners":         lambda: _data["banners"],
    "title_subscriptions": lambda: _data["title_subscriptions"],
    "title_autorenew":     lambda: _data["title_autorenew"],
    "profile_privacy":     lambda: _data["profile_privacy"],
    "tos_accepted":        lambda: _data["tos_accepted"],
    "lifetime_earned":     lambda: _data["lifetime_earned"],
    "lifetime_spent":      lambda: _data["lifetime_spent"],
    "system_breach_badges": lambda: _data["system_breach_badges"],
    "votes":                lambda: _data["votes"],
    "guild_prefixes":       lambda: _data["guild_prefixes"],
    "locked_messages":      lambda: _data["locked_messages"],
    "user_cards":           lambda: _data["user_cards"],
    "card_meta":            lambda: _data["card_meta"],
    "card_quests":          lambda: _data["card_quests"],
    "card_set_claims":      lambda: _data["card_set_claims"],
    "api_metrics":          lambda: _data["api_metrics"],
    "auto_quests":          lambda: _data["auto_quests"],
    "bump_counts":          lambda: _data["bump_counts"],
    "server_message_counts": lambda: _data["server_message_counts"],
}


# ── DB init ───────────────────────────────────────────────────────────────────



async def init_db():
    """Call once at startup. Connects to Turso and loads all state into memory."""
    global _db

    turso_url   = os.getenv("TURSO_URL",   "").strip().lstrip("=").strip()
    turso_token = os.getenv("TURSO_TOKEN", "").strip().lstrip("=").strip()

    if not turso_url or not turso_token:
        print(
            "⚠️  TURSO_URL or TURSO_TOKEN not set.\n"
            "   Jarvis will run in memory-only mode — all data will be lost on restart.\n"
            "   Add TURSO_URL and TURSO_TOKEN to your .env to persist data."
        )
        return

    async def _ensure_table():
        await _db.conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

    _db = TursoConnection("State", turso_url, turso_token, init_fn=_ensure_table)
    connected = await _db.connect_async()
    if not connected:
        print("❌ Turso state DB connection failed — Jarvis will run in memory-only mode.")
        _db = None
        return

    async def _load_state():
        result = await _db.conn.execute("SELECT key, value FROM state")
        return result.rows

    rows = await _db.run(_load_state, default=[])
    db = {row[0]: json.loads(row[1]) for row in rows}

    if "bans"           in db: _data["bans"]           = db["bans"]
    if "seen"           in db: _data["seen"]           = set(int(uid) for uid in db["seen"])
    if "first_interaction" in db: _data["first_interaction"] = db["first_interaction"]
    if "stats"          in db: _data["stats"]          = db["stats"]
    if "prompts"        in db: _data["prompts"]        = db["prompts"]
    if "settings"       in db: _data["settings"]       = db["settings"]
    if "preferred_names" in db: _data["preferred_names"] = db["preferred_names"]
    if "reminders"      in db: _data["reminders"]      = db["reminders"]
    if "playlists"      in db: _data["playlists"]      = db["playlists"]
    if "playlist_shares" in db: _data["playlist_shares"] = db["playlist_shares"]
    if "playlist_inbox"  in db: _data["playlist_inbox"]  = db["playlist_inbox"]
    if "song_history"   in db: _data["song_history"]   = db["song_history"]
    if "rate_limits"    in db: _data["rate_limits"]    = db["rate_limits"]
    if "guild_bans"     in db: _data["guild_bans"]     = db["guild_bans"]
    if "credits"        in db: _data["credits"]        = db["credits"]
    if "credit_meta"    in db: _data["credit_meta"]    = db["credit_meta"]
    if "guild_logs"     in db: _data["guild_logs"]     = db["guild_logs"]
    if "referral_codes" in db: _data["referral_codes"] = db["referral_codes"]
    if "referred_by"    in db: _data["referred_by"]    = db["referred_by"]
    if "dnd_users"      in db: _data["dnd_users"]       = db["dnd_users"]
    if "game_stats"     in db: _data["game_stats"]      = db["game_stats"]
    if "songs_played"   in db: _data["songs_played"]    = db["songs_played"]
    if "badges"         in db: _data["badges"]          = db["badges"]
    if "titles"         in db: _data["titles"]          = db["titles"]
    if "banners"        in db: _data["banners"]         = db["banners"]
    if "title_subscriptions" in db: _data["title_subscriptions"] = db["title_subscriptions"]
    if "title_autorenew" in db: _data["title_autorenew"] = db["title_autorenew"]
    if "profile_privacy" in db: _data["profile_privacy"] = db["profile_privacy"]
    if "tos_accepted"    in db: _data["tos_accepted"]    = db["tos_accepted"]
    if "lifetime_earned" in db: _data["lifetime_earned"] = db["lifetime_earned"]
    if "lifetime_spent"  in db: _data["lifetime_spent"]  = db["lifetime_spent"]
    if "system_breach_badges" in db: _data["system_breach_badges"] = db["system_breach_badges"]
    if "votes"           in db: _data["votes"]           = db["votes"]
    if "guild_prefixes"  in db: _data["guild_prefixes"]  = db["guild_prefixes"]
    if "locked_messages" in db: _data["locked_messages"] = db["locked_messages"]
    if "user_cards"      in db: _data["user_cards"]      = db["user_cards"]
    if "card_meta"       in db: _data["card_meta"]       = db["card_meta"]
    if "card_quests"     in db: _data["card_quests"]     = db["card_quests"]
    if "card_set_claims" in db: _data["card_set_claims"] = db["card_set_claims"]
    if "api_metrics"     in db: _data["api_metrics"]     = db["api_metrics"]
    if "auto_quests"     in db: _data["auto_quests"]     = db["auto_quests"]
    if "bump_counts"     in db: _data["bump_counts"]     = db["bump_counts"]
    if "server_message_counts" in db: _data["server_message_counts"] = db["server_message_counts"]

    # ── One-time migration: back-fill first_interaction for users who were
    # already marked "seen" before this table existed. mark_seen() only
    # stamps first_interaction for users NOT already in "seen", so anyone
    # who joined pre-migration would otherwise show "Not yet interacted"
    # forever — which is wrong, they've clearly interacted. Best available
    # guess is their AI-chat first_seen timestamp, if they have one.
    backfilled = False
    for uid in _data["seen"]:
        key = str(uid)
        if key not in _data["first_interaction"]:
            legacy_first_seen = _data["stats"].get(key, {}).get("first_seen")
            if legacy_first_seen:
                _data["first_interaction"][key] = legacy_first_seen
                backfilled = True
    if backfilled:
        _schedule_save("first_interaction")

    # ── One-time migration: promote pre-12h-reset card_quests records
    # from the old {"date": "YYYY-MM-DD", ...} format to the current
    # {"period": "YYYY-MM-DD-HH", ...} format. Without this, every
    # in-progress/claimed quest record from before this change looks
    # "expired" the moment it's read (missing "period" never matches
    # quest_period_utc()) and silently gets replaced with a fresh,
    # unclaimed set — so someone who legitimately claimed today's quest
    # would appear able to claim again. If the old record's calendar
    # date is still today, we carry it forward as the *current* period
    # (preserving quest_ids/baselines/claimed exactly); if it's from an
    # earlier day it's genuinely stale and is left alone — it'll be
    # naturally replaced on next read either way, migrated or not.
    quests_migrated = False
    _today = _today_utc()
    _cur_period = quest_period_utc()
    for uid, entry in _data["card_quests"].items():
        if "period" in entry:
            continue  # already new-format
        if entry.get("date") == _today:
            entry["period"] = _cur_period
            quests_migrated = True
    if quests_migrated:
        _schedule_save("card_quests")

    print("✅ Turso state DB connected")
    asyncio.create_task(_db.keepalive_loop())

    # Restore the !api dashboard's all-time counters (see cogs/api_metrics.py)
    # so they don't silently reset to 0 on this restart. Lazy import — avoids
    # a module-load-order dependency, since api_metrics.py has no imports of
    # its own back into state.py.
    try:
        from cogs.api_metrics import load_persisted_snapshot
        load_persisted_snapshot(_data.get("api_metrics", {}))
    except Exception as e:
        print(f"❌ Error restoring api_metrics snapshot: {e}")


# ── Save helpers ──────────────────────────────────────────────────────────────

async def _save_key(key: str, value: Any) -> None:
    """Upsert a single key into the state table. Auto-reconnect, retry,
    and off-event-loop execution are all handled by TursoConnection.run() —
    this can never raise or stall the event loop."""
    if _db is None:
        return

    async def _do_save():
        await _db.conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value))
        )

    await _db.run(_do_save)


# ── Debounced save ────────────────────────────────────────────────────────────

_save_tasks: dict[str, asyncio.Task] = {}

def _schedule_save(key: str) -> None:
    """Debounce saves — waits 2 s after last change before writing."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = _save_tasks.get(key)
    if task and not task.done():
        task.cancel()
    _save_tasks[key] = loop.create_task(_debounced_save(key))

async def _debounced_save(key: str, delay: float = 2.0) -> None:
    await asyncio.sleep(delay)
    serialiser = _SERIALISE.get(key)
    if serialiser:
        try:
            loop = asyncio.get_running_loop()
            if loop.is_closed() or not loop.is_running():
                return
            await _save_key(key, serialiser())
        except Exception as e:
            # Last-resort guard: a debounced save must never crash its Task
            # silently and must never raise into the event loop's task runner.
            print(f"❌ Unexpected error in debounced save ({key}): {e}")


async def flush_all_saves() -> None:
    """Immediately persist every key with a pending (not-yet-fired) debounced
    save, skipping the normal 2s wait. Call this from a SIGTERM/SIGINT
    handler before the process actually exits — otherwise any change made
    in the last ~2s before a restart/redeploy is silently lost, since
    platforms like Railway send SIGTERM and Python doesn't run pending
    asyncio tasks or `finally` blocks for that signal on its own.
    """
    if _db is None:
        return

    pending = [key for key, task in _save_tasks.items() if task and not task.done()]
    for key in pending:
        _save_tasks[key].cancel()

    saved = 0
    for key in pending:
        serialiser = _SERIALISE.get(key)
        if not serialiser:
            continue
        try:
            await _save_key(key, serialiser())
            saved += 1
        except Exception as e:
            print(f"❌ Error flushing '{key}' on shutdown: {e}")

    if saved:
        print(f"✅ Flushed {saved} pending state key(s) before shutdown.")


# ══════════════════════════════════════════════════════════════════════════════
# BAN STATE
# ══════════════════════════════════════════════════════════════════════════════

class _BanProxy(dict):
    """Proxy so existing cog code (bot_bans[x] = y, del bot_bans[x]) still works."""
    def __contains__(self, key):        return key in _data["bans"]
    def __getitem__(self, key):         return _data["bans"][key]
    def __setitem__(self, key, value):
        _data["bans"][key] = value
        _schedule_save("bans")
    def __delitem__(self, key):
        del _data["bans"][key]
        _schedule_save("bans")
    def __iter__(self):                 return iter(_data["bans"])
    def __len__(self):                  return len(_data["bans"])
    def __repr__(self):                 return repr(_data["bans"])
    def get(self, key, default=None):   return _data["bans"].get(key, default)
    def items(self):                    return _data["bans"].items()
    def keys(self):                     return _data["bans"].keys()
    def values(self):                   return _data["bans"].values()
    def update(self, other=(), **kw):
        _data["bans"].update(other, **kw)
        _schedule_save("bans")
    def pop(self, key, *args):
        val = _data["bans"].pop(key, *args)
        _schedule_save("bans")
        return val

bot_bans: dict = _BanProxy()

def save_bans() -> None:
    _schedule_save("bans")

def is_bot_banned(user_id: int) -> bool:
    uid  = str(user_id)
    ban  = _data["bans"].get(uid)
    if not ban:
        return False
    expires = ban.get("expires")
    if expires is not None and time.time() >= expires:
        # Temp ban expired — remove it automatically
        del _data["bans"][uid]
        _schedule_save("bans")
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# SEEN USERS
# ══════════════════════════════════════════════════════════════════════════════

class _SeenProxy(set):
    def __contains__(self, item): return item in _data["seen"]
    def __iter__(self):           return iter(_data["seen"])
    def __len__(self):            return len(_data["seen"])
    def __repr__(self):           return repr(_data["seen"])
    def add(self, item):
        if item not in _data["seen"]:
            _data["seen"].add(item)
            _schedule_save("seen")

seen_users: set = _SeenProxy()

def mark_seen(user_id: int) -> None:
    if user_id not in _data["seen"]:
        _data["seen"].add(user_id)
        _schedule_save("seen")
        # Stamp the FIRST ever interaction here — this is the single call site
        # that fires exactly once per user, regardless of whether their first
        # touch was a chat message, a slash command, or a referral redemption.
        # (record_message()'s "first_seen" is NOT a substitute: it's only set
        # the first time a message goes through the AI chat pipeline, which
        # can happen long after a user's true first interaction.)
        uid = str(user_id)
        if uid not in _data["first_interaction"]:
            _data["first_interaction"][uid] = time.time()
            _schedule_save("first_interaction")

def is_new_user(user_id: int) -> bool:
    return user_id not in _data["seen"]

def get_first_interaction(user_id: int) -> float | None:
    """Epoch timestamp of the user's first-ever interaction with Jarvis, or
    None if unknown (e.g. the user was marked seen before this field existed).
    This is what /profile should use for "First Interaction" / "Account Age" —
    not stats["first_seen"], which only reflects first AI chat message."""
    return _data["first_interaction"].get(str(user_id))


# ══════════════════════════════════════════════════════════════════════════════
# STATS
# ══════════════════════════════════════════════════════════════════════════════

def record_message(user_id: int, user_text: str, reply_text: str) -> None:
    uid    = str(user_id)
    now    = time.time()
    tokens = (len(user_text) + len(reply_text)) // 4
    stats  = _data["stats"]
    if uid not in stats:
        stats[uid] = {
            "messages":   0,
            "tokens_est": 0,
            "first_seen": now,
            "last_seen":  now,
        }
    s = stats[uid]
    s["messages"]   += 1
    s["tokens_est"] += tokens
    s["last_seen"]   = now
    _schedule_save("stats")

def get_stats(user_id: int) -> dict | None:
    return _data["stats"].get(str(user_id))

def record_image_search(user_id: int) -> None:
    """Bump a user's cumulative !image usage count. Reuses the same 'stats'
    table/persistence as record_message() rather than adding a new table."""
    uid   = str(user_id)
    now   = time.time()
    stats = _data["stats"]
    if uid not in stats:
        stats[uid] = {
            "messages":   0,
            "tokens_est": 0,
            "first_seen": now,
            "last_seen":  now,
        }
    stats[uid]["image_searches"] = stats[uid].get("image_searches", 0) + 1
    _schedule_save("stats")

def get_image_search_count(user_id: int) -> int:
    data = _data["stats"].get(str(user_id))
    return data.get("image_searches", 0) if data else 0

def record_mystery_box_open(user_id: int, *, deluxe: bool = False) -> None:
    """Bump a user's cumulative Mystery Box open count. Tracks regular and
    Deluxe boxes as separate counters (deluxe=True bumps the deluxe one)
    so each can back its own card quest. Reuses the same 'stats' table/
    persistence as record_message() rather than adding a new table."""
    uid   = str(user_id)
    now   = time.time()
    stats = _data["stats"]
    if uid not in stats:
        stats[uid] = {
            "messages":   0,
            "tokens_est": 0,
            "first_seen": now,
            "last_seen":  now,
        }
    key = "mystery_boxes_deluxe_opened" if deluxe else "mystery_boxes_opened"
    stats[uid][key] = stats[uid].get(key, 0) + 1
    _schedule_save("stats")

def get_mystery_box_open_count(user_id: int) -> int:
    data = _data["stats"].get(str(user_id))
    return data.get("mystery_boxes_opened", 0) if data else 0

def get_deluxe_mystery_box_open_count(user_id: int) -> int:
    data = _data["stats"].get(str(user_id))
    return data.get("mystery_boxes_deluxe_opened", 0) if data else 0

def get_all_stats() -> dict[str, dict]:
    """Return a copy of all user stats, keyed by str(user_id)."""
    return dict(_data["stats"])

def get_all_bans() -> dict[str, dict]:
    """Return a copy of all active bans, keyed by str(user_id)."""
    return dict(_data["bans"])

def get_all_rate_limits() -> dict[str, dict]:
    """Return a copy of all rate limit entries, keyed by str(user_id)."""
    return dict(_data["rate_limits"])


# ══════════════════════════════════════════════════════════════════════════════
# GUILD PROMPTS
# ══════════════════════════════════════════════════════════════════════════════

def get_guild_prompt(guild_id: int | None) -> str | None:
    if guild_id is None:
        return None
    return _data["prompts"].get(str(guild_id))

def set_guild_prompt(guild_id: int, prompt: str) -> None:
    _data["prompts"][str(guild_id)] = prompt
    _schedule_save("prompts")

def reset_guild_prompt(guild_id: int) -> bool:
    uid = str(guild_id)
    if uid in _data["prompts"]:
        del _data["prompts"][uid]
        _schedule_save("prompts")
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# GUILD COMMAND PREFIX
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_PREFIX = "!"

def get_guild_prefix(guild_id: int | None) -> str:
    """Return this guild's custom prefix, or the default '!' if unset/DM."""
    if guild_id is None:
        return DEFAULT_PREFIX
    return _data["guild_prefixes"].get(str(guild_id), DEFAULT_PREFIX)

def set_guild_prefix(guild_id: int, prefix: str) -> None:
    _data["guild_prefixes"][str(guild_id)] = prefix
    _schedule_save("guild_prefixes")

def reset_guild_prefix(guild_id: int) -> bool:
    uid = str(guild_id)
    if uid in _data["guild_prefixes"]:
        del _data["guild_prefixes"][uid]
        _schedule_save("guild_prefixes")
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# AI RATE LIMITING
# ══════════════════════════════════════════════════════════════════════════════

DAILY_AI_LIMIT = 100
WARN_AT        = 80

# Simple 1-second cache for today's UTC date string — avoids repeated datetime
# formatting on every single AI message.
_today_cache: tuple[float, str] = (0.0, "")

def _today_utc() -> str:
    global _today_cache
    ts = time.time()
    if ts - _today_cache[0] > 1.0:
        _today_cache = (ts, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    return _today_cache[1]


def today_utc() -> str:
    """Public wrapper around _today_utc() for cogs that need to seed
    date-scoped logic (e.g. deterministic daily quest selection)."""
    return _today_utc()


# 12-hour quest period, aligned to 00:00 and 12:00 UTC. Kept separate from
# _today_utc()/today_utc() above since those still back once-a-day systems
# (AI message rate limit, the free daily card pull) — only card quests
# reset twice a day.
_QUEST_PERIOD_HOURS = 12

def quest_period_utc() -> str:
    """Identifier for the current quest period, e.g. '2026-08-26-00' for
    00:00–12:00 UTC and '2026-08-26-12' for 12:00–24:00 UTC. Changes every
    12 hours, so anything keyed on this (assignment, baselines, claims)
    naturally resets on that cadence."""
    now = datetime.now(timezone.utc)
    block = (now.hour // _QUEST_PERIOD_HOURS) * _QUEST_PERIOD_HOURS
    return f"{now.strftime('%Y-%m-%d')}-{block:02d}"


def quest_period_seconds_remaining() -> int:
    """Seconds left until the current 12h quest period rolls over (i.e.
    until new quests unlock). Used to show a live countdown in /quests."""
    now = datetime.now(timezone.utc)
    block_start_hour = (now.hour // _QUEST_PERIOD_HOURS) * _QUEST_PERIOD_HOURS
    block_start = now.replace(hour=block_start_hour, minute=0, second=0, microsecond=0)
    block_end = block_start + timedelta(hours=_QUEST_PERIOD_HOURS)
    return max(0, int((block_end - now).total_seconds()))


def _reset_entry(uid: str, today: str) -> dict:
    """Return a fresh rate-limit entry and persist it."""
    entry = {"count": 0, "day": today}
    _data["rate_limits"][uid] = entry
    _schedule_save("rate_limits")
    return entry


def get_ai_usage(user_id: int) -> tuple[int, str]:
    uid   = str(user_id)
    today = _today_utc()
    entry = _data["rate_limits"].get(uid)
    if not entry or entry.get("day") != today:
        entry = _reset_entry(uid, today)
    return entry["count"], today

def increment_ai_usage(user_id: int) -> int:
    uid   = str(user_id)
    today = _today_utc()
    entry = _data["rate_limits"].get(uid)
    if not entry or entry.get("day") != today:
        entry = _reset_entry(uid, today)
    entry["count"] += 1
    _schedule_save("rate_limits")
    return entry["count"]

def get_ai_limit(bonus: int = 0) -> int:
    # Enforce the single source of truth for daily AI usage limits.
    # `bonus` lets callers (cogs/ai.py) add a perk-driven extra allowance
    # (e.g. VIP/Elite) without this module importing cogs.economy — keeps
    # state.py free of a circular dependency on the cog that imports it.
    return DAILY_AI_LIMIT + bonus

def is_ai_rate_limited(user_id: int, bonus: int = 0) -> bool:
    count, _ = get_ai_usage(user_id)
    return count >= get_ai_limit(bonus)

def reset_ai_usage(user_id: int) -> None:
    uid = str(user_id)
    _data["rate_limits"][uid] = {"count": 0, "day": _today_utc()}
    _schedule_save("rate_limits")


# ── Generic settings storage ──────────────────────────────────────────────────

# In-memory cache for settings — avoids dict-in-dict lookup on every message.
# Invalidated on every set_setting() call so values are always fresh.
_settings_cache: dict[str, object] = {}

def get_setting(key: str, default=None):
    if key in _settings_cache:
        return _settings_cache[key]
    val = _data.get("settings", {}).get(key, default)
    _settings_cache[key] = val
    return val


def set_setting(key: str, value) -> None:
    if "settings" not in _data:
        _data["settings"] = {}
    _data["settings"][key] = value
    _settings_cache[key] = value  # update cache immediately
    _schedule_save("settings")


def get_preferred_name(user_id: int) -> str | None:
    return _data.get("preferred_names", {}).get(str(user_id))


def set_preferred_name(user_id: int, name: str) -> None:
    if "preferred_names" not in _data:
        _data["preferred_names"] = {}
    _data["preferred_names"][str(user_id)] = name
    _schedule_save("preferred_names")


def clear_preferred_name(user_id: int) -> bool:
    uid = str(user_id)
    if uid in _data.get("preferred_names", {}):
        del _data["preferred_names"][uid]
        _schedule_save("preferred_names")
        return True
    return False


def get_reminders(user_id: int) -> list[dict[str, object]]:
    return list(_data.get("reminders", {}).get(str(user_id), []))


def add_reminder(user_id: int, when: float, content: str) -> int:
    if "reminders" not in _data:
        _data["reminders"] = {}
    uid = str(user_id)
    if uid not in _data["reminders"]:
        _data["reminders"][uid] = []
    reminders = _data["reminders"][uid]
    new_id = max((reminder.get("id", 0) for reminder in reminders), default=0) + 1
    reminder = {"id": new_id, "when": when, "content": content}
    reminders.append(reminder)
    _schedule_save("reminders")
    return new_id


def delete_reminder(user_id: int, reminder_id: int) -> bool:
    uid = str(user_id)
    reminders = _data.get("reminders", {}).get(uid)
    if not reminders:
        return False
    new_reminders = [r for r in reminders if r.get("id") != reminder_id]
    if len(new_reminders) == len(reminders):
        return False
    _data["reminders"][uid] = new_reminders
    _schedule_save("reminders")
    return True


def pop_due_reminders(now: float | None = None) -> list[tuple[int, dict[str, object]]]:
    if now is None:
        now = time.time()
    due: list[tuple[int, dict[str, object]]] = []
    for uid_str, reminders in list(_data.get("reminders", {}).items()):
        remaining = []
        for reminder in reminders:
            if reminder.get("when", 0) <= now:
                try:
                    uid = int(uid_str)
                except ValueError:
                    continue
                due.append((uid, reminder))
            else:
                remaining.append(reminder)
        if remaining:
            _data["reminders"][uid_str] = remaining
        else:
            del _data["reminders"][uid_str]
    if due:
        _schedule_save("reminders")
    return due


def get_user_playlists(user_id: int) -> dict[str, list[dict[str, object]]]:
    return _data.get("playlists", {}).get(str(user_id), {})


def set_user_playlist(user_id: int, name: str, tracks: list[dict[str, object]]) -> None:
    if "playlists" not in _data:
        _data["playlists"] = {}
    uid = str(user_id)
    if uid not in _data["playlists"]:
        _data["playlists"][uid] = {}
    _data["playlists"][uid][name] = tracks
    _schedule_save("playlists")


def delete_user_playlist(user_id: int, name: str) -> bool:
    uid = str(user_id)
    if "playlists" not in _data or uid not in _data["playlists"]:
        return False
    if name not in _data["playlists"][uid]:
        return False
    del _data["playlists"][uid][name]
    _schedule_save("playlists")
    # Clean up any sharing metadata tied to the deleted playlist so stale
    # entries don't linger in other users' inboxes forever.
    shares = _data.get("playlist_shares", {}).get(uid, {})
    if name in shares:
        del shares[name]
        _schedule_save("playlist_shares")
    for target_uid, entries in list(_data.get("playlist_inbox", {}).items()):
        kept = [e for e in entries if not (e.get("owner_id") == user_id and e.get("name") == name)]
        if len(kept) != len(entries):
            _data["playlist_inbox"][target_uid] = kept
            _schedule_save("playlist_inbox")
    return True


def rename_user_playlist(user_id: int, old_name: str, new_name: str) -> bool:
    """Rename a playlist in place, preserving its tracks and any active shares."""
    uid = str(user_id)
    playlists = _data.get("playlists", {}).get(uid, {})
    if old_name not in playlists or new_name in playlists:
        return False
    playlists[new_name] = playlists.pop(old_name)
    _schedule_save("playlists")

    shares = _data.get("playlist_shares", {}).get(uid, {})
    if old_name in shares:
        shares[new_name] = shares.pop(old_name)
        _schedule_save("playlist_shares")
        for target_uid in shares[new_name]:
            for entry in _data.get("playlist_inbox", {}).get(target_uid, []):
                if entry.get("owner_id") == user_id and entry.get("name") == old_name:
                    entry["name"] = new_name
        _schedule_save("playlist_inbox")
    return True


# ── Playlist sharing / permissions ──────────────────────────────────────────
# permission is one of: "read" (can view/play/copy) or "write" (can also
# add/remove tracks on the ORIGINAL playlist, like a shared Google Doc).

def share_user_playlist(owner_id: int, name: str, target_id: int, permission: str = "read") -> bool:
    if permission not in ("read", "write"):
        return False
    owner_uid = str(owner_id)
    if name not in _data.get("playlists", {}).get(owner_uid, {}):
        return False

    shares = _data.setdefault("playlist_shares", {}).setdefault(owner_uid, {})
    shares.setdefault(name, {})[str(target_id)] = permission
    _schedule_save("playlist_shares")

    inbox = _data.setdefault("playlist_inbox", {}).setdefault(str(target_id), [])
    # Replace any existing pending entry for the same owner+playlist rather than duplicating.
    inbox[:] = [e for e in inbox if not (e.get("owner_id") == owner_id and e.get("name") == name)]
    inbox.append({"owner_id": owner_id, "name": name, "permission": permission, "ts": time.time()})
    _schedule_save("playlist_inbox")
    return True


def revoke_user_playlist_share(owner_id: int, name: str, target_id: int) -> bool:
    owner_uid = str(owner_id)
    shares = _data.get("playlist_shares", {}).get(owner_uid, {}).get(name, {})
    removed = shares.pop(str(target_id), None) is not None
    if removed:
        _schedule_save("playlist_shares")

    inbox = _data.get("playlist_inbox", {}).get(str(target_id), [])
    kept = [e for e in inbox if not (e.get("owner_id") == owner_id and e.get("name") == name)]
    if len(kept) != len(inbox):
        _data["playlist_inbox"][str(target_id)] = kept
        _schedule_save("playlist_inbox")
        removed = True
    return removed


def get_playlist_shares(owner_id: int, name: str) -> dict[str, str]:
    """target_id (str) -> permission, for a playlist you own."""
    return dict(_data.get("playlist_shares", {}).get(str(owner_id), {}).get(name, {}))


def get_playlist_inbox(user_id: int) -> list[dict[str, object]]:
    """Playlists that have been shared WITH this user (pending in their inbox)."""
    return list(_data.get("playlist_inbox", {}).get(str(user_id), []))


def get_playlist_permission(owner_id: int, name: str, requester_id: int) -> str | None:
    """Returns 'owner', 'read', 'write', or None if requester has no access."""
    if owner_id == requester_id:
        return "owner"
    return _data.get("playlist_shares", {}).get(str(owner_id), {}).get(name, {}).get(str(requester_id))


def find_accessible_playlist(user_id: int, name: str) -> tuple[int, str, list[dict[str, object]], str] | None:
    """
    Resolve `name` to a playlist the user can access — either one they own,
    or one shared with them (case-insensitive match).
    Returns (owner_id, canonical_name, tracks, permission) or None.
    """
    own = get_user_playlists(user_id)
    key = next((k for k in own if k.lower() == name.lower()), None)
    if key is not None:
        return user_id, key, own[key], "owner"

    for entry in get_playlist_inbox(user_id):
        if entry["name"].lower() == name.lower():
            owner_tracks = get_user_playlists(entry["owner_id"]).get(entry["name"])
            if owner_tracks is not None:
                return entry["owner_id"], entry["name"], owner_tracks, entry["permission"]
    return None


def get_song_history(user_id: int, limit: int = 50) -> list[dict[str, object]]:
    history = _data.get("song_history", {}).get(str(user_id), [])
    return history[-limit:]


def set_dnd(user_id: int, enabled: bool) -> None:
    """Enable or disable Do Not Disturb mode for a user."""
    if "dnd_users" not in _data:
        _data["dnd_users"] = {}
    if enabled:
        _data["dnd_users"][str(user_id)] = True
    else:
        _data["dnd_users"].pop(str(user_id), None)
    _schedule_save("dnd_users")


def is_dnd(user_id: int) -> bool:
    """Check if user has DND mode enabled."""
    return str(user_id) in _data.get("dnd_users", {})


def append_song_history(user_id: int, track: dict[str, object], max_items: int = 50) -> None:
    if "song_history" not in _data:
        _data["song_history"] = {}
    uid = str(user_id)
    if uid not in _data["song_history"]:
        _data["song_history"][uid] = []
    history = _data["song_history"][uid]
    history.append(track)
    if len(history) > max_items:
        del history[:-max_items]
    _schedule_save("song_history")
    # Lifetime counter — kept separate from the capped history above so
    # achievement thresholds (e.g. "played 250 songs") stay accurate even
    # after old plays get trimmed off song_history.
    _data["songs_played"][uid] = _data["songs_played"].get(uid, 0) + 1
    _schedule_save("songs_played")


# ══════════════════════════════════════════════════════════════════════════════
# BURST PROTECTION
# ══════════════════════════════════════════════════════════════════════════════

_burst_records: dict[int, deque] = {}


def check_burst_and_maybe_timeout(user_id: int) -> tuple[bool, float | None]:
    now = time.monotonic()
    window = float(get_setting("burst_window_seconds", 60.0))
    limit = int(get_setting("burst_limit_count", 20))
    timeout = float(get_setting("burst_timeout_seconds", 300.0))

    dq = _burst_records.setdefault(user_id, deque())
    dq.append(now)
    cutoff = now - window
    while dq and dq[0] < cutoff:
        dq.popleft()



    if len(dq) >= limit:
        bot_bans[str(user_id)] = {
            "reason": f"Flooding commands ({len(dq)} in {int(window)}s)",
            "expires": time.time() + timeout,
        }
        save_bans()
        dq.clear()
        print(f"[burst] Timed out user {user_id}: {limit} hits (limit={limit}, window={window}s, timeout={timeout}s)")
        return False, timeout

    return True, None


def get_burst_status(user_id: int) -> dict:
    """Live snapshot of a user's current burst-window activity, for display
    purposes only (does not mutate state or trigger a timeout). Useful for
    spotting a user who's ramping up before they actually hit the limit."""
    now    = time.monotonic()
    window = float(get_setting("burst_window_seconds", 60.0))
    limit  = int(get_setting("burst_limit_count", 20))

    dq = _burst_records.get(user_id)
    if not dq:
        count = 0
    else:
        cutoff = now - window
        count = sum(1 for t in dq if t >= cutoff)

    return {
        "count":  count,
        "limit":  limit,
        "window": window,
        "pct":    round((count / limit) * 100) if limit else 0,
    }


# ── Mention spam protection ──────────────────────────────────────────────────

_mention_records: dict[tuple[int, int], deque] = {}

def record_mention(invoker_id: int, target_id: int) -> tuple[bool, float | None]:
    """Record that `invoker_id` caused the bot to mention `target_id`.

    Returns (allowed, timeout_seconds). If allowed is False the invoker has
    been temporarily bot-banned and the timeout value is returned.
    """
    now = time.monotonic()
    window = float(get_setting("mention_window_seconds", 60.0))
    limit = int(get_setting("mention_limit_count", 4))
    timeout = float(get_setting("mention_timeout_seconds", 600.0))

    key = (invoker_id, target_id)
    dq = _mention_records.setdefault(key, deque())
    dq.append(now)
    cutoff = now - window
    while dq and dq[0] < cutoff:
        dq.popleft()

    # Trigger only when more than the configured limit within the window.
    if len(dq) > limit:
        # Temp ban the invoker
        bot_bans[str(invoker_id)] = {
            "reason": f"Mention spamming user {target_id} ({len(dq)} in {int(window)}s)",
            "expires": time.time() + timeout,
        }
        save_bans()
        dq.clear()
        print(f"[mention] Timed out user {invoker_id} for mentioning {target_id}: {len(dq)} hits (limit={limit}, window={window}s, timeout={timeout}s)")
        return False, timeout

    return True, None


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND COOLDOWN
# ══════════════════════════════════════════════════════════════════════════════

_last_command_time: dict[int, float] = {}

def check_cooldown(user_id: int, cooldown_multiplier: float = 1.0) -> bool:
    """Check if user has waited long enough since last command/message.
    Returns True if cooldown passed, False if still cooling down.
    `cooldown_multiplier` lets callers (cogs/ai.py) shrink the wait for a
    perk-driven title (VIP/Elite) without this module importing cogs.economy.
    """
    now = time.monotonic()
    last = _last_command_time.get(user_id)
    cooldown = float(get_setting("user_command_cooldown", 2.0)) * cooldown_multiplier
    if last is None or (now - last) >= cooldown:
        _last_command_time[user_id] = now
        return True
    return False

# ══════════════════════════════════════════════════════════════════════════════
# JARVIS CREDITS (JC)
# ══════════════════════════════════════════════════════════════════════════════

# Streak length (consecutive days chatted) → JC bonus paid out once that
# length is reached. Kept here (not economy.py) so state.py's bump_streak()
# has no import dependency on the economy cog.
STREAK_MILESTONES: dict[int, int] = {
    7:  200,   # 🔥 7-day streak bonus
    30: 500,   # ⭐ Monthly loyal user
}

def get_all_credits() -> dict[str, int]:
    """Return a copy of the str(user_id) → JC balance mapping."""
    return dict(_data["credits"])


def get_credits(user_id: int) -> int:
    """Return the user's current JC balance."""
    return int(_data["credits"].get(str(user_id), 0))


def add_credits(user_id: int, amount: int) -> int:
    """Add (or subtract, if amount is negative) JC. Balance never goes below 0.
    Returns the new balance."""
    uid = str(user_id)
    bal = _data["credits"].get(uid, 0) + amount
    if bal < 0:
        bal = 0
    _data["credits"][uid] = bal
    _schedule_save("credits")
    if amount > 0:
        _data["lifetime_earned"][uid] = _data["lifetime_earned"].get(uid, 0) + amount
        _schedule_save("lifetime_earned")
    return bal


def get_lifetime_earned(user_id: int) -> int:
    """Cumulative JC ever gained via add_credits() — a 'net worth' style
    stat that only ever goes up, unlike the spendable balance. Doesn't
    include amounts moved purely via spend_credits() (which only deducts,
    never grants), so it reflects earnings only."""
    return int(_data["lifetime_earned"].get(str(user_id), 0))


def get_lifetime_spent(user_id: int) -> int:
    """Cumulative JC ever deducted via spend_credits() — only ever goes up,
    mirroring get_lifetime_earned() but for the spending side."""
    return int(_data["lifetime_spent"].get(str(user_id), 0))


def spend_credits(user_id: int, amount: int) -> bool:
    """Attempt to deduct `amount` JC. Returns False if insufficient."""
    uid = str(user_id)
    bal = _data["credits"].get(uid, 0)
    if bal < amount:
        return False
    _data["credits"][uid] = bal - amount
    _schedule_save("credits")
    _data["lifetime_spent"][uid] = _data["lifetime_spent"].get(uid, 0) + amount
    _schedule_save("lifetime_spent")

    # Notify any registered event cog listening for JC-spent activity
    # (e.g. a seasonal quest event). No-op if none is registered — see
    # cogs/quest_hooks.py.
    fire_quest_hook("jc_spent", user_id, amount)

    return True

def _credit_meta(user_id: int) -> dict:
    uid = str(user_id)
    meta = _data["credit_meta"].get(uid)
    if not meta:
        meta = {
            "last_daily": "", "chat_day": "", "chat_count": 0,
            "streak": 0, "last_streak_day": "", "streak_milestones": [],
        }
        _data["credit_meta"][uid] = meta
    else:
        # Backfill defaults for users created before streaks/mystery-box existed.
        meta.setdefault("streak", 0)
        meta.setdefault("last_streak_day", "")
        meta.setdefault("streak_milestones", [])
    return meta


def claim_daily_credits(user_id: int, amount: int) -> tuple[bool, int]:
    """Grant the daily JC bonus if the user hasn't claimed it today.
    Returns (claimed, new_balance). claimed=False if already claimed today."""
    today = _today_utc()
    meta = _credit_meta(user_id)
    if meta.get("last_daily") == today:
        return False, get_credits(user_id)
    meta["last_daily"] = today
    _schedule_save("credit_meta")
    new_balance = add_credits(user_id, amount)
    return True, new_balance


def earn_chat_credits(user_id: int, amount: int, daily_cap: int) -> int:
    """Award JC for an AI chat message, up to `daily_cap` JC per day from
    this source. Returns the amount actually awarded (0 if cap reached)."""
    today = _today_utc()
    meta = _credit_meta(user_id)
    if meta.get("chat_day") != today:
        meta["chat_day"] = today
        meta["chat_count"] = 0
    if meta["chat_count"] >= daily_cap:
        return 0
    award = min(amount, daily_cap - meta["chat_count"])
    meta["chat_count"] += award
    _schedule_save("credit_meta")
    add_credits(user_id, award)
    return award


def get_streak(user_id: int) -> int:
    """Return the user's current consecutive daily-chat streak."""
    return int(_credit_meta(user_id).get("streak", 0))


def bump_streak(user_id: int) -> tuple[int, list[int]]:
    """
    Advance the user's daily streak. Call this once per UTC day, at the same
    point `claim_daily_credits` fires (first message of the day) — both rely
    on the same `chat_day`/date-rollover signal, so they always stay in sync.

    Streak rules:
      - Same day as last bump → no-op, streak unchanged.
      - Exactly the day after `last_streak_day` → streak += 1.
      - Any gap (missed a day, or brand new user) → streak resets to 1.

    Returns (new_streak, newly_hit_milestones) where newly_hit_milestones is
    a subset of STREAK_MILESTONES the user just reached *this call* (usually
    empty, sometimes one entry). Milestones only fire once per user ever —
    `streak_milestones` tracks which ones have already been paid out so a
    user who breaks and rebuilds a streak across the same milestone twice
    still gets paid both times (it's cleared on reset, see below).
    """
    today = _today_utc()
    meta = _credit_meta(user_id)

    if meta["last_streak_day"] == today:
        return meta["streak"], []  # already counted today

    if meta["last_streak_day"]:
        try:
            last = datetime.strptime(meta["last_streak_day"], "%Y-%m-%d").date()
            cur = datetime.strptime(today, "%Y-%m-%d").date()
            consecutive = (cur - last).days == 1
        except ValueError:
            consecutive = False
    else:
        consecutive = False

    if consecutive:
        meta["streak"] += 1
    else:
        meta["streak"] = 1
        meta["streak_milestones"] = []  # streak broke — milestones can be earned again

    meta["last_streak_day"] = today

    hit = [m for m in STREAK_MILESTONES if meta["streak"] == m and m not in meta["streak_milestones"]]
    for m in hit:
        meta["streak_milestones"].append(m)

    _schedule_save("credit_meta")
    return meta["streak"], hit


# ── top.gg vote tracking ─────────────────────────────────────────────────────
# top.gg lets a user vote once every 12h; the webhook (web/app.py) only fires
# when a real vote lands, so streak continuity is judged purely by the gap
# between consecutive votes rather than a calendar day like the chat streak.

VOTE_COOLDOWN_SECONDS = 12 * 3600       # top.gg's own per-user vote cooldown
VOTE_STREAK_GRACE_SECONDS = 24 * 3600   # must vote again within this window
                                          # of the last vote to keep the streak
                                          # alive (one missed 12h slot is OK,
                                          # a full missed day is not)

VOTE_STREAK_MILESTONES: dict[int, int] = {
    3:  50,    # 🔥 3 votes in a row
    7:  150,   # ⭐ a full week of voting
    30: 750,   # 👑 a month of voting
}


def _vote_entry(user_id: int) -> dict:
    uid = str(user_id)
    entry = _data["votes"].get(uid)
    if not entry:
        entry = {
            "total_votes": 0, "streak": 0, "last_vote_ts": 0.0, "streak_milestones": [],
            "pending_boxes": 0, "reminder_enabled": False, "reminder_sent": False,
        }
        _data["votes"][uid] = entry
    else:
        entry.setdefault("total_votes", 0)
        entry.setdefault("streak", 0)
        entry.setdefault("last_vote_ts", 0.0)
        entry.setdefault("streak_milestones", [])
        entry.setdefault("pending_boxes", 0)
        entry.setdefault("reminder_enabled", False)
        entry.setdefault("reminder_sent", False)
    return entry


def get_vote_stats(user_id: int) -> dict:
    """Read-only snapshot of a user's vote stats, plus derived cooldown info.
    Safe to call even if the user has never voted."""
    entry = _vote_entry(user_id)
    now = time.time()
    last_ts = entry["last_vote_ts"]
    next_vote_ts = last_ts + VOTE_COOLDOWN_SECONDS if last_ts else 0.0
    return {
        "total_votes": entry["total_votes"],
        "streak": entry["streak"],
        "last_vote_ts": last_ts,
        "next_vote_ts": next_vote_ts,
        "can_vote_now": (not last_ts) or now >= next_vote_ts,
        "pending_boxes": entry["pending_boxes"],
        "reminder_enabled": entry["reminder_enabled"],
    }


def bump_vote(user_id: int) -> dict:
    """Record a fresh top.gg vote (call this from the /webhook/topgg handler
    only — this is trusted, authenticated input, not user-triggered).

    Streak continues if this vote lands within VOTE_STREAK_GRACE_SECONDS of
    the previous one, otherwise it resets to 1. Doesn't hand out any JC
    itself — it just banks one unclaimed Vote Mystery Box, which the user
    opens themselves via !voteclaim / /voteclaim. Returns the updated stats
    plus any newly-hit streak milestones (subset of VOTE_STREAK_MILESTONES),
    each fired once per user per streak run — same pattern as bump_streak().
    """
    entry = _vote_entry(user_id)
    now = time.time()
    last_ts = entry["last_vote_ts"]

    if last_ts and (now - last_ts) <= VOTE_STREAK_GRACE_SECONDS:
        entry["streak"] += 1
    else:
        entry["streak"] = 1
        entry["streak_milestones"] = []

    entry["total_votes"] += 1
    entry["last_vote_ts"] = now
    entry["pending_boxes"] += 1
    entry["reminder_sent"] = False  # new cooldown window — allow a fresh reminder once it elapses

    hit = [m for m in VOTE_STREAK_MILESTONES if entry["streak"] == m and m not in entry["streak_milestones"]]
    for m in hit:
        entry["streak_milestones"].append(m)

    _schedule_save("votes")
    return {
        "total_votes": entry["total_votes"],
        "streak": entry["streak"],
        "last_vote_ts": entry["last_vote_ts"],
        "next_vote_ts": entry["last_vote_ts"] + VOTE_COOLDOWN_SECONDS,
        "pending_boxes": entry["pending_boxes"],
        "milestones_hit": hit,
    }


def claim_vote_box(user_id: int) -> bool:
    """Consume one unclaimed Vote Mystery Box. Returns False if the user
    has none pending (caller should not grant a reward in that case)."""
    entry = _vote_entry(user_id)
    if entry["pending_boxes"] <= 0:
        return False
    entry["pending_boxes"] -= 1
    _schedule_save("votes")
    return True


def claim_vote_boxes(user_id: int, amount: int) -> int:
    """Consume up to `amount` unclaimed Vote Mystery Boxes at once (for
    !voteclaim <quantity>). Returns the number actually claimed, which may
    be less than `amount` if fewer were pending — 0 means none were
    claimed and the caller should not grant any reward."""
    entry = _vote_entry(user_id)
    claimed = max(0, min(amount, entry["pending_boxes"]))
    if claimed <= 0:
        return 0
    entry["pending_boxes"] -= claimed
    _schedule_save("votes")
    return claimed


def get_vote_reminder_enabled(user_id: int) -> bool:
    """Whether this user has opted into a DM the moment their top.gg vote
    cooldown resets."""
    return bool(_vote_entry(user_id)["reminder_enabled"])


def set_vote_reminder_enabled(user_id: int, enabled: bool) -> None:
    """Toggle vote-reset DM reminders on/off for this user."""
    entry = _vote_entry(user_id)
    entry["reminder_enabled"] = bool(enabled)
    _schedule_save("votes")


def get_users_due_for_vote_reminder() -> list[int]:
    """Users who opted in, haven't already been reminded this cooldown
    cycle, and whose 12h vote cooldown has just elapsed. Meant to be
    polled periodically (see cogs/vote.py's background loop) rather than
    computed on every state change."""
    now = time.time()
    due = []
    for uid_str, entry in _data["votes"].items():
        if not entry.get("reminder_enabled"):
            continue
        if entry.get("reminder_sent"):
            continue
        last_ts = entry.get("last_vote_ts", 0.0)
        if not last_ts:
            continue
        if now >= last_ts + VOTE_COOLDOWN_SECONDS:
            due.append(int(uid_str))
    return due


def mark_vote_reminder_sent(user_id: int) -> None:
    """Mark this cooldown cycle's reminder as sent (or attempted) so the
    background loop doesn't keep retrying every cycle — cleared again the
    next time the user actually votes."""
    entry = _vote_entry(user_id)
    entry["reminder_sent"] = True
    _schedule_save("votes")


def get_vote_leaderboard(limit: int = 10) -> list[tuple[str, dict]]:
    """Top voters by total_votes, most votes first. Returns (user_id_str, entry) pairs."""
    ranked = sorted(
        _data["votes"].items(),
        key=lambda kv: (kv[1].get("total_votes", 0), kv[1].get("streak", 0)),
        reverse=True,
    )
    return ranked[:limit]


def grant_onboarding_bonus(user_id: int, amount: int) -> int:
    """One-time JC grant for a brand-new user. Returns new balance."""
    return add_credits(user_id, amount)


# ══════════════════════════════════════════════════════════════════════════════
# REFERRALS
# ══════════════════════════════════════════════════════════════════════════════
#
# How attribution works:
#   - Every user can generate their own stable referral code with get_or_create_referral_code().
#   - A code is only ever consumed via redeem_referral_code(), which must be the
#     FIRST thing a brand-new user does — it checks is_new_user() itself and
#     refuses anyone who has already been "seen" by the bot, so chatting first
#     and redeeming a code afterwards never counts.
#   - referred_by is also the de-dupe guard: once set for a user, that user can
#     never redeem a second code (prevents farming bonuses with the same code
#     on the same account, or chaining referrals).

import random as _random
import string as _string

REFERRAL_CODE_LENGTH = 8
REFERRAL_CODE_ALPHABET = _string.ascii_uppercase + _string.digits


def _generate_referral_code() -> str:
    return "".join(_random.choices(REFERRAL_CODE_ALPHABET, k=REFERRAL_CODE_LENGTH))


def get_or_create_referral_code(user_id: int) -> str:
    """Return the user's existing referral code, generating one if needed.
    Codes are stable for a user's lifetime (used by !invite / /invite)."""
    uid = str(user_id)
    code = _data["referral_codes"].get(uid)
    if code:
        return code

    existing = set(_data["referral_codes"].values())
    code = _generate_referral_code()
    while code in existing:
        code = _generate_referral_code()

    _data["referral_codes"][uid] = code
    _schedule_save("referral_codes")
    return code


def get_referrer_id_for_code(code: str) -> int | None:
    """Resolve a referral code back to the referrer's user_id, or None if invalid."""
    code = code.strip().upper()
    for uid, c in _data["referral_codes"].items():
        if c == code:
            return int(uid)
    return None


def has_been_referred(user_id: int) -> bool:
    """True if this user has already redeemed a referral code (ever)."""
    return str(user_id) in _data["referred_by"]


def get_referral_count(user_id: int) -> int:
    """How many people this user has successfully referred (i.e. how many
    entries in referred_by point back to them). Used on /profile."""
    return sum(1 for referrer_id in _data["referred_by"].values() if referrer_id == user_id)


def redeem_referral_code(user_id: int, code: str) -> tuple[bool, str, int | None]:
    """
    Attempt to attribute `user_id` as referred by whoever owns `code`.

    This is intentionally strict — it's the only path that can ever set
    referred_by, and it refuses unless ALL of the following hold:
      - the code resolves to a real referrer
      - the redeemer isn't the referrer themselves
      - the redeemer is still a brand-new user (is_new_user) — i.e. this is
        the first thing they've ever done with Jarvis, not a retroactive claim
        after chatting normally
      - the redeemer has never redeemed any code before

    Returns (success, reason, referrer_id):
      reason is one of: "ok", "invalid_code", "self_referral",
      "already_seen" (chatted/used Jarvis before redeeming — too late),
      "already_referred" (already redeemed a code previously).
      referrer_id is the resolved referrer's id on success, else None.
    """
    referrer_id = get_referrer_id_for_code(code)
    if referrer_id is None:
        return False, "invalid_code", None

    if referrer_id == user_id:
        return False, "self_referral", None

    if not is_new_user(user_id):
        return False, "already_seen", None

    if has_been_referred(user_id):
        return False, "already_referred", None

    _data["referred_by"][str(user_id)] = referrer_id
    _schedule_save("referred_by")
    return True, "ok", referrer_id


# ══════════════════════════════════════════════════════════════════════════════
# GUILD LOGS
# ══════════════════════════════════════════════════════════════════════════════

def log_guild_join(guild_id: int, name: str, member_count: int, owner_id: int, joined_at: float | None = None) -> bool:
    """Record that the bot joined a guild.

    Returns True if this is a *new* entry (first time seeing this guild),
    False if the guild was already in the log (e.g. re-invite or on_ready scan).
    """
    gid = str(guild_id)
    if gid in _data["guild_logs"]:
        return False
    _data["guild_logs"][gid] = {
        "name":         name,
        "joined_at":    joined_at if joined_at is not None else time.time(),
        "member_count": member_count,
        "owner_id":     owner_id,
    }
    _schedule_save("guild_logs")
    return True


def get_guild_log(guild_id: int) -> dict | None:
    """Return the stored log entry for a guild, or None if not found."""
    return _data["guild_logs"].get(str(guild_id))


def get_all_guild_logs() -> dict[str, dict]:
    """Return a copy of all guild log entries, keyed by str(guild_id)."""
    return dict(_data["guild_logs"])


# ══════════════════════════════════════════════════════════════════════════════
# GAME STATS  (win/loss records, used by /profile and achievements)
# ══════════════════════════════════════════════════════════════════════════════

_GAME_STATS_DEFAULT = {
    "chess_wins": 0, "chess_losses": 0,
    "mafia_wins": 0, "mafia_losses": 0,
    "hangman_wins": 0,
}


def get_game_stats(user_id: int) -> dict:
    """Return a copy of the user's win/loss record, backfilled with defaults."""
    stats = dict(_GAME_STATS_DEFAULT)
    stats.update(_data["game_stats"].get(str(user_id), {}))
    return stats


def record_game_result(user_id: int, game: str, result: str) -> dict:
    """Record a win or loss for `game` ("chess"/"mafia"/"hangman") and
    `result` ("win"/"loss"). Returns the user's updated stats dict.

    Hangman has no "losses" bucket since it's a free-for-all shared game —
    only the person who guesses the word gets a recorded result.
    """
    uid = str(user_id)
    stats = _data["game_stats"].setdefault(uid, dict(_GAME_STATS_DEFAULT))
    for key, default in _GAME_STATS_DEFAULT.items():
        stats.setdefault(key, default)
    key = f"{game}_{'wins' if result == 'win' else 'losses'}"
    if key in stats:
        stats[key] += 1
    _schedule_save("game_stats")
    return dict(stats)


# ══════════════════════════════════════════════════════════════════════════════
# SONGS PLAYED  (lifetime counter — song_history above is capped/trimmed,
# this is not, so it stays accurate for achievements like "played 100 songs")
# ══════════════════════════════════════════════════════════════════════════════

def get_songs_played(user_id: int) -> int:
    return int(_data["songs_played"].get(str(user_id), 0))


def get_favorite_song(user_id: int) -> str | None:
    """Return the most-played track title from the user's (capped) song
    history, or None if they haven't played anything yet."""
    history = get_song_history(user_id)
    if not history:
        return None
    counts: dict[str, int] = {}
    for track in history:
        title = track.get("title")
        if title:
            counts[title] = counts.get(title, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


# ══════════════════════════════════════════════════════════════════════════════
# BADGES  (achievement unlocks — never purchasable, always earned)
# ══════════════════════════════════════════════════════════════════════════════

def get_badges(user_id: int) -> list[str]:
    return list(_data["badges"].get(str(user_id), []))


def unlock_badge(user_id: int, badge_id: str) -> bool:
    """Grant `badge_id` to the user if they don't already have it.
    Returns True if it was newly unlocked (so the caller can announce it)."""
    uid = str(user_id)
    owned = _data["badges"].setdefault(uid, [])
    if badge_id in owned:
        return False
    owned.append(badge_id)
    _schedule_save("badges")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# TITLES & BANNERS  (cosmetic, bot-rendered only — never real Discord roles,
# so behavior is identical across every server the bot is in)
# ══════════════════════════════════════════════════════════════════════════════

def _cosmetic_entry(kind: str, user_id: int) -> dict:
    uid = str(user_id)
    store = _data[kind]  # "titles" or "banners"
    entry = store.setdefault(uid, {"owned": [], "equipped": None})
    entry.setdefault("owned", [])
    entry.setdefault("equipped", None)
    return entry


def get_titles(user_id: int) -> dict:
    return dict(_cosmetic_entry("titles", user_id))


def grant_title(user_id: int, title_id: str) -> bool:
    """Add `title_id` to the user's owned titles. Returns True if newly owned."""
    entry = _cosmetic_entry("titles", user_id)
    if title_id in entry["owned"]:
        return False
    entry["owned"].append(title_id)
    _schedule_save("titles")
    return True


def equip_title(user_id: int, title_id: str | None) -> bool:
    """Equip an owned title (or None to unequip). Returns False if the
    user doesn't own that title."""
    entry = _cosmetic_entry("titles", user_id)
    if title_id is not None and title_id not in entry["owned"]:
        return False
    entry["equipped"] = title_id
    _schedule_save("titles")
    return True


def get_equipped_title(user_id: int) -> str | None:
    return _cosmetic_entry("titles", user_id).get("equipped")


def get_banners(user_id: int) -> dict:
    return dict(_cosmetic_entry("banners", user_id))


def grant_banner(user_id: int, banner_id: str) -> bool:
    entry = _cosmetic_entry("banners", user_id)
    if banner_id in entry["owned"]:
        return False
    entry["owned"].append(banner_id)
    _schedule_save("banners")
    return True


def equip_banner(user_id: int, banner_id: str | None) -> bool:
    entry = _cosmetic_entry("banners", user_id)
    if banner_id is not None and banner_id not in entry["owned"]:
        return False
    entry["equipped"] = banner_id
    _schedule_save("banners")
    return True


def get_equipped_banner(user_id: int) -> str | None:
    return _cosmetic_entry("banners", user_id).get("equipped")


def revoke_banner(user_id: int, banner_id: str) -> None:
    """Remove an owned banner entirely — used when a paid subscription that
    grants an exclusive banner (e.g. the VIP/Elite Prestige banner) lapses.
    Safe to call even if the user doesn't own it. Mirrors revoke_title."""
    entry = _cosmetic_entry("banners", user_id)
    if banner_id in entry["owned"]:
        entry["owned"].remove(banner_id)
        if entry["equipped"] == banner_id:
            entry["equipped"] = None
        _schedule_save("banners")


PROFILE_FIELDS = {
    "balance":  "Balance",
    "streak":   "Streak",
    "level":    "Level",
    "chess":    "Chess record",
    "mafia":    "Mafia record",
    "hangman":  "Hangman record",
    "songs":    "Songs played",
    "favorite": "Favorite song",
    "badges":   "Badges",
    "joined":   "First interaction date",
    "referrals":      "Referral count",
    "ai_messages":    "AI messages sent",
    "lifetime_earned": "Lifetime JC earned",
    "account_age":    "Account age",
}


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE PRIVACY  (per-field visibility — hides a stat from OTHER viewers'
# /profile calls; the owner always sees their own full card regardless)
# ══════════════════════════════════════════════════════════════════════════════

def get_hidden_fields(user_id: int) -> list[str]:
    return list(_data["profile_privacy"].get(str(user_id), []))


def set_field_hidden(user_id: int, field: str, hidden: bool) -> None:
    """Hide or unhide a single profile field for other viewers."""
    uid = str(user_id)
    hidden_fields = _data["profile_privacy"].setdefault(uid, [])
    if hidden:
        if field not in hidden_fields:
            hidden_fields.append(field)
    else:
        if field in hidden_fields:
            hidden_fields.remove(field)
    _schedule_save("profile_privacy")


def revoke_title(user_id: int, title_id: str) -> None:
    """Remove an owned title entirely — used when a paid subscription lapses.
    Safe to call even if the user doesn't own it."""
    entry = _cosmetic_entry("titles", user_id)
    if title_id in entry["owned"]:
        entry["owned"].remove(title_id)
        if entry["equipped"] == title_id:
            entry["equipped"] = None
        _schedule_save("titles")


# ══════════════════════════════════════════════════════════════════════════════
# TITLE SUBSCRIPTIONS  (recurring weekly JC billing for premium titles)
# ══════════════════════════════════════════════════════════════════════════════

def get_subscriptions(user_id: int) -> dict[str, float]:
    return dict(_data["title_subscriptions"].get(str(user_id), {}))


def set_subscription(user_id: int, title_id: str, next_charge_epoch: float) -> None:
    uid = str(user_id)
    subs = _data["title_subscriptions"].setdefault(uid, {})
    subs[title_id] = next_charge_epoch
    _schedule_save("title_subscriptions")


def clear_subscription(user_id: int, title_id: str) -> None:
    uid = str(user_id)
    subs = _data["title_subscriptions"].get(uid)
    if subs and title_id in subs:
        del subs[title_id]
        _schedule_save("title_subscriptions")


def get_all_subscriptions() -> dict[str, dict[str, float]]:
    """Return a copy of every user's subscriptions, keyed by str(user_id)."""
    return {uid: dict(subs) for uid, subs in _data["title_subscriptions"].items()}


def get_auto_renew(user_id: int, title_id: str) -> bool:
    """Whether a subscription renews automatically when its current period
    ends. Defaults to True (the original always-renew behavior) for any
    subscription that never had this explicitly set — so existing/older
    subscribers aren't silently switched to non-renewing."""
    return _data["title_autorenew"].get(str(user_id), {}).get(title_id, True)


def set_auto_renew(user_id: int, title_id: str, value: bool) -> None:
    uid = str(user_id)
    entry = _data["title_autorenew"].setdefault(uid, {})
    entry[title_id] = value
    _schedule_save("title_autorenew")


def clear_auto_renew(user_id: int, title_id: str) -> None:
    """Drop the stored flag entirely (back to the True default) — used when
    a subscription lapses/ends so stale flags don't pile up."""
    uid = str(user_id)
    entry = _data["title_autorenew"].get(uid)
    if entry and title_id in entry:
        del entry[title_id]
        _schedule_save("title_autorenew")

# ══════════════════════════════════════════════════════════════════════════════
# TERMS & CONDITIONS
# ══════════════════════════════════════════════════════════════════════════════
# Every user — brand new, or already using Jarvis before this feature shipped —
# must accept the Terms & Conditions once before Jarvis will respond to any
# command or chat message. Bumping TOS_VERSION re-prompts EVERYONE (including
# users who accepted an older version), which is how a material wording change
# would be rolled out in future.

TOS_VERSION = 1


def has_accepted_tos(user_id: int) -> bool:
    entry = _data["tos_accepted"].get(str(user_id))
    return bool(entry) and entry.get("version", 0) >= TOS_VERSION


def accept_tos(user_id: int) -> None:
    _data["tos_accepted"][str(user_id)] = {
        "version":     TOS_VERSION,
        "accepted_at": time.time(),
    }
    _schedule_save("tos_accepted")


def get_tos_status(user_id: int) -> dict | None:
    """Return {'version': int, 'accepted_at': float} or None if never accepted."""
    return _data["tos_accepted"].get(str(user_id))


def has_used_bot_before(user_id: int) -> bool:
    """Best-effort check for 'this person was already using Jarvis before
    the Terms & Conditions gate shipped' — used to show existing users a
    different framing ('we've introduced new Terms & Conditions') instead
    of the brand-new-user welcome prompt. Checks every table someone could
    already have a footprint in, since seen_users alone only gets marked on
    a first AI chat/referral redemption and would miss command-only users."""
    uid = str(user_id)
    if user_id in _data["seen"]:
        return True
    if uid in _data["stats"]:
        return True
    if _data["credits"].get(uid):
        return True
    if uid in _data["game_stats"]:
        return True
    if _data["badges"].get(uid):
        return True
    if _data["titles"].get(uid, {}).get("owned"):
        return True
    if _data["banners"].get(uid, {}).get("owned"):
        return True
    return False

# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM BREACH BADGES (persist after event)
# ══════════════════════════════════════════════════════════════════════════════

def get_system_breach_badges(user_id: int) -> list[str]:
    """Return list of System Breach badges earned by the user."""
    return list(_data["system_breach_badges"].get(str(user_id), []))

def grant_system_breach_badge(user_id: int, badge_id: str) -> bool:
    """Grant a System Breach badge. Returns False if already granted."""
    uid = str(user_id)
    badges = _data["system_breach_badges"].setdefault(uid, [])
    if badge_id in badges:
        return False
    badges.append(badge_id)
    _schedule_save("system_breach_badges")
    return True

# ══════════════════════════════════════════════════════════════════════════════
# LOCKED MESSAGES (!lock / /lock — a message only the intended recipient can open)
# ══════════════════════════════════════════════════════════════════════════════

def create_locked_message(lock_id: str, author_id: int, target_id: int, content: str) -> None:
    """Store a new locked message, keyed by a short random id embedded in the
    reveal button's custom_id. Persists via Turso so the button keeps working
    even if the bot restarts before it's opened."""
    _data["locked_messages"][lock_id] = {
        "author":  author_id,
        "target":  target_id,
        "content": content,
        "opened":  False,
        "created": time.time(),
    }
    _schedule_save("locked_messages")


def get_locked_message(lock_id: str) -> dict | None:
    """Return the locked message entry for lock_id, or None if it doesn't
    exist (expired data, tampered custom_id, etc.)."""
    return _data["locked_messages"].get(lock_id)


def mark_locked_message_opened(lock_id: str) -> None:
    """Flag a locked message as opened once the intended recipient reveals it."""
    entry = _data["locked_messages"].get(lock_id)
    if entry is not None:
        entry["opened"] = True
        _schedule_save("locked_messages")

# ══════════════════════════════════════════════════════════════════════════════
# CARD COLLECTION (beast cards — inventory, quantities, trading)
# ══════════════════════════════════════════════════════════════════════════════

def get_user_cards(user_id: int) -> dict[str, int]:
    """Return a copy of the user's card_id → quantity inventory."""
    return dict(_data["user_cards"].get(str(user_id), {}))


def get_card_quantity(user_id: int, card_id: str) -> int:
    """Return how many copies of card_id the user owns (0 if none)."""
    return int(_data["user_cards"].get(str(user_id), {}).get(card_id, 0))


def add_card(user_id: int, card_id: str, amount: int = 1) -> int:
    """Add `amount` copies of card_id to the user's inventory (duplicates
    stack as a quantity, not separate entries). Returns the new quantity
    owned of that card."""
    uid = str(user_id)
    inv = _data["user_cards"].setdefault(uid, {})
    new_qty = inv.get(card_id, 0) + amount
    inv[card_id] = new_qty
    _schedule_save("user_cards")
    return new_qty


def remove_card(user_id: int, card_id: str, amount: int = 1) -> bool:
    """Attempt to remove `amount` copies of card_id from the user's
    inventory. Returns False (and changes nothing) if they don't own
    enough. Drops the card_id key entirely once quantity hits 0."""
    uid = str(user_id)
    inv = _data["user_cards"].get(uid, {})
    have = inv.get(card_id, 0)
    if have < amount:
        return False
    remaining = have - amount
    if remaining <= 0:
        inv.pop(card_id, None)
    else:
        inv[card_id] = remaining
    _schedule_save("user_cards")
    return True


def transfer_card(sender_id: int, recipient_id: int, card_id: str, amount: int = 1) -> bool:
    """Atomically move `amount` copies of card_id from sender to recipient.
    Returns False (nothing changed) if the sender doesn't own enough —
    the deduction and grant either both happen or neither does."""
    if not remove_card(sender_id, card_id, amount):
        return False
    add_card(recipient_id, card_id, amount)
    return True


# ── Daily card pull ──────────────────────────────────────────────────────
# Mirrors claim_daily_credits' shape: one free weighted pull per UTC day,
# tracked separately from the JC daily bonus so the two cooldowns don't
# collide with each other.

def has_claimed_daily_card(user_id: int) -> bool:
    """Whether the user already claimed their free daily card pull today."""
    uid = str(user_id)
    return _data["card_meta"].get(uid, {}).get("last_daily_card") == _today_utc()


def claim_daily_card(user_id: int) -> bool:
    """Mark today's free card pull as claimed. Returns True if this call
    newly claimed it, False if it was already claimed today (caller should
    not grant a card in that case)."""
    uid = str(user_id)
    today = _today_utc()
    meta = _data["card_meta"].setdefault(uid, {})
    if meta.get("last_daily_card") == today:
        return False
    meta["last_daily_card"] = today
    _schedule_save("card_meta")
    return True


# ── Card quests ───────────────────────────────────────────────────────────
# state.py only stores whatever quest_ids/baselines the cog hands it — the
# meaning of each quest_id (what it tracks, its target, its reward) is
# entirely the cog's business. This keeps state.py a dumb persistence layer
# the same way the rest of the file works.
#
# Scoped to quest_period_utc() (a 12-hour block) rather than a calendar
# day, so quests assign/reset twice a day instead of once. The stored
# "period" key naturally invalidates any pre-upgrade "date"-keyed record
# the first time it's read — the user just gets a fresh set assigned.

def get_daily_quests(user_id: int) -> dict | None:
    """Return the current period's assigned quest record for this user,
    or None if they haven't been assigned quests yet this period (caller
    should then call assign_daily_quests)."""
    entry = _data["card_quests"].get(str(user_id))
    if not entry or entry.get("period") != quest_period_utc():
        return None
    return entry


def assign_daily_quests(user_id: int, quest_ids: list[str], baselines: dict[str, int]) -> dict:
    """Assign a fresh set of quests for the current period, overwriting
    any stale (previous-period) record. Returns the newly-stored record."""
    entry = {
        "period": quest_period_utc(),
        "quest_ids": list(quest_ids),
        "baselines": dict(baselines),
        "claimed": [],
    }
    _data["card_quests"][str(user_id)] = entry
    _schedule_save("card_quests")
    return entry


def mark_quest_claimed(user_id: int, quest_id: str) -> None:
    """Flag a quest as claimed so its reward can't be collected twice."""
    entry = _data["card_quests"].get(str(user_id))
    if entry is not None and quest_id not in entry["claimed"]:
        entry["claimed"].append(quest_id)
        _schedule_save("card_quests")


# ── Auto quests (cogs/auto_quests.py) ───────────────────────────────────────
# A SEPARATE, simpler quest system from the card quests above: one quest
# assigned per user per day, auto-detected and auto-rewarded (JC + a public
# log post) the moment it's completed — no manual claim button. Kept in its
# own "auto_quests" key so it can't collide with card_quests state.

def auto_quest_period_utc() -> str:
    """Calendar-day (UTC) period key — auto quests reset once a day, unlike
    the 12h card-quest period."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_auto_quest(user_id: int) -> dict | None:
    """Return this user's current-period auto quest record, or None if
    they haven't been assigned one yet this period (caller should then
    call assign_auto_quest)."""
    entry = _data["auto_quests"].get(str(user_id))
    if not entry or entry.get("period") != auto_quest_period_utc():
        return None
    return entry


def assign_auto_quest(user_id: int, quest_id: str, baseline: int) -> dict:
    """Assign (or reassign, e.g. right after a reward) a fresh auto quest
    for the current period."""
    entry = {
        "period": auto_quest_period_utc(),
        "quest_id": quest_id,
        "baseline": baseline,
        "rewarded": False,
    }
    _data["auto_quests"][str(user_id)] = entry
    _schedule_save("auto_quests")
    return entry


def mark_auto_quest_rewarded(user_id: int) -> None:
    """Flag the user's current auto quest as rewarded so the periodic
    checker doesn't pay it out twice before a new one gets assigned."""
    entry = _data["auto_quests"].get(str(user_id))
    if entry is not None:
        entry["rewarded"] = True
        _schedule_save("auto_quests")


def get_bump_count(user_id: int) -> int:
    """Lifetime count of successful server bumps detected for this user."""
    return _data["bump_counts"].get(str(user_id), 0)


def increment_bump_count(user_id: int) -> int:
    """Bump (pun intended) a user's lifetime bump count by 1. Returns the
    new count."""
    uid = str(user_id)
    _data["bump_counts"][uid] = _data["bump_counts"].get(uid, 0) + 1
    _schedule_save("bump_counts")
    return _data["bump_counts"][uid]


def get_server_message_count(user_id: int) -> int:
    """Lifetime count of ALL messages this user has sent in any guild the
    bot can see — unlike get_stats()["messages"], this is NOT limited to
    messages that went through the AI chat pipeline."""
    return _data["server_message_counts"].get(str(user_id), 0)


def increment_server_message_count(user_id: int) -> int:
    """Increment a user's server-wide message count by 1. Returns the new
    count. Call this from a plain on_message listener with no AI-trigger
    requirement — see cogs/auto_quests.py."""
    uid = str(user_id)
    _data["server_message_counts"][uid] = _data["server_message_counts"].get(uid, 0) + 1
    _schedule_save("server_message_counts")
    return _data["server_message_counts"][uid]


# ── Card set rewards ──────────────────────────────────────────────────────

def get_claimed_sets(user_id: int) -> list[str]:
    """Return the list of set_ids this user has already claimed the
    completion reward for."""
    return list(_data["card_set_claims"].get(str(user_id), []))


def claim_set_reward(user_id: int, set_id: str) -> bool:
    """Mark a set's completion reward as claimed. Returns False if it was
    already claimed before (caller should not grant the reward again)."""
    uid = str(user_id)
    claimed = _data["card_set_claims"].setdefault(uid, [])
    if set_id in claimed:
        return False
    claimed.append(set_id)
    _schedule_save("card_set_claims")
    return True


# ── API metrics persistence ───────────────────────────────────────────────
# Backs the !api dashboard's all-time counters (cogs/api_metrics.py). Those
# counters live in plain in-memory Python variables for speed (recorded on
# every AI call / DB round-trip / command — far too hot a path to hit Turso
# each time), so without this they silently reset to 0 on every restart.
# Instead, cogs/system.py periodically snapshots them here on a slow loop,
# and state.init_db() restores the last snapshot into api_metrics.py at
# startup.

def get_api_metrics_snapshot() -> dict:
    """Return the last-persisted !api counters (empty dict if nothing has
    been saved yet, e.g. a fresh install)."""
    return dict(_data["api_metrics"])


def set_api_metrics_snapshot(snapshot: dict) -> None:
    """Persist the current !api counters. Called on a periodic loop, not
    per-sample — see cogs/api_metrics.py's snapshot() docstring for why."""
    _data["api_metrics"] = dict(snapshot)
    _schedule_save("api_metrics")