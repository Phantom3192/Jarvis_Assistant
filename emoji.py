"""
emoji.py — single source of truth for every emoji the bot uses
=================================================================

Every embed/message across the bot should pull its emoji from here
instead of hardcoding a literal. That way the bot's whole "look" (and
any future rebrand/reskin) is a one-file edit instead of a grep-and-
replace across main.py, quests.py, boxes.py, vanity.py and help_menu.py.

Grouped by where they're used — feel free to add more as new features
show up, just keep each constant named for its *meaning*, not its
glyph (e.g. SUCCESS, not CHECKMARK), so swapping the actual emoji later
doesn't leave a misleading name behind.
"""

# ---------------------------------------------------------------------------
# Generic status / feedback — used everywhere for command responses
# ---------------------------------------------------------------------------
SUCCESS   = "✅"   # something worked
ERROR     = "❌"   # something failed / was removed
WARNING   = "⚠️"   # non-fatal problem, bad input, delivery failure
DENIED    = "🚫"   # permission check failed (owner-only commands)
DELETE    = "🗑️"   # destructive action confirmation (resetvanitydata)
CONFIG    = "⚙️"   # config/settings display
LOCKED    = "🔒"   # gated behind a requirement (vanity time, etc.)
PENDING   = "⏳"   # still in progress, not done yet
TEST      = "🧪"   # test/debug commands
CELEBRATE = "🎉"   # rewards, claims, milestones
GIFT      = "🎁"   # anything reward/box related
USER      = "👥"   # "user" field label in log embeds

# ---------------------------------------------------------------------------
# Currency — Jarvis Credits (JC), the bot's single reward currency
# ---------------------------------------------------------------------------
JC = "🪙"
JC_NAME = "Jarvis Credit"

# ---------------------------------------------------------------------------
# Vanity system
# ---------------------------------------------------------------------------
ROLE_GIVEN       = "🎖️"
SESSION_DURATION = "⏱️"

# ---------------------------------------------------------------------------
# Quests
# ---------------------------------------------------------------------------
QUEST_LIST = "📋"   # -quest / quest log channel headers
QUEST_TYPE = "🌸"   # "quest" field label in the claim log embed

# ---------------------------------------------------------------------------
# Help menu (-help / -adminhelp)
# ---------------------------------------------------------------------------
HELP_BOOK        = "📖"   # -help title
HELP_TOOLS       = "🛠️"   # -adminhelp title
HELP_HOME        = "🏠"   # dropdown "back to overview" option
CAT_VANITY       = "🎉"   # "Vanity & Rewards" category
CAT_QUESTS       = "📜"   # "Quests" category
CAT_VANITY_SETUP = "🎭"   # "Vanity Setup" admin category
CAT_TESTING      = "🧪"   # "Testing & Data" admin category
CAT_QUEST_SETUP  = "📋"   # "Quest Setup" admin category
