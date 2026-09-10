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
SUCCESS   = "<a:tick:1547570094186561576>"   # something worked
ERROR     = "<a:error:1547577406037172355>"   # something failed / was removed
WARNING   = "⚠️"   # non-fatal problem, bad input, delivery failure
DENIED    = "<a:error:1547577406037172355>"   # permission check failed (owner-only commands)
DELETE    = "🗑️"   # destructive action confirmation (resetvanitydata)
CONFIG    = "⚙️"   # config/settings display
LOCKED    = "🔒"   # gated behind a requirement (vanity time, etc.)
PENDING   = "<:pending:1547574726468042904>"   # still in progress, not done yet
TEST      = "🧪"   # test/debug commands
CELEBRATE = "<a:giveaway:1547569452470632458>"   # rewards, claims, milestones
GIFT      = "<a:gifts:1547574389338415185>"   # anything reward/box related
USER      = "<a:users:1547575226148200552>"   # "user" field label in log embeds

# ---------------------------------------------------------------------------
# Currency — Jarvis Credits (JC), the bot's single reward currency
# ---------------------------------------------------------------------------
JC = "<a:coinn:1547572413808779264>"
JC_NAME = "Jarvis Credit"

# ---------------------------------------------------------------------------
# Vanity system
# ---------------------------------------------------------------------------
ROLE_GIVEN       = "🎖️"
SESSION_DURATION = "<a:clock_new:1547584940814770236>"

# ---------------------------------------------------------------------------
# Quests
# ---------------------------------------------------------------------------
QUEST_LIST = "<a:diamond_black:1547569545726926990>"   # -quest / quest log channel headers
QUEST_TYPE = "🌸"   # "quest" field label in the claim log embed

# ---------------------------------------------------------------------------
# Help menu (-help / -adminhelp)
# ---------------------------------------------------------------------------
HELP_BOOK        = "<a:diamond_black:1547569545726926990>"   # -help title
HELP_TOOLS       = "🛠️"   # -adminhelp title
HELP_HOME        = "🏠"   # dropdown "back to overview" option
CAT_VANITY       = "<a:giveaway:1547569452470632458>"   # "Vanity & Rewards" category
CAT_QUESTS       = "<a:target:1547585169060405368>"   # "Quests" category
CAT_VANITY_SETUP = "🎭"   # "Vanity Setup" admin category
CAT_TESTING      = "🧪"   # "Testing & Data" admin category
CAT_QUEST_SETUP  = "📋"   # "Quest Setup" admin category
