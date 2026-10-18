"""
Replaces Modmail's `logs search` with a version that:

- returns the newest matching logs first,
- accepts an optional date cutoff (`--since 30d`, `--since 2026-09-01`),
- can require every word to appear somewhere in the log (`--all`)
  instead of only matching the exact phrase.

Without any flags the command behaves like the built-in one (exact phrase
match), apart from the newest-first ordering.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel, getLogger
from core.paginator import EmbedPaginatorSession
from core.utils import safe_typing

logger = getLogger(__name__)

SINCE_RE = re.compile(r"(?:^|\s)(?:--since|-s)(?:\s+|=)(\S+)", re.IGNORECASE)
ALL_RE = re.compile(r"(?:^|\s)(?:--all|-a)(?=\s|$)", re.IGNORECASE)
RELATIVE_RE = re.compile(r"^(\d+)\s*(d|days?|w|weeks?|m|mo|months?|y|years?)$", re.IGNORECASE)
TERM_RE = re.compile(r'"([^"]+)"|(\S+)')

HELP = """
Retrieve all logs that contain messages with your query, newest first.

Provide a `limit` to specify the maximum number of logs the bot should find.

**Options** (can go anywhere in the query):
- `--all` / `-a`: match logs containing *every* word, not the exact phrase. Wrap words in quotes to keep a phrase together.
- `--since <when>` / `-s <when>`: only logs created on or after a date. Use a relative age (`7d`, `2w`, `3mo`, `1y`) or a date (`2026-09-01`).

**Examples:**
`{prefix}logs search concert ticket` - exact phrase "concert ticket"
`{prefix}logs search --all concert ticket` - both words, anywhere in the log
`{prefix}logs search 20 --since 30d --all concert ticket`
`{prefix}logs search -a -s 2026-09-01 "concert ticket" refund`
"""


class SearchError(Exception):
    pass


def parse_since(value: str) -> datetime:
    value = value.strip()
    match = RELATIVE_RE.match(value)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("d"):
            delta = timedelta(days=amount)
        elif unit.startswith("w"):
            delta = timedelta(weeks=amount)
        elif unit.startswith("m"):
            delta = timedelta(days=30 * amount)
        else:
            delta = timedelta(days=365 * amount)
        return datetime.now(timezone.utc) - delta
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SearchError(
            f"Couldn't understand `--since {value}`. Use a relative age like `7d`, `2w`, `3mo`, `1y`, "
            "or a date like `2026-09-01`."
        )


def parse_query(raw: str):
    """Returns (text_search_string, since_datetime_or_None, match_all, display_terms)."""
    since = None
    since_match = SINCE_RE.search(raw)
    if since_match:
        since = parse_since(since_match.group(1))
        raw = raw[: since_match.start()] + " " + raw[since_match.end() :]
        if SINCE_RE.search(raw):
            raise SearchError("`--since` can only be given once.")

    match_all = bool(ALL_RE.search(raw))
    raw = ALL_RE.sub(" ", raw).strip()

    if not raw:
        raise SearchError("You need to give something to search for.")

    if match_all:
        terms = [(m.group(1) or m.group(2)).replace('"', "").strip() for m in TERM_RE.finditer(raw)]
        terms = [t for t in terms if t]
        if not terms:
            raise SearchError("You need to give something to search for.")
        # MongoDB ANDs multiple quoted phrases in a $text search.
        search = " ".join(f'"{t}"' for t in terms)
        return search, since, True, terms

    # Same as the built-in command: one exact phrase.
    return f'"{raw}"', since, False, [raw]


class LogsSearch(commands.Cog):
    """Newest-first `logs search` with date cutoffs and match-all-words."""

    def __init__(self, bot):
        self.bot = bot
        self._original = None
        self._command = None

    async def cog_load(self):
        logs = self.bot.get_command("logs")
        if not isinstance(logs, commands.Group):
            raise RuntimeError("logs_search: the built-in `logs` command group wasn't found.")

        self._original = logs.get_command("search")
        if self._original is not None:
            logs.remove_command(self._original.name)

        command = commands.Command(
            LogsSearch._search_command,
            name="search",
            aliases=["find"],
            help=HELP.strip(),
            usage="[limit] [--all] [--since <when>] <query>",
        )
        command.cog = self
        logs.add_command(command)
        self._command = command
        logger.info("logs_search: replaced the built-in `logs search` command.")

    async def cog_unload(self):
        logs = self.bot.get_command("logs")
        if not isinstance(logs, commands.Group):
            return
        if self._command is not None:
            logs.remove_command(self._command.name)
        if self._original is not None:
            logs.add_command(self._original)
            logger.info("logs_search: restored the built-in `logs search` command.")

    async def search(self, search: str, since: Optional[datetime], limit: Optional[int]):
        filters = {
            "guild_id": str(self.bot.guild_id),
            "open": False,
            "$text": {"$search": search},
        }
        if since is not None:
            # created_at is stored as a UTC timestamp string ("YYYY-MM-DD ...") so a
            # date-only prefix compares correctly regardless of the time format.
            filters["created_at"] = {"$gte": since.strftime("%Y-%m-%d")}

        cursor = self.bot.db.logs.find(filters, {"messages": {"$slice": 5}}).sort("created_at", -1)
        if limit:
            cursor = cursor.limit(limit)
        return await cursor.to_list(None)

    # Not decorated with @commands.command so the cog doesn't register it as a
    # top-level command; cog_load attaches it to the `logs` group instead.
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def _search_command(self, ctx, limit: Optional[int] = None, *, query: str):
        try:
            search, since, match_all, terms = parse_query(query)
        except SearchError as e:
            embed = discord.Embed(color=self.bot.error_color, description=str(e))
            return await ctx.send(embed=embed)

        async with safe_typing(ctx):
            pass

        entries = await self.search(search, since, limit)

        modmail = self.bot.get_cog("Modmail")
        embeds = modmail.format_log_embeds(entries, avatar_url=self.bot.get_guild_icon(guild=ctx.guild))

        if not embeds:
            what = (
                "containing all of " + ", ".join(f"`{t}`" for t in terms)
                if match_all
                else f"containing `{terms[0]}`"
            )
            when = f" since {since.strftime('%Y-%m-%d')}" if since else ""
            hint = "" if match_all else "\nTip: add `--all` to match the words separately instead of as a phrase."
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"No log entries have been found {what}{when}.{hint}",
            )
            return await ctx.send(embed=embed)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()


async def setup(bot):
    await bot.add_cog(LogsSearch(bot))
