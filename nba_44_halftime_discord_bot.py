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
    team_abbr = team_abbr.lower()
    return f"https://a.espncdn.com/combiner/i?img=/i/teamlogos/nba/500/{team_abbr}.png"


def get_player_photo_url(player_id: str) -> str:
    # Smaller current NBA CDN headshot format tends to render more reliably in embeds
    return f"https://cdn.nba.com/headshots/nba/latest/260x190/{player_id}.png"


# ---------------------------------
# Config via environment variables
# ---------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
DEBUG_STATS = os.getenv("DEBUG_STATS", "false").lower() == "true"

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

    if hit.alert_type == "early-watch":
        title = "👀 Early Watch"
        stat_line = f"**{hit.assists} AST • {hit.rebounds} REB • {hit.points} PTS**"
        subtitle = "Q1 signal"
    elif hit.alert_type == "triple-double-watch":
        title = "👀 Triple-Double Watch"
        stat_line = f"**{hit.points} PTS • {hit.rebounds} REB • {hit.assists} AST**"
        subtitle = "Halftime"
    else:
        title = "🚨 Double-Double Watch 🚨"
        stat_line = f"**{hit.assists} AST • {hit.rebounds} REB • {hit.points} PTS**"
        subtitle = "Halftime"

    embed = discord.Embed(
        title=title,
        description=(
            f"**{hit.player_name}** ({hit.team_abbr})\n"
            f"{stat_line}\n\n"
            f"**{hit.matchup}**"
        ),
        color=get_team_color(hit.team_abbr),
    )

    # Player photo first. Team logo still shows in footer as fallback branding.
    embed.set_thumbnail(url=get_player_photo_url(hit.player_id))
    embed.set_footer(text=hit.team_abbr, icon_url=get_team_logo_url(hit.team_abbr))

    embed.add_field(
        name="Game",
        value=f"{hit.game_status or 'Live'} • {subtitle}",
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
        try:
            log.info("Polling cycle started")

            if not DISCORD_CHANNEL_ID:
                log.warning("DISCORD_CHANNEL_ID is not set. Skipping poll.")
                return

            hits = await self.find_alert_hits()
            log.info("Found %s qualifying hits this cycle", len(hits))

            if not hits:
                return

            try:
                channel = await self.fetch_channel(DISCORD_CHANNEL_ID)
            except Exception:
                log.exception("Channel %s could not be fetched.", DISCORD_CHANNEL_ID)
                return

            for hit in hits:
                if hit.dedupe_key in self.alerted:
                    log.info("Skipping duplicate hit %s", hit.dedupe_key)
                    continue

                self.alerted.add(hit.dedupe_key)
                embed = build_alert_embed(hit)

                try:
                    await channel.send(embed=embed)
                    log.info("Sent alert for %s", hit.dedupe_key)
                except Exception:
                    log.exception("Failed to send alert for %s", hit.dedupe_key)

        except Exception:
            log.exception("poll_live_games crashed this cycle")

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
            log.info("Fetched box score for game %s", game_id)
            return await resp.json()

    async def fetch_live_scoreboard_games(self) -> List[dict]:
        board = scoreboard.ScoreBoard()
        games = board.get_dict().get("scoreboard", {}).get("games", [])
        log.info("Fetched %s live games from scoreboard", len(games))
        return games

    async def find_alert_hits(self) -> List[PlayerHit]:
        try:
            games = await self.fetch_live_scoreboard_games()
        except Exception:
            log.exception("Could not fetch live scoreboard")
            return []

        hits: List[PlayerHit] = []

        for game in games:
            game_id = str(game.get("gameId", ""))
            if not game_id:
                continue

            home_team = game.get("homeTeam", {}) or {}
            away_team = game.get("awayTeam", {}) or {}
            home_abbr = (home_team.get("teamTricode") or "HOME").upper()
            away_abbr = (away_team.get("teamTricode") or "AWAY").upper()

            if TEAM_FILTER and home_abbr not in TEAM_FILTER and away_abbr not in TEAM_FILTER:
                continue

            matchup = f"{away_abbr} @ {home_abbr}"
            status = str(game.get("gameStatusText", "") or "")
            period = int(game.get("period", 0) or 0)
            clock = str(game.get("gameClock", "") or "")

            # Q1 early watch only, Q2 halftime alerts only. No Q3 alerts.
            if period not in {1, 2}:
                continue

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
                    pts = int(stats.get("points", 0) or 0)

                    first = str(player.get("firstName", "") or "").strip()
                    last = str(player.get("familyName", "") or "").strip()
                    player_name = f"{first} {last}".strip() or "Unknown Player"
                    player_id = str(player.get("personId", "unknown"))

                    if DEBUG_STATS:
                        log.info(
                            "%s | %s | Q%s | PTS=%s REB=%s AST=%s",
                            matchup, player_name, period, pts, reb, ast
                        )

                    early_watch = (period == 1 and ast >= 3 and reb >= 3)
                    double_double_watch = (period == 2 and ast >= 4 and reb >= 4)
                    triple_double_watch = (period == 2 and pts >= 5 and reb >= 4 and ast >= 4)

                    if not early_watch and not double_double_watch and not triple_double_watch:
                        continue

                    common_data = dict(
                        game_id=game_id,
                        player_id=player_id,
                        player_name=player_name,
                        team_abbr=team_abbr,
                        opponent_abbr=opponent_abbr,
                        assists=ast,
                        rebounds=reb,
                        points=pts,
                        minutes=str(stats.get("minutes", "") or "0"),
                        game_status=status,
                        game_period=period,
                        game_clock=clock,
                        matchup=matchup,
                    )

                    if early_watch:
                        hits.append(PlayerHit(**common_data, alert_type="early-watch"))

                    if double_double_watch:
                        hits.append(PlayerHit(**common_data, alert_type="double-double-watch"))

                    if triple_double_watch:
                        hits.append(PlayerHit(**common_data, alert_type="triple-double-watch"))

        return hits


bot = HalftimeAlertBot()


@bot.command()
async def ping(ctx: commands.Context) -> None:
    await ctx.send("pong")


@bot.command()
async def health(ctx: commands.Context) -> None:
    await ctx.send(
        f"Watching live NBA games every {POLL_SECONDS}s. "
        f"Team filter: {', '.join(sorted(TEAM_FILTER)) or 'none'}. "
        f"Debug stats: {DEBUG_STATS}."
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
