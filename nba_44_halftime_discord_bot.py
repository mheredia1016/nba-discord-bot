import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import aiohttp
import discord
from discord.ext import commands, tasks

# ---------------------------------
# Config via environment variables
# ---------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
BALLDONTLIE_API_KEY = os.getenv("BALLDONTLIE_API_KEY", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "45"))
EXACT_ONLY = os.getenv("EXACT_ONLY", "true").lower() == "true"

# Optional team filter, comma-separated abbreviations like: BOS,LAL,NYK
TEAM_FILTER = {
    team.strip().upper()
    for team in os.getenv("TEAM_FILTER", "").split(",")
    if team.strip()
}

API_URL = "https://api.balldontlie.io/v1/box_scores/live"
HEADERS = {"Authorization": BALLDONTLIE_API_KEY} if BALLDONTLIE_API_KEY else {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("nba_44_halftime_bot")


@dataclass
class PlayerHit:
    game_id: str
    player_id: str
    player_name: str
    team_abbr: str
    opponent_abbr: str
    assists: int
    rebounds: int
    points: int
    minutes: str
    game_status: str
    game_period: int
    game_clock: str
    matchup: str

    @property
    def dedupe_key(self) -> str:
        return f"{self.game_id}:{self.player_id}:halftime-4x4"


class HalftimeAlertBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None
        self.alerted: Set[str] = set()

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.poll_live_games.start()

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    @tasks.loop(seconds=POLL_SECONDS)
    async def poll_live_games(self) -> None:
        if not DISCORD_CHANNEL_ID:
            log.warning("DISCORD_CHANNEL_ID is not set. Skipping poll.")
            return
        if not BALLDONTLIE_API_KEY:
            log.warning("BALLDONTLIE_API_KEY is not set. Skipping poll.")
            return

        hits = await self.find_halftime_4x4_hits()
        if not hits:
            return

        channel = self.get_channel(DISCORD_CHANNEL_ID)
        if channel is None:
            log.warning("Channel %s not found in cache.", DISCORD_CHANNEL_ID)
            return

        for hit in hits:
            if hit.dedupe_key in self.alerted:
                continue
            self.alerted.add(hit.dedupe_key)
            message = self.format_alert(hit)
            try:
                await channel.send(message)
                log.info("Sent alert for %s", hit.dedupe_key)
            except Exception:
                log.exception("Failed to send alert for %s", hit.dedupe_key)

    @poll_live_games.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()

    async def fetch_live_box_scores(self) -> List[dict]:
        assert self.session is not None
        async with self.session.get(API_URL, headers=HEADERS) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"API error {resp.status}: {text[:400]}")
            payload = await resp.json()
        return payload.get("data", [])

    async def find_halftime_4x4_hits(self) -> List[PlayerHit]:
        try:
            games = await self.fetch_live_box_scores()
        except Exception:
            log.exception("Could not fetch live box scores")
            return []

        hits: List[PlayerHit] = []
        for game in games:
            if not self.is_halftime_window(game):
                continue

            home_team = game.get("home_team", {}) or {}
            away_team = game.get("visitor_team", {}) or {}
            home_abbr = home_team.get("abbreviation", "HOME")
            away_abbr = away_team.get("abbreviation", "AWAY")

            if TEAM_FILTER and home_abbr not in TEAM_FILTER and away_abbr not in TEAM_FILTER:
                continue

            matchup = f"{away_abbr} @ {home_abbr}"
            game_id = str(game.get("id", matchup))
            status = str(game.get("status", ""))
            period = int(game.get("period") or 0)
            clock = str(game.get("time", ""))

            for side, opponent in ((home_team, away_abbr), (away_team, home_abbr)):
                team_abbr = side.get("abbreviation", "UNK")
                for player in side.get("players", []) or []:
                    ast = int(player.get("ast") or 0)
                    reb = int(player.get("reb") or 0)

                    matched = (ast == 4 and reb == 4) if EXACT_ONLY else (ast >= 4 and reb >= 4)
                    if not matched:
                        continue

                    player_info = player.get("player", {}) or {}
                    player_id = str(player_info.get("id", player.get("id", "unknown")))
                    player_name = (
                        f"{player_info.get('first_name', '').strip()} {player_info.get('last_name', '').strip()}"
                    ).strip() or "Unknown Player"

                    hits.append(
                        PlayerHit(
                            game_id=game_id,
                            player_id=player_id,
                            player_name=player_name,
                            team_abbr=team_abbr,
                            opponent_abbr=opponent,
                            assists=ast,
                            rebounds=reb,
                            points=int(player.get("pts") or 0),
                            minutes=str(player.get("min") or "0"),
                            game_status=status,
                            game_period=period,
                            game_clock=clock,
                            matchup=matchup,
                        )
                    )

        return hits

    @staticmethod
    def is_halftime_window(game: dict) -> bool:
        """
        Treat these as the halftime check window:
        - explicit halftime/intermission status strings
        - end of 2nd quarter / halftime clock text
        - start of 3rd quarter as a fallback, using cumulative stats
        """
        status = str(game.get("status", "")).lower()
        clock = str(game.get("time", "")).lower()
        period = int(game.get("period") or 0)

        halftime_terms = ("halftime", "half", "intermission")
        if any(term in status for term in halftime_terms):
            return True
        if any(term in clock for term in halftime_terms):
            return True

        # common fallback: game has rolled into Q3 and cumulative stats still reflect halftime snapshot timing
        return period in {2, 3}

    @staticmethod
    def format_alert(hit: PlayerHit) -> str:
        return (
            "🚨 **Halftime 4x4 Alert** 🚨\n"
            f"**{hit.player_name}** ({hit.team_abbr}) has **{hit.assists} AST** and **{hit.rebounds} REB** by halftime.\n"
            f"Matchup: **{hit.matchup}**\n"
            f"Points: **{hit.points}** | Minutes: **{hit.minutes}**\n"
            f"Game status: **{hit.game_status}** | Period: **{hit.game_period}** | Clock: **{hit.game_clock}**"
        )


bot = HalftimeAlertBot()


@bot.command()
async def ping(ctx: commands.Context) -> None:
    await ctx.send("pong")


@bot.command()
async def health(ctx: commands.Context) -> None:
    await ctx.send(
        f"Watching live NBA box scores every {POLL_SECONDS}s. "
        f"Exact-only mode: {EXACT_ONLY}. Team filter: {', '.join(sorted(TEAM_FILTER)) or 'none'}."
    )


if __name__ == "__main__":
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not DISCORD_CHANNEL_ID:
        missing.append("DISCORD_CHANNEL_ID")
    if not BALLDONTLIE_API_KEY:
        missing.append("BALLDONTLIE_API_KEY")

    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

    bot.run(DISCORD_TOKEN)
