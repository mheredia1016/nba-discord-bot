import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Set

import aiohttp
from nba_api.live.nba.endpoints import scoreboard
import discord
from discord.ext import commands, tasks

TEAM_COLORS = {
    "ATL": 0xE03A3E,
    "BOS": 0x007A33,
    "BKN": 0x000000,
    "CHA": 0x1D1160,
    "CHI": 0xCE1141,
    "CLE": 0x860038,
    "DAL": 0x00538C,
    "DEN": 0x0E2240,
    "DET": 0xC8102E,
    "GSW": 0x1D428A,
    "HOU": 0xCE1141,
    "IND": 0x002D62,
    "LAC": 0xC8102E,
    "LAL": 0x552583,
    "MEM": 0x5D76A9,
    "MIA": 0x98002E,
    "MIL": 0x00471B,
    "MIN": 0x0C2340,
    "NOP": 0x0C2340,
    "NYK": 0x006BB6,
    "OKC": 0x007AC1,
    "ORL": 0x0077C0,
    "PHI": 0x006BB6,
    "PHX": 0x1D1160,
    "POR": 0xE03A3E,
    "SAC": 0x5A2D81,
    "SAS": 0xC4CED4,
    "TOR": 0xCE1141,
    "UTA": 0x002B5C,
    "WAS": 0x002B5C,
}

def get_team_color(team_abbr: str) -> int:
    return TEAM_COLORS.get(team_abbr.upper(), 0x2F3136)

def get_team_logo_url(team_abbr: str) -> str:
    team_abbr = team_abbr.upper()
    return f"https://i.cdn.turner.com/nba/nba/.element/img/1.0/teamsites/logos/teamlogos_500x500/{team_abbr}.png"

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
    alert_type: str

    @property
    def dedupe_key(self) -> str:
        return f"{self.game_id}:{self.player_id}:{self.alert_type}"


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

    embed = build_alert_embed(hit)

    try:
        await channel.send(embed=embed)
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
                    pts = int(player.get("pts") or 0)

                    four_by_four_match = (ast >= 4 and reb >= 4)
                    triple_double_watch_match = (pts >= 5 and reb >= 4 and ast >= 4)

                    if not four_by_four_match and not triple_double_watch_match:
                        continue

                    player_info = player.get("player", {}) or {}
                    player_id = str(player_info.get("id", player.get("id", "unknown")))
                    player_name = (
                        f"{player_info.get('first_name', '').strip()} {player_info.get('last_name', '').strip()}"
                    ).strip() or "Unknown Player"

                    common_data = dict(
                        game_id=game_id,
                        player_id=player_id,
                        player_name=player_name,
                        team_abbr=team_abbr,
                        opponent_abbr=opponent,
                        assists=ast,
                        rebounds=reb,
                        points=pts,
                        minutes=str(player.get("min") or "0"),
                        game_status=status,
                        game_period=period,
                        game_clock=clock,
                        matchup=matchup,
                    )

                    if four_by_four_match:
                        hits.append(
                            PlayerHit(
                                **common_data,
                                alert_type="halftime-4x4",
                            )
                        )

                    if triple_double_watch_match:
                        hits.append(
                            PlayerHit(
                                **common_data,
                                alert_type="triple-double-watch",
                            )
                        )

        return hits

def build_alert_embed(hit: PlayerHit) -> discord.Embed:
    def clean_time(val: str) -> str:
        if not val:
            return ""
        val = val.replace("PT", "").replace(".00S", "")
        if "M" in val:
            mins, rest = val.split("M", 1)
            secs = rest.replace("S", "") or "0"
            return f"{int(mins)}:{int(secs):02d}"
        return val.replace("S", "")

    pretty_clock = clean_time(hit.game_clock)
    pretty_minutes = clean_time(hit.minutes)

    if hit.alert_type == "triple-double-watch":
        title = "👀 Triple-Double Watch"
        stat_line = f"**{hit.points} PTS • {hit.rebounds} REB • {hit.assists} AST**"
    else:
        title = "🚨 Double-Double Watch 🚨"
        stat_line = f"**{hit.assists} AST • {hit.rebounds} REB • {hit.points} PTS**"

    embed = discord.Embed(
        title=title,
        description=(
            f"**{hit.player_name}** ({hit.team_abbr})\n"
            f"{stat_line}\n\n"
            f"**{hit.matchup}**"
        ),
        color=get_team_color(hit.team_abbr),
    )

    embed.set_thumbnail(url=get_team_logo_url(hit.team_abbr))

    embed.add_field(
        name="Game",
        value=hit.game_status or "Halftime / Early 3Q",
        inline=True,
    )

    embed.add_field(
        name="Minutes",
        value=pretty_minutes or hit.minutes or "-",
        inline=True,
    )

    if pretty_clock:
        embed.add_field(
            name="Clock",
            value=pretty_clock,
            inline=True,
        )

    return embed


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
