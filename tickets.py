"""
tickets.py — Ticket claiming (Ticket Tool's premium "claim" feature, free)
============================================================================

Ticket Tool gates the "claim" button behind premium — this replaces it.
One configured "ticket staff" role gets its ability to SEND MESSAGES in
the current channel revoked the moment someone on that role runs
/claimticket; the claimer gets an explicit per-member override that lets
them keep typing (a member-level overwrite always beats a role-level one
in Discord's permission resolution, so this works even though the role
itself is now denied). Nobody else on the staff role can type in that
ticket until the claimer runs /unclaimticket (or another staff member
re-claims it), which restores the role's normal access.

This only touches SendMessages — everyone with the staff role can still
see and read the channel, they just can't reply until it's unclaimed.

Wired up from main.py via setup(bot, owner_id); load_config_from_db()
is called once at startup (same pattern as vanity.py/automod.py).
"""
import time

import discord
from discord import app_commands
from discord.ext import commands

import db
import emoji

# In-memory cache of config (which role is "ticket staff"), loaded from
# the shared `config` table at startup — same pattern as automod.py.
DEFAULT_CONFIG = {"ticket_staff_role_id": "0"}
config = dict(DEFAULT_CONFIG)


def cfg_int(key: str) -> int:
    try:
        return int(config.get(key, 0))
    except (TypeError, ValueError):
        return 0


async def load_config_from_db():
    stored = await db.get_all_config()
    for key in DEFAULT_CONFIG:
        if key in stored:
            config[key] = stored[key]


def _staff_role(guild: discord.Guild) -> discord.Role | None:
    role_id = cfg_int("ticket_staff_role_id")
    return guild.get_role(role_id) if role_id else None


def _format_claimed_since(claimed_at: float) -> str:
    return f"<t:{int(claimed_at)}:R>"


def setup(bot, owner_id: int):
    def is_owner():
        async def predicate(ctx):
            return ctx.author.id == owner_id
        return commands.check(predicate)

    # -----------------------------------------------------------------
    # Owner-only config
    # -----------------------------------------------------------------

    @bot.command(name="setticketstaffrole")
    @is_owner()
    async def setticketstaffrole(ctx, role: discord.Role):
        """-setticketstaffrole @role — owner-only. Sets the role whose
        send-message access gets revoked in a channel once someone with
        that role runs /claimticket there."""
        config["ticket_staff_role_id"] = str(role.id)
        await db.set_config("ticket_staff_role_id", role.id)
        await ctx.send(f"{emoji.SUCCESS} Ticket staff role set to {role.mention}.")

    @bot.command(name="ticketclaimconfig")
    @is_owner()
    async def ticketclaimconfig(ctx):
        role = _staff_role(ctx.guild)
        embed = discord.Embed(title=f"{emoji.CONFIG} Ticket Claim Config", color=discord.Color.blurple())
        embed.add_field(name="Ticket Staff Role", value=role.mention if role else "Not set", inline=False)
        await ctx.send(embed=embed)

    # -----------------------------------------------------------------
    # /claimticket and /unclaimticket
    # -----------------------------------------------------------------

    @bot.tree.command(name="claimticket", description="Claim this ticket — other staff lose send access until you unclaim it.")
    async def claimticket(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(f"{emoji.WARNING} This only works in a server.", ephemeral=True)
            return

        channel = interaction.channel
        member = interaction.user

        role = _staff_role(interaction.guild)
        if role is None:
            await interaction.response.send_message(
                f"{emoji.WARNING} No ticket staff role is configured yet — an admin needs to run "
                f"`-setticketstaffrole @role` first.",
                ephemeral=True,
            )
            return

        if role not in member.roles and not member.guild_permissions.administrator:
            await interaction.response.send_message(
                f"{emoji.DENIED} Only {role.mention} (or an admin) can claim tickets.", ephemeral=True
            )
            return

        existing = await db.get_ticket_claim(channel.id)
        if existing is not None:
            if existing["claimed_by"] == member.id:
                await interaction.response.send_message(
                    f"{emoji.WARNING} You've already claimed this ticket.", ephemeral=True
                )
                return
            claimer = interaction.guild.get_member(existing["claimed_by"])
            claimer_mention = claimer.mention if claimer else f"<@{existing['claimed_by']}>"
            await interaction.response.send_message(
                f"{emoji.WARNING} This ticket is already claimed by {claimer_mention} "
                f"({_format_claimed_since(existing['claimed_at'])}). They (or an admin) need to "
                f"`/unclaimticket` before it can be re-claimed.",
                ephemeral=True,
            )
            return

        try:
            # Deny the whole staff role from sending in this channel...
            await channel.set_permissions(
                role,
                send_messages=False,
                reason=f"Ticket claimed by {member} ({member.id})",
            )
            # ...then explicitly re-allow just the claimer, which overrides
            # the role-level deny above since member overwrites win.
            await channel.set_permissions(
                member,
                send_messages=True,
                view_channel=True,
                reason=f"Ticket claimed by {member} ({member.id})",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"{emoji.ERROR} I don't have permission to edit this channel's permissions "
                f"(need **Manage Channel**/**Manage Permissions**).",
                ephemeral=True,
            )
            return

        await db.claim_ticket(channel.id, member.id)

        embed = discord.Embed(
            description=f"{emoji.LOCKED} Ticket claimed by {member.mention}.",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="unclaimticket", description="Release this ticket, restoring the ticket staff role's send access.")
    async def unclaimticket(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(f"{emoji.WARNING} This only works in a server.", ephemeral=True)
            return

        channel = interaction.channel
        member = interaction.user

        existing = await db.get_ticket_claim(channel.id)
        if existing is None:
            await interaction.response.send_message(f"{emoji.WARNING} This ticket isn't claimed.", ephemeral=True)
            return

        if existing["claimed_by"] != member.id and not member.guild_permissions.administrator:
            claimer = interaction.guild.get_member(existing["claimed_by"])
            claimer_mention = claimer.mention if claimer else f"<@{existing['claimed_by']}>"
            await interaction.response.send_message(
                f"{emoji.DENIED} Only {claimer_mention} (or an admin) can unclaim this ticket.", ephemeral=True
            )
            return

        role = _staff_role(interaction.guild)
        try:
            if role is not None:
                # Remove the role-level deny entirely (back to inheriting
                # whatever the category/channel normally grants) rather
                # than flipping it to an explicit allow, so it stays in
                # sync with however the staff role's access is set up
                # elsewhere.
                await channel.set_permissions(role, overwrite=None, reason=f"Ticket unclaimed by {member}")
            claimer = interaction.guild.get_member(existing["claimed_by"])
            if claimer is not None:
                await channel.set_permissions(claimer, overwrite=None, reason=f"Ticket unclaimed by {member}")
        except discord.Forbidden:
            await interaction.response.send_message(
                f"{emoji.ERROR} I don't have permission to edit this channel's permissions.",
                ephemeral=True,
            )
            return

        await db.unclaim_ticket(channel.id)

        embed = discord.Embed(
            description=f"{emoji.SUCCESS} Ticket unclaimed by {member.mention}.",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)
