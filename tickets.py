"""
tickets.py — Ticket claiming (Ticket Tool's premium "claim" feature, free)
============================================================================

Ticket Tool gates the "claim" button behind premium — this replaces it.
A configurable set of permissions (see TICKET_PERM_OPTIONS /
/ticketclaimperms) gets revoked for the "ticket staff" role in the
current channel the moment someone on that role runs /claimticket; the
claimer gets those same permissions explicitly re-allowed on a per-member
overwrite, which always beats a role-level one in Discord's permission
resolution — so they alone keep access to whatever was picked. Nobody
else on the staff role gets that access back in that ticket until the
claimer runs /unclaimticket (or another staff member re-claims it).

Only the fields in the configured perm list are ever touched — anything
else already set on the role's or a member's overwrite (e.g. view access
set up by Ticket Tool) is read, copied, and written back untouched, so
claiming/unclaiming never wipes out permissions this feature doesn't
manage.

Wired up from main.py via setup(bot, owner_id); load_config_from_db()
is called once at startup (same pattern as vanity.py/automod.py).
"""
import discord
from discord import app_commands
from discord.ext import commands

import db
import emoji

# Curated, ticket-channel-relevant subset of Discord's full permission
# list — shown in the /ticketclaimperms picker. (label, value, description)
TICKET_PERM_OPTIONS = [
    ("Send Messages", "send_messages", "Type in the ticket at all"),
    ("View Channel", "view_channel", "See the ticket channel exists"),
    ("Read Message History", "read_message_history", "Scroll back through past messages"),
    ("Send Messages in Threads", "send_messages_in_threads", "Reply inside threads on the ticket"),
    ("Create Public Threads", "create_public_threads", "Start public threads here"),
    ("Create Private Threads", "create_private_threads", "Start private threads here"),
    ("Add Reactions", "add_reactions", "React to messages"),
    ("Attach Files", "attach_files", "Upload files/images"),
    ("Embed Links", "embed_links", "Post embeds/link previews"),
    ("Use Application Commands", "use_application_commands", "Run slash commands in the ticket"),
    ("Manage Messages", "manage_messages", "Delete/pin others' messages"),
    ("Mention Everyone", "mention_everyone", "Use @everyone/@here"),
]
TICKET_PERM_VALUES = {value for _, value, _ in TICKET_PERM_OPTIONS}

# In-memory cache of config (staff role + which perms claiming controls),
# loaded from the shared `config` table at startup — same pattern as
# automod.py.
DEFAULT_CONFIG = {"ticket_staff_role_id": "0", "ticket_claim_perms": "send_messages"}
config = dict(DEFAULT_CONFIG)


def cfg_int(key: str) -> int:
    try:
        return int(config.get(key, 0))
    except (TypeError, ValueError):
        return 0


def _configured_perms() -> list[str]:
    raw = config.get("ticket_claim_perms", DEFAULT_CONFIG["ticket_claim_perms"])
    perms = [p.strip() for p in raw.split(",") if p.strip() in TICKET_PERM_VALUES]
    return perms or ["send_messages"]


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


def _perm_list_text(perms: list[str]) -> str:
    labels = {value: label for label, value, _ in TICKET_PERM_OPTIONS}
    return ", ".join(f"`{labels.get(p, p)}`" for p in perms)


class _ClaimPermsSelect(discord.ui.Select):
    def __init__(self, current: list[str]):
        options = [
            discord.SelectOption(label=label, value=value, description=desc, default=(value in current))
            for label, value, desc in TICKET_PERM_OPTIONS
        ]
        super().__init__(
            placeholder="Choose which permissions /claimticket controls...",
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values or ["send_messages"]
        config["ticket_claim_perms"] = ",".join(chosen)
        await db.set_config("ticket_claim_perms", ",".join(chosen))
        await interaction.response.edit_message(
            content=f"{emoji.SUCCESS} `/claimticket` will now control: {_perm_list_text(chosen)}",
            view=None,
        )


class _ClaimPermsView(discord.ui.View):
    def __init__(self, owner_id: int, current: list[str]):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.add_item(_ClaimPermsSelect(current))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"{emoji.DENIED} Only the bot owner can change this.", ephemeral=True
            )
            return False
        return True


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
        permissions get revoked in a channel once someone with that role
        runs /claimticket there."""
        config["ticket_staff_role_id"] = str(role.id)
        await db.set_config("ticket_staff_role_id", role.id)
        await ctx.send(f"{emoji.SUCCESS} Ticket staff role set to {role.mention}.")

    @bot.command(name="ticketclaimperms")
    @is_owner()
    async def ticketclaimperms(ctx):
        """-ticketclaimperms — owner-only. Opens a picker of which
        permissions get revoked from the staff role (and kept for the
        claimer alone) when a ticket is claimed."""
        view = _ClaimPermsView(owner_id, _configured_perms())
        await ctx.send(
            f"{emoji.CONFIG} Pick which permissions `/claimticket` should control "
            f"(currently: {_perm_list_text(_configured_perms())}):",
            view=view,
        )

    @bot.command(name="ticketclaimconfig")
    @is_owner()
    async def ticketclaimconfig(ctx):
        role = _staff_role(ctx.guild)
        embed = discord.Embed(title=f"{emoji.CONFIG} Ticket Claim Config", color=discord.Color.blurple())
        embed.add_field(name="Ticket Staff Role", value=role.mention if role else "Not set", inline=False)
        embed.add_field(name="Controlled Permissions", value=_perm_list_text(_configured_perms()), inline=False)
        await ctx.send(embed=embed)

    # -----------------------------------------------------------------
    # /claimticket and /unclaimticket
    # -----------------------------------------------------------------

    @bot.tree.command(name="claimticket", description="Claim this ticket — other staff lose access to the configured permissions until you unclaim it.")
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

        perms = _configured_perms()
        try:
            # Only touch the configured permission fields on whatever
            # overwrite the role already has — anything else (e.g. view
            # access set by Ticket Tool, if not itself in the list) is
            # read, kept, and written back untouched.
            role_ow = channel.overwrites_for(role)
            for perm in perms:
                setattr(role_ow, perm, False)
            await channel.set_permissions(
                role, overwrite=role_ow, reason=f"Ticket claimed by {member} ({member.id})"
            )
            # Explicitly re-allow just the claimer on those same fields
            # (member overwrites win over role overwrites), on top of
            # whatever overwrite they may already have.
            member_ow = channel.overwrites_for(member)
            for perm in perms:
                setattr(member_ow, perm, True)
            await channel.set_permissions(
                member, overwrite=member_ow, reason=f"Ticket claimed by {member} ({member.id})"
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

    @bot.tree.command(name="unclaimticket", description="Release this ticket, restoring the ticket staff role's normal access.")
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
        perms = _configured_perms()
        try:
            if role is not None:
                # Clear only the fields we set on claim, leaving any
                # other permissions on this overwrite exactly as they
                # already were. If that leaves nothing set at all, drop
                # the overwrite entirely so it doesn't linger empty.
                role_ow = channel.overwrites_for(role)
                for perm in perms:
                    setattr(role_ow, perm, None)
                await channel.set_permissions(
                    role,
                    overwrite=None if role_ow.is_empty() else role_ow,
                    reason=f"Ticket unclaimed by {member}",
                )
            claimer = interaction.guild.get_member(existing["claimed_by"])
            if claimer is not None:
                claimer_ow = channel.overwrites_for(claimer)
                for perm in perms:
                    setattr(claimer_ow, perm, None)
                await channel.set_permissions(
                    claimer,
                    overwrite=None if claimer_ow.is_empty() else claimer_ow,
                    reason=f"Ticket unclaimed by {member}",
                )
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
