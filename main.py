import discord
from discord.ext import commands
import os
import asyncio
import logging
import logging.handlers
import signal
import time
from dotenv import load_dotenv

load_dotenv()

from cogs.errorhandler import install_stdout_error_forwarding, install_view_error_suppression
from cogs.state import is_bot_banned, init_db, check_burst_and_maybe_timeout, check_cooldown, flush_all_saves, get_guild_prefix, is_new_user, mark_seen
from cogs.tos import ensure_tos
import cogs.http_session as http_session
from cogs.history import init_history, load_all_histories
from cogs.memory import init_memory
import uvicorn
from web.app import create_app

install_stdout_error_forwarding()
install_view_error_suppression()  

logging.basicConfig(level=logging.WARNING)
# wavelink logs its reconnect/retry attempts at INFO level. The root logger
# above is WARNING, which was silently swallowing those messages — this is
# why no reconnect activity showed up in the console during disconnects.
logging.getLogger("wavelink").setLevel(logging.INFO)

# Persist logs to disk in addition to stdout. The host's live console view
# can be empty/reset around a restart even though the process printed
# something right before dying — a file on the same container survives a
# plain process kill/restart (though not a full redeploy/new container).
# This is what lets us actually see what happened during the next
# Lavalink-connection-drop instead of an empty log window.
#
# Implemented as a builtins.print() monkeypatch rather than replacing
# sys.stdout — replacing sys.stdout previously broke uvicorn's logging
# setup (it introspects sys.stdout.isatty()/fileno() directly), and any
# other library could do the same. Patching print() only ever touches
# calls in THIS codebase and can't break third-party stream introspection.
try:
    os.makedirs("logs", exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(
        "logs/bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _file_handler.setLevel(logging.INFO)

    import builtins as _builtins

    # "stdout" mirror for print() — logs everything print() outputs to the
    # file too. propagate=False + its own handler (rather than relying on
    # root's) means these lines don't ALSO hit root's default console
    # handler and print twice on the terminal.
    _stdout_logger = logging.getLogger("stdout")
    _stdout_logger.addHandler(_file_handler)
    _stdout_logger.setLevel(logging.INFO)
    _stdout_logger.propagate = False

    # "quiet" logger for routine, expected background activity (e.g. the
    # watchdog silently self-healing a dropped Lavalink connection) that
    # should be on record in logs/bot.log for later debugging, but isn't
    # worth spamming the live terminal with every time it happens. Genuine
    # failures should still use print()/this file's own error path so they
    # stay visible.
    _quiet_logger = logging.getLogger("quiet")
    _quiet_logger.addHandler(_file_handler)
    _quiet_logger.setLevel(logging.INFO)
    _quiet_logger.propagate = False

    _original_print = _builtins.print

    def _print_and_log(*args, **kwargs):
        _original_print(*args, **kwargs)
        try:
            sep = kwargs.get("sep", " ")
            message = sep.join(str(a) for a in args).strip()
            if message:
                _stdout_logger.info(message)
        except Exception:
            pass  # never let file-logging break an actual print() call

    _builtins.print = _print_and_log
except Exception as _e:
    print(f"⚠️ Could not set up file logging: {_e}")

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members         = True
intents.voice_states = True


def _resolve_prefix(bot: commands.Bot, message: discord.Message) -> str:
    """Per-server custom prefix (set via !setprefix). DMs and guilds that
    haven't customised it fall back to the default '!'."""
    guild_id = message.guild.id if message.guild else None
    return get_guild_prefix(guild_id)


bot = commands.Bot(
    command_prefix=_resolve_prefix,
    intents=intents,
    help_command=None,
    allowed_mentions=discord.AllowedMentions.none(),
)

COGS = [
    "cogs.ai",
    "cogs.admin",
    "cogs.panel",
    "cogs.stats",
    "cogs.prompts",
    "cogs.announce",
    "cogs.dm",
    "cogs.suggestions",
    "cogs.bugreport",
    "cogs.vote",
    "cogs.errorhandler",
    "cogs.help",
    "cogs.game",
    "cogs.system",
    "cogs.image_search",
    "cogs.summary",
    "cogs.presence",
    "cogs.youtube",
    "cogs.music",
    "cogs.economy",
    "cogs.profile", 
    "cogs.status",
    "cogs.tos",
    "cogs.lockedmsg",
    "cogs.cards",
    "cogs.auto_quests",
]

# Track users we've already DM'd about their ban this session — avoid spamming
_dm_sent_bans: set[int] = set()


def _is_guild_banned(guild_id: int) -> bool:
    """Lazy import to avoid circular dependency at module load time."""
    try:
        from cogs.admin import _guild_bans
        return guild_id in _guild_bans
    except ImportError:
        return False


async def _notify_banned(user: discord.User | discord.Member) -> None:
    """DM a banned user once per session to inform them."""
    if user.id in _dm_sent_bans:
        return
    _dm_sent_bans.add(user.id)
    try:
        embed = discord.Embed(
            title="🚫 Banned from Jarvis",
            description="You are banned from using **Jarvis** and cannot use any of its commands.",
            color=discord.Color.red(),
        )
        embed.set_footer(text="If you believe this is a mistake, contact Phantom.")
        await user.send(embed=embed)
    except discord.Forbidden:
        pass


def _maybe_register_new_user(user: discord.User | discord.Member) -> None:
    """If this is the user's first-ever interaction with Jarvis, register them
    right away — mark_seen() plus the same new-user webhook the AI chat path
    already fires — no matter which command triggered it. Called from both
    global command checks below so it covers every "!"/"/" command, not just
    AI chat or !redeem (each of which already handles its own registration
    and is skipped here — see the callers)."""
    if not is_new_user(user.id):
        return
    mark_seen(user.id)

    async def _fire():
        try:
            # Deferred import: cogs.ai isn't safe to import at module load
            # time from here.
            from cogs.ai import _log_new_user
            await _log_new_user(user)
        except Exception as e:
            print(f"❌ new-user registration webhook error: {e}")

    asyncio.create_task(_fire())


@bot.check
async def global_ban_check(ctx: commands.Context) -> bool:
    # Guild-level ban check
    if ctx.guild and _is_guild_banned(ctx.guild.id):
        await ctx.reply("🚫 This server has been banned from using Jarvis.")
        return False

    if is_bot_banned(ctx.author.id):
        await ctx.reply("🚫 You are banned from using Jarvis.")
        await _notify_banned(ctx.author)
        return False

    # Terms & Conditions gate — applies even to the bot owner, so testing
    # the flow behaves the same for everyone. There's no standalone !tos
    # command; this is the only place the prompt appears. on_accept
    # re-dispatches the original message through the command processor once
    # they hit Accept, so the command they typed still runs instead of
    # silently vanishing.
    if not await ensure_tos(
        ctx.author.id,
        lambda **kw: ctx.reply(**kw),
        on_accept=lambda: bot.process_commands(ctx.message),
    ):
        return False

    # Register brand-new users on ANY command — !redeem is excluded because
    # it needs to see is_new_user() still True when its own handler runs, to
    # decide the onboarding bonus (see _handle_redeem in cogs/economy.py),
    # and registers itself.
    if not (ctx.command and ctx.command.name == "redeem"):
        _maybe_register_new_user(ctx.author)

    if await bot.is_owner(ctx.author):
        return True

    # Burst/timeout check
    allowed, t = check_burst_and_maybe_timeout(ctx.author.id)
    if not allowed:
        await ctx.reply(
            f"⏱️ You have been temporarily blocked from using Jarvis for {int(t)} seconds due to command flooding."
        )
        await _notify_banned(ctx.author)
        return False
    if not check_cooldown(ctx.author.id):
        try:
            await ctx.message.add_reaction("⏳")
        except discord.NotFound:
            # The user's message was deleted (by them, AutoMod, another
            # bot, etc.) in the moment between them sending it and this
            # check running — nothing to react to anymore, safe to ignore.
            pass
        except discord.Forbidden:
            # Bot lacks "Add Reactions" permission in this channel — same
            # deal, the cooldown itself still applies via check_cooldown()
            # above, we just can't visually signal it here.
            pass
        return False
    return True


async def slash_ban_check(interaction: discord.Interaction) -> bool:
    if is_bot_banned(interaction.user.id):
        await interaction.response.send_message("🚫 You are banned from using Jarvis.", ephemeral=True)
        await _notify_banned(interaction.user)
        return False
    return True

async def slash_interaction_check(interaction: discord.Interaction) -> bool:
    # Guild-level ban check
    if interaction.guild and _is_guild_banned(interaction.guild.id):
        await interaction.response.send_message(
            "🚫 This server has been banned from using Jarvis.", ephemeral=True
        )
        return False

    if not await slash_ban_check(interaction):
        return False

    # Terms & Conditions gate — applies even to the bot owner, so testing
    # the flow behaves the same for everyone. There's no standalone /tos
    # command; this is the only place the prompt appears.
    async def _send_tos(**kw):
        await interaction.response.send_message(**kw, ephemeral=True)
    if not await ensure_tos(interaction.user.id, _send_tos):
        return False

    # Register brand-new users on ANY slash command — /redeem excluded for
    # the same reason as !redeem above (see global_ban_check).
    if not (interaction.command and interaction.command.name == "redeem"):
        _maybe_register_new_user(interaction.user)

    if await bot.is_owner(interaction.user):
        return True

    allowed, t = check_burst_and_maybe_timeout(interaction.user.id)
    if not allowed:
        await interaction.response.send_message(
            f"⏱️ You have been temporarily blocked from using Jarvis for {int(t)} seconds due to command flooding.",
            ephemeral=True,
        )
        await _notify_banned(interaction.user)
        return False
    if not check_cooldown(interaction.user.id):
        await interaction.response.send_message("⏳", ephemeral=True)
        return False
    return True

bot.tree.interaction_check = slash_interaction_check 


@bot.event
async def on_ready():
    guild_count = len(bot.guilds)
    try:
        synced = await bot.tree.sync()
        print(
            f"✅ Jarvis online as {bot.user} | "
            f"Guilds: {guild_count} | "
            f"Synced {len(synced)} slash command(s)"
        )
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")

async def run_web_server() -> None:
    """Run the Jarvis stats/categories API in this same process.

    Sharing the process means the API's /api/stats route can read
    bot.guilds / seen_users directly — no network hop, no second deployment.
    The actual website (HTML/CSS/JS) is a SEPARATE project deployed on its
    own — it polls this API over HTTP. Failure here should never take the
    bot down, so errors are caught and logged.
    """
    try:
        app = create_app(bot)
        port = int(os.getenv("PORT", "8000"))
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()
    except Exception as e:
        print(f"❌ Web server failed to start: {e}")

async def _flush_and_exit(sig_name: str = "?") -> None:
    """SIGTERM/SIGINT handler: flush any pending debounced state saves to
    Turso before the process actually terminates, then exit immediately.

    Railway (and most container platforms) send SIGTERM on every
    redeploy/restart. Python's default SIGTERM handling kills the process
    right away without running pending asyncio tasks or `finally` blocks,
    so anything still sitting inside state.py's 2s debounce window would
    otherwise be silently lost — this is what was causing stats like
    Messages/Tokens/Last Active to intermittently revert after a restart.

    Logging which signal fired (and that this handler ran at all) is also
    the key piece of evidence for the Lavalink-drops-every-~3min issue:
    if this line is in logs/bot.log right around a drop, the host is
    killing/restarting the bot process itself (graceful SIGTERM caught
    here); if a drop happens with NO such line anywhere near it, the
    process is still alive and it's a pure network/proxy blip instead.
    """
    print(f"🛑 Shutdown signal received ({sig_name}) — flushing pending state before exit…")
    try:
        # Snapshot the !api dashboard's counters one last time so the final
        # ~60s (since the last periodic snapshot in cogs/system.py) isn't
        # lost — this just schedules the save, flush_all_saves() below
        # immediately fires it.
        from cogs.api_metrics import snapshot as _api_metrics_snapshot
        from cogs.state import set_api_metrics_snapshot
        set_api_metrics_snapshot(_api_metrics_snapshot())
    except Exception as e:
        print(f"❌ Error snapshotting api_metrics on shutdown: {e}")
    try:
        await flush_all_saves()
    except Exception as e:
        print(f"❌ Error flushing state on shutdown: {e}")
    try:
        # Lazy import — cogs.game is already loaded via load_extension() by
        # the time a shutdown signal can fire, so this just looks up the
        # already-imported module rather than re-running its module-level
        # code (unlike importing it eagerly at the top of this file, which
        # would double-init it the same way cogs.ai/cogs.system avoid above).
        from cogs.game import flush_all_counting_saves
        flush_all_counting_saves()
    except Exception as e:
        print(f"❌ Error flushing counting state on shutdown: {e}")
    os._exit(0)


def _install_signal_handlers() -> None:
    """Registered once at startup — replaces the default SIGTERM/SIGINT
    behaviour with the flush-then-exit sequence above."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(_flush_and_exit(s.name))
            )
        except (NotImplementedError, RuntimeError):
            # Not supported on some platforms (e.g. Windows for SIGTERM) —
            # falls back to default signal handling there.
            pass

async def main():
    # Validate token early for a clear error message
    if not TOKEN:
        print("❌ DISCORD_TOKEN is not set in your .env file. Exiting.")
        return

    _install_signal_handlers()

    MAX_RETRIES = 5
    BASE_DELAY  = 10    # seconds before first retry
    MAX_DELAY   = 300   # cap at 5 minutes

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with bot:
                await http_session.create_session()
                await init_db()
                await init_history()
                await init_memory()

                for cog in COGS:
                    try:
                        await bot.load_extension(cog)
                    except Exception as e:
                        print(f"❌ Failed to load cog '{cog}': {e}")

                ai_ext = bot.extensions.get("cogs.ai")
                if ai_ext is not None:
                    private_history = ai_ext.private_history
                    restored = await load_all_histories()
                    for uid, msgs in restored.items():
                        private_history[uid].extend(msgs)
                    if restored:
                        print(f"✅ Restored session history for {len(restored)} user(s)")
                else:
                    print("❌ cogs.ai failed to load — skipping session history restore.")
                
                try:
                    await asyncio.gather(
                        bot.start(TOKEN),
                        run_web_server(),
                    )
                finally:
                    await http_session.close_session()

            break  # clean exit

        except discord.errors.HTTPException as e:
            if e.status == 429:
                delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
                print(
                    f"⚠️  Discord rate-limited on login (attempt {attempt}/{MAX_RETRIES}). "
                    f"Waiting {delay}s before retrying…"
                )
                await asyncio.sleep(delay)
            else:
                raise

        except discord.errors.LoginFailure:
            print("❌ Invalid DISCORD_TOKEN — check your .env file. Not retrying.")
            break

        except Exception as e:
            if attempt < MAX_RETRIES:
                delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
                print(
                    f"❌ Unexpected error (attempt {attempt}/{MAX_RETRIES}): {e}\n"
                    f"   Retrying in {delay}s…"
                )
                await asyncio.sleep(delay)
            else:
                print(f"❌ Failed after {MAX_RETRIES} attempts. Giving up.")
                raise

    else:
        print(f"❌ Exhausted {MAX_RETRIES} login attempts. Exiting.")


if __name__ == "__main__":
    asyncio.run(main())
