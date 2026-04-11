import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Set

import aiohttp
from nba_api.live.nba.endpoints import scoreboard
import discord
from discord.ext import commands, tasks

# ---------------------------------
# Config via environment variables
# ---------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "45"))
EXACT_ONLY = os.getenv("EXACT_ONLY", "true").lower() == "true"

# Optional team filter, comma-separated abbreviations like: BOS,LAL,NYK
TEAM_FILTER = {
    team.strip().upper()
    for team in os.getenv("TEAM_FILTER", "").split(",")
    if team.strip()
}

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

        hits = await self.find_halftime_4x4_hits()
        if not hits:
            return

        try:
            channel = await self.fetch_channel(DISCORD_CHANNEL_ID)
        except Exception:
            log.exception("Channel %s could not be fetched.", DISCORD_CHANNEL_ID)
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

    async def fetch_live_box_score_json(self, game_id: str) -> dict:
        assert self.session is not None
        url = f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json"
        async with self.session.get(url) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Live boxscore API error {resp.status}: {text[:300]}")
            return await resp.json()

    async def fetch_live_scoreboard_games(self) -> List[dict]:
        board = scoreboard.ScoreBoard()
        games = board.get_dict().get("scoreboard", {}).get("games", [])
        return games

    async def find_halftime_4x4_hits(self) -> List[PlayerHit]:
        try:
            games = await self.fetch_live_scoreboard_games()
        except Exception:
            log.exception("Could not fetch live scoreboard")
            return []

        candidate_games = [g for g in games if self.is_halftime_window(g)]
        if not candidate_games:
            return []

        hits: List[PlayerHit] = []

        for game in candidate_games:
            game_id = str(game.get("gameId", ""))
            if not game_id:
                continue

            home_team = game.get("homeTeam", {}) or {}
            away_team = game.get("awayTeam", {}) or {}
            home_abbr = (home_team.get("teamTricode") or home_team.get("teamCode") or "HOME").upper()
            away_abbr = (away_team.get("teamTricode") or away_team.get("teamCode") or "AWAY").upper()

            if TEAM_FILTER and home_abbr not in TEAM_FILTER and away_abbr not in TEAM_FILTER:
                continue

            matchup = f"{away_abbr} @ {home_abbr}"
            period = int(game.get("period", 0) or 0)
            status_text = str(game.get("gameStatusText", "") or "")
            clock = str(game.get("gameClock", "") or "")

            try:
                box = await self.fetch_live_box_score_json(game_id)
            except Exception:
                log.exception("Could not fetch live box score for game %s", game_id)
                continue

            game_data = box.get("game", {}) or {}
            home = game_data.get("homeTeam", {}) or {}
            away = game_data.get("awayTeam", {}) or {}

            for side, team_abbr, opponent_abbr in (
                (home, home_abbr, away_abbr),
                (away, away_abbr, home_abbr),
            ):
                for player in side.get("players", []) or []:
                    stats = player.get("statistics", {}) or {}
                    ast = int(stats.get("assists", 0) or 0)
                    reb = int(stats.get("reboundsTotal", 0) or 0)

                    matched = (ast >= 4 and reb >= 4) if EXACT_ONLY else (ast >= 4 and reb >= 4)
                    if not matched:
                        continue

                    first = str(player.get("firstName", "") or "").strip()
                    last = str(player.get("familyName", "") or "").strip()
                    player_name = f"{first} {last}".strip() or "Unknown Player"

                    hits.append(
                        PlayerHit(
                            game_id=game_id,
                            player_id=str(player.get("personId", "unknown")),
                            player_name=player_name,
                            team_abbr=team_abbr,
                            opponent_abbr=opponent_abbr,
                            assists=ast,
                            rebounds=reb,
                            points=int(stats.get("points", 0) or 0),
                            minutes=str(stats.get("minutes", "") or "0"),
                            game_status=status_text,
                            game_period=period,
                            game_clock=clock,
                            matchup=matchup,
                        )
                    )

        return hits

    @staticmethod
    def is_halftime_window(game: dict) -> bool:
        status_text = str(game.get("gameStatusText", "") or "").lower()
        period = int(game.get("period", 0) or 0)

        halftime_terms = ("halftime", "half", "intermission", "end of 2nd quarter")
        if any(term in status_text for term in halftime_terms):
            return True

        # Fallback: start of Q3 still close enough for a halftime-style check.
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
        f"Watching live NBA games every {POLL_SECONDS}s. "
        f"Exact-only mode: {EXACT_ONLY}. Team filter: {', '.join(sorted(TEAM_FILTER)) or 'none'}."
    )


if __name__ == "__main__":
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not DISCORD_CHANNEL_ID:
        missing.append("DISCORD_CHANNEL_ID")

    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

    bot.run(DISCORD_TOKEN)
