"""
help_menu.py — Embed + dropdown help menus
===========================================

Two entry points, wired up from main.py via setup_help(bot, owner_id):

  -help       Every member. Shows the public commands (vanity time,
              quests). Mentions -adminhelp exists but doesn't reveal
              its contents.
  -adminhelp  Bot owner only. Shows every owner-only config/testing
              command. Silently refuses (same message on_command_error
              already uses) for anyone else.

Both render a "home" embed with one field per category, plus a select
menu (dropdown) that swaps the embed to a per-category command list —
same browsing pattern as J.A.R.V.I.S.'s existing admin menu, rebuilt
here for this bot's actual command set.
"""
import discord


# ---------------------------------------------------------------------------
# Command catalog
# ---------------------------------------------------------------------------
# Each category: emoji, label, one-line blurb (shown on the home embed),
# and a list of (usage, description) shown when the category is opened.

PUBLIC_CATEGORIES = [
    {
        "key": "vanity",
        "emoji": "🎉",
        "label": "Vanity & Rewards",
        "blurb": "Check your vanity time and reward cycle progress.",
        "commands": [
            ("-vanitytime [@user]",
             "Show today's vanity time, lifetime total, current 24h cycle "
             "progress, and time until the next reward. Defaults to you."),
        ],
    },
    {
        "key": "quests",
        "emoji": "📜",
        "label": "Quests",
        "blurb": "Daily quests and claiming your rewards.",
        "commands": [
            ("-quest [@user]",
             "Show ALL of today's quests at once, with each one's progress "
             "and claim status. Defaults to you."),
            ("-claimquest [quest_id]",
             "Claim quest reward(s). Omit the ID to claim every quest that's "
             "currently ready at once, or pass one (shown on -quest) to claim "
             "just that quest. Requires at least 2h of vanity time today."),
        ],
    },
]

ADMIN_CATEGORIES = [
    {
        "key": "vanity_setup",
        "emoji": "🎭",
        "label": "Vanity Setup",
        "blurb": "Configure the vanity text, role, log channel and tracked server.",
        "commands": [
            ("-setvanity <text>",
             "Set the vanity status text Jarvis watches for."),
            ("-setrole @role",
             "Set the role granted while a member's vanity is active."),
            ("-setlogchannel #channel",
             "Set the channel where vanity reward logs are posted."),
            ("-setguild",
             "Lock vanity tracking to the current server."),
            ("-vanityconfig",
             "View the current vanity config (text, role, channels, guild lock)."),
        ],
    },
    {
        "key": "testing",
        "emoji": "🧪",
        "label": "Testing & Data",
        "blurb": "Manually trigger rewards and manage stored vanity data.",
        "commands": [
            ("-testreward [@user] [amount]",
             "Manually fire the full reward flow (webhook + log embed + DM) "
             "without waiting for a real cycle. Doesn't touch real cycle "
             "progress — for testing only."),
            ("-resetvanitydata confirm",
             "Wipe every user's vanity time and cycle progress. Config is "
             "left untouched. Requires the literal word `confirm`."),
        ],
    },
    {
        "key": "quest_setup",
        "emoji": "📋",
        "label": "Quest Setup",
        "blurb": "Configure where quest completions and claims get logged.",
        "commands": [
            ("-setquestlogchannel #channel",
             "Set the channel where quest completions/claims are logged."),
            ("-questlogchannel",
             "View the current quest log channel."),
        ],
    },
]


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def _build_home_embed(title, description, categories, color, footer):
    embed = discord.Embed(title=title, description=description, color=color)
    for cat in categories:
        embed.add_field(
            name=f"{cat['emoji']} {cat['label']}",
            value=cat["blurb"],
            inline=True,
        )
    embed.set_footer(text=footer)
    return embed


def _build_category_embed(category, color, footer):
    embed = discord.Embed(
        title=f"{category['emoji']} {category['label']}",
        description=category["blurb"],
        color=color,
    )
    for usage, desc in category["commands"]:
        embed.add_field(name=f"`{usage}`", value=desc, inline=False)
    embed.set_footer(text=footer)
    return embed


# ---------------------------------------------------------------------------
# View / dropdown
# ---------------------------------------------------------------------------

class _CategorySelect(discord.ui.Select):
    def __init__(self, categories, color, footer, author_id):
        self.categories = {c["key"]: c for c in categories}
        self.color = color
        self.footer = footer
        self.author_id = author_id

        options = [
            discord.SelectOption(
                label="Home", description="Back to the overview", emoji="🏠", value="__home__",
            )
        ] + [
            discord.SelectOption(
                label=c["label"], description=c["blurb"][:100], emoji=c["emoji"], value=c["key"],
            )
            for c in categories
        ]
        super().__init__(placeholder="Choose a category...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't yours — run the command yourself to browse it.",
                ephemeral=True,
            )
            return

        choice = self.values[0]
        if choice == "__home__":
            embed = self.view.home_embed
        else:
            embed = _build_category_embed(self.categories[choice], self.color, self.footer)
        await interaction.response.edit_message(embed=embed)


class HelpView(discord.ui.View):
    def __init__(self, categories, home_embed, color, footer, author_id, timeout=120):
        super().__init__(timeout=timeout)
        self.home_embed = home_embed
        self.message = None  # set by the caller right after sending
        self.add_item(_CategorySelect(categories, color, footer, author_id))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Setup — registers -help and -adminhelp on the given bot
# ---------------------------------------------------------------------------

def setup_help(bot, owner_id: int):
    prefix = bot.command_prefix if isinstance(bot.command_prefix, str) else "-"

    @bot.command(name="help")
    async def help_cmd(ctx):
        """-help — lists every command available to members."""
        footer = f"Prefix: {prefix}  •  Bot owner: {prefix}adminhelp"
        embed = _build_home_embed(
            title="📖 J.A.R.V.I.S. Help",
            description="Here's what I can do. Pick a category below to see the commands.",
            categories=PUBLIC_CATEGORIES,
            color=discord.Color.blurple(),
            footer=footer,
        )
        view = HelpView(PUBLIC_CATEGORIES, embed, discord.Color.blurple(), footer, ctx.author.id)
        view.message = await ctx.send(embed=embed, view=view)

    @bot.command(name="adminhelp")
    async def adminhelp_cmd(ctx):
        """-adminhelp — owner-only. Lists owner config/testing commands."""
        if ctx.author.id != owner_id:
            await ctx.send("🚫 Only the bot owner can use this command.")
            return

        footer = f"Prefix: {prefix}  •  Bot owner only"
        embed = _build_home_embed(
            title="🛠️ Admin Commands",
            description=(
                "Owner-only config and testing utilities. These won't work "
                "for anyone but the bot owner."
            ),
            categories=ADMIN_CATEGORIES,
            color=discord.Color.red(),
            footer=footer,
        )
        view = HelpView(ADMIN_CATEGORIES, embed, discord.Color.red(), footer, ctx.author.id)
        view.message = await ctx.send(embed=embed, view=view)

    return help_cmd, adminhelp_cmd
