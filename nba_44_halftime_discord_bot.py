import asyncio
import io
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

import aiohttp
import discord
from discord.ext import commands, tasks

TEAM_COLORS = {
    "ATL": 0xE03A3E, "BOS": 0x007A33, "BKN": 0x000000, "CHA": 0x1D1160,
    "CHI": 0xCE1141, "CLE": 0x860038, "DAL": 0x00538C, "DEN": 0x0E2240,
    "DET": 0xC8102E, "GSW": 0x1D428A, "HOU": 0xCE1141, "IND": 0x002D62,
    "LAC": 0xC8102E, "LAL": 0x552583, "MEM": 0x5D76A9, "MIA": 0x98002E,
    "MIL": 0x00471B, "MIN": 0x0C2340, "NOP": 0x0C2340, "NYK": 0x006BB6,
    "OKC": 0x007AC1, "ORL": 0x0077C0, "PHI": 0x006BB6, "PHX": 0x1D1160,
    "POR": 0xE03A3E, "SAC": 0x5A2D81, "SAS": 0xC4CED4, "TOR": 0xCE1141,
    "UTA": 0x002B5C, "WAS": 0x002B5C,
}

TEAM_NAMES = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}


def get_team_color(team_abbr: str) -> int:
    return TEAM_COLORS.get(team_abbr.upper(), 0x2F3136)


def get_team_logo_url(team_abbr: str) -> str:
    return f"https://a.espncdn.com/combiner/i?img=/i/teamlogos/nba/500/{team_abbr.lower()}.png"


def get_player_photo_url(player_id: str) -> str:
    return f"https://cdn.nba.com/headshots/nba/latest/260x190/{player_id}.png"


DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
DEBUG_STATS = os.getenv("DEBUG_STATS", "false").lower() == "true"

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports/basketball_nba"

TEAM_FILTER = {
    team.strip().upper()
    for team in os.getenv("TEAM_FILTER", "").split(",")
    if team.strip()
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("nba_alert_bot")


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
    threes_made: int
    minutes: str
    game_status: str
    game_period: int
    game_clock: str
    matchup: str
    alert_type: str

    @property
    def dedupe_key(self) -> str:
        return f"{self.game_id}:{self.player_id}:{self.alert_type}"


@dataclass
class OddsPick:
    book: str
    market_key: str
    label: str
    side: str
    line: Optional[float]
    price: Optional[int]
    link: Optional[str] = None

    def display(self) -> str:
        line_txt = "" if self.line is None else f" {self.line:g}"
        price_txt = "" if self.price is None else f" ({self.price:+d})"
        book_txt = f"[{self.book}]({self.link})" if self.link else self.book
        return f"{book_txt} — {self.side}{line_txt} {self.label}{price_txt}"


def clean_time(val: str) -> str:
    if not val:
        return ""
    val = val.replace("PT", "").replace(".00S", "")
    if "M" in val:
        mins, rest = val.split("M", 1)
        secs = rest.replace("S", "") or "0"
        return f"{int(mins)}:{int(secs):02d}"
    return val.replace("S", "")


def build_alert_embed(hit: PlayerHit, odds_pick: Optional[OddsPick] = None) -> discord.Embed:
    pretty_clock = clean_time(hit.game_clock)
    pretty_minutes = clean_time(hit.minutes)

    if hit.alert_type == "early-watch":
        title = "📈 Q1 Stat Watch"
        stat_line = f"**{hit.assists} AST • {hit.rebounds} REB • {hit.points} PTS**"

        if hit.assists >= 3 and hit.rebounds >= 3:
            tag = "🔥 All-Around"
        elif hit.assists >= 3:
            tag = "🎯 Playmaker"
        elif hit.rebounds >= 3:
            tag = "🧱 Glass Cleaner"
        else:
            tag = ""

        if tag:
            stat_line += f"\\n{tag}"

        subtitle = "Early activity"
    elif hit.alert_type == "q2-stat-watch":
        title = "⚡ Q2 Stat Watch"
        stat_line = f"**{hit.assists} AST • {hit.rebounds} REB • {hit.points} PTS**"

        if hit.assists >= 3 and hit.rebounds >= 3:
            tag = "🔥 All-Around"
        elif hit.assists >= 3:
            tag = "🎯 Playmaker"
        elif hit.rebounds >= 3:
            tag = "🧱 Glass Cleaner"
        else:
            tag = ""

        if tag:
            stat_line += f"\n{tag}"

        subtitle = "Mid-game activity"

    elif hit.alert_type == "triple-double-watch":
        title = "👀 Triple-Double Watch"
        stat_line = f"**{hit.points} PTS • {hit.rebounds} REB • {hit.assists} AST**"
        subtitle = "Halftime"
    elif hit.alert_type == "hes-on-fire":
        title = "🔥 He's On Fire"
        stat_line = f"**{hit.points} PTS • {hit.threes_made} 3PM**"
        subtitle = "Hot shooting start"
    else:
        title = "🚨 Double-Double Watch 🚨"
        stat_line = f"**{hit.assists} AST • {hit.rebounds} REB • {hit.points} PTS**"
        subtitle = "Halftime"

    embed = discord.Embed(
        title=title,
        description=f"**{hit.player_name}** ({hit.team_abbr})\n{stat_line}\n\n**{hit.matchup}**",
        color=get_team_color(hit.team_abbr),
    )

    embed.set_footer(text=hit.team_abbr, icon_url=get_team_logo_url(hit.team_abbr))
    embed.add_field(name="Game", value=f"{hit.game_status or 'Live'} • {subtitle}", inline=True)
    embed.add_field(name="Minutes", value=pretty_minutes or hit.minutes or "-", inline=True)

    if pretty_clock:
        embed.add_field(name="Clock", value=pretty_clock, inline=True)

    if odds_pick:
        embed.add_field(
            name="Best Related Odds",
            value=f"{odds_pick.display()}\n⚠️ Odds may be pregame/last available.",
            inline=False,
        )

    return embed


class NBAAlertBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None
        self.alerted: Set[str] = set()
        self.odds_events_cache: List[dict] = []
        self.odds_events_cache_cycle: int = 0
        self.poll_cycle: int = 0

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.poll_live_games.start()

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def fetch_player_photo_bytes(self, player_id: str) -> Optional[bytes]:
        assert self.session is not None
        try:
            async with self.session.get(get_player_photo_url(player_id)) as resp:
                if resp.status == 200:
                    return await resp.read()
                log.info("No player photo for %s (status %s)", player_id, resp.status)
                return None
        except Exception:
            log.exception("Failed to fetch player photo for %s", player_id)
            return None

    @tasks.loop(seconds=POLL_SECONDS)
    async def poll_live_games(self) -> None:
        try:
            self.poll_cycle += 1
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
                await self.send_player_alert(channel, hit)

        except Exception:
            log.exception("poll_live_games crashed this cycle")

    async def send_player_alert(self, channel, hit: PlayerHit) -> None:
        odds_pick = await self.get_best_related_odds(hit)
        embed = build_alert_embed(hit, odds_pick)

        try:
            photo_bytes = await self.fetch_player_photo_bytes(hit.player_id)

            if photo_bytes:
                file = discord.File(fp=io.BytesIO(photo_bytes), filename=f"{hit.player_id}.png")
                embed.set_thumbnail(url=f"attachment://{hit.player_id}.png")
                await channel.send(embed=embed, file=file)
            else:
                embed.set_thumbnail(url=get_team_logo_url(hit.team_abbr))
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
            log.info("Fetched box score for game %s", game_id)
            return await resp.json()

    async def fetch_live_scoreboard_games(self) -> List[dict]:
        assert self.session is not None

        url = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"

        try:
            async with self.session.get(url) as resp:
                text = await resp.text()

                if resp.status == 429:
                    log.warning("NBA scoreboard rate limited (429)")
                    return []

                if resp.status != 200:
                    log.warning("NBA scoreboard HTTP %s: %s", resp.status, text[:300])
                    return []

                if not text.strip():
                    log.warning("NBA scoreboard returned empty response")
                    return []

                try:
                    data = await resp.json()
                except Exception:
                    log.exception("NBA scoreboard returned invalid JSON: %s", text[:300])
                    return []

                games = data.get("scoreboard", {}).get("games", [])
                log.info("Fetched %s live NBA games from CDN scoreboard", len(games))
                return games

        except Exception:
            log.exception("Failed fetching live NBA scoreboard")
            return []

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
                    threes_made = int(stats.get("threePointersMade", 0) or 0)

                    first = str(player.get("firstName", "") or "").strip()
                    last = str(player.get("familyName", "") or "").strip()
                    player_name = f"{first} {last}".strip() or "Unknown Player"
                    player_id = str(player.get("personId", "unknown"))

                    if DEBUG_STATS:
                        log.info(
                            "%s | %s | Q%s | PTS=%s REB=%s AST=%s 3PM=%s",
                            matchup, player_name, period, pts, reb, ast, threes_made
                        )

                    early_watch = (
                        period == 1 and (
                            ast >= 3 or reb >= 3
                        )
                    )

                    q2_stat_watch = (
                        period == 2 and (
                            ast >= 3 or reb >= 3
                        )
                    )

                    double_double_watch = (period == 2 and ast >= 4 and reb >= 4)
                    triple_double_watch = (period == 2 and pts >= 5 and reb >= 4 and ast >= 4)
                    hes_on_fire = (
                        (period == 1 and threes_made >= 2 and pts >= 8) or
                        (period == 2 and threes_made >= 3 and pts >= 12)
                    )

                    if not early_watch and not q2_stat_watch and not double_double_watch and not triple_double_watch and not hes_on_fire:
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
                        threes_made=threes_made,
                        minutes=str(stats.get("minutes", "") or "0"),
                        game_status=status,
                        game_period=period,
                        game_clock=clock,
                        matchup=matchup,
                    )

                    if early_watch:
                        hits.append(PlayerHit(**common_data, alert_type="early-watch"))

                    if q2_stat_watch:
                        hits.append(PlayerHit(**common_data, alert_type="q2-stat-watch"))

                    if double_double_watch:
                        hits.append(PlayerHit(**common_data, alert_type="double-double-watch"))

                    if triple_double_watch:
                        hits.append(PlayerHit(**common_data, alert_type="triple-double-watch"))

                    if hes_on_fire:
                        hits.append(PlayerHit(**common_data, alert_type="hes-on-fire"))

        return hits

    # -----------------------------
    # Odds API helpers
    # -----------------------------

    async def fetch_odds_events(self) -> List[dict]:
        assert self.session is not None

        if not ODDS_API_KEY:
            return []

        # Cache for ~10 polling cycles to reduce Odds API credits.
        if self.odds_events_cache and (self.poll_cycle - self.odds_events_cache_cycle) < 10:
            return self.odds_events_cache

        url = f"{ODDS_API_BASE}/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "us",
            "markets": "h2h",
            "oddsFormat": "american",
            "includeLinks": "true",
        }

        async with self.session.get(url, params=params) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Odds API events error {resp.status}: {text[:300]}")
            events = await resp.json()
            self.odds_events_cache = events
            self.odds_events_cache_cycle = self.poll_cycle
            return events

    def find_matching_odds_event(self, events: List[dict], hit: PlayerHit) -> Optional[dict]:
        team_name = TEAM_NAMES.get(hit.team_abbr)
        opp_name = TEAM_NAMES.get(hit.opponent_abbr)

        if not team_name or not opp_name:
            return None

        expected = {team_name, opp_name}

        for event in events:
            if {event.get("home_team"), event.get("away_team")} == expected:
                return event

        return None

    def markets_for_alert(self, alert_type: str) -> List[str]:
        if alert_type == "double-double-watch":
            return [
                "player_double_double",
                "player_rebounds_assists",
                "player_rebounds",
                "player_assists",
            ]

        if alert_type == "triple-double-watch":
            return [
                "player_triple_double",
                "player_points_rebounds_assists",
                "player_double_double",
            ]

        if alert_type == "hes-on-fire":
            return [
                "player_threes",
                "player_points",
            ]

        if alert_type in {"early-watch", "q2-stat-watch"}:
            return [
                "player_rebounds_assists",
                "player_assists",
                "player_rebounds",
            ]

        return ["player_points", "player_rebounds", "player_assists"]

    async def fetch_event_props(self, event_id: str, markets: List[str]) -> dict:
        assert self.session is not None

        url = f"{ODDS_API_BASE}/events/{event_id}/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "us",
            "markets": ",".join(markets),
            "oddsFormat": "american",
            "includeLinks": "true",
        }

        async with self.session.get(url, params=params) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Odds API props error {resp.status}: {text[:300]}")
            return await resp.json()

    def extract_link(self, bookmaker: dict, market: dict, outcome: dict) -> Optional[str]:
        for obj in (outcome, market, bookmaker):
            for key in ("link", "links", "url", "urls"):
                val = obj.get(key)

                if isinstance(val, str):
                    return val

                if isinstance(val, dict):
                    for nested in val.values():
                        if isinstance(nested, str):
                            return nested

        return None

    def market_label(self, market_key: str) -> str:
        labels = {
            "player_double_double": "Double-Double",
            "player_triple_double": "Triple-Double",
            "player_points_rebounds_assists": "PRA",
            "player_rebounds_assists": "REB+AST",
            "player_points_rebounds": "PTS+REB",
            "player_points_assists": "PTS+AST",
            "player_points": "PTS",
            "player_rebounds": "REB",
            "player_assists": "AST",
            "player_threes": "3PM",
        }
        return labels.get(market_key, market_key)

    def score_odds_pick(self, pick: OddsPick, preferred_markets: List[str]) -> float:
        market_priority = len(preferred_markets) - preferred_markets.index(pick.market_key) if pick.market_key in preferred_markets else 0
        price_score = float(pick.price or -999) / 100
        return market_priority * 100 + price_score

    async def get_best_related_odds(self, hit: PlayerHit) -> Optional[OddsPick]:
        if not ODDS_API_KEY:
            return None

        try:
            markets = self.markets_for_alert(hit.alert_type)
            events = await self.fetch_odds_events()
            event = self.find_matching_odds_event(events, hit)

            if not event:
                log.info("No matching odds event found for %s", hit.matchup)
                return None

            odds_data = await self.fetch_event_props(event["id"], markets)
            picks: List[OddsPick] = []

            for bookmaker in odds_data.get("bookmakers", []) or []:
                book = bookmaker.get("title") or bookmaker.get("key") or "Sportsbook"

                for market in bookmaker.get("markets", []) or []:
                    market_key = market.get("key", "")

                    if market_key not in markets:
                        continue

                    for outcome in market.get("outcomes", []) or []:
                        desc = str(outcome.get("description", "") or "").strip()

                        if desc.lower() != hit.player_name.lower():
                            continue

                        side = str(outcome.get("name", "") or "").strip()

                        # We only want actionable positive legs.
                        if side.lower() not in {"over", "yes"}:
                            continue

                        try:
                            price = int(outcome.get("price"))
                        except Exception:
                            price = None

                        try:
                            line = float(outcome.get("point")) if outcome.get("point") is not None else None
                        except Exception:
                            line = None

                        picks.append(
                            OddsPick(
                                book=book,
                                market_key=market_key,
                                label=self.market_label(market_key),
                                side=side,
                                line=line,
                                price=price,
                                link=self.extract_link(bookmaker, market, outcome),
                            )
                        )

            if not picks:
                log.info("No related odds found for %s / %s", hit.player_name, hit.alert_type)
                return None

            return sorted(picks, key=lambda p: self.score_odds_pick(p, markets), reverse=True)[0]

        except Exception:
            log.exception("Could not fetch best related odds for %s", hit.player_name)
            return None


bot = NBAAlertBot()


@bot.command()
async def ping(ctx: commands.Context) -> None:
    await ctx.send("pong")


@bot.command()
async def health(ctx: commands.Context) -> None:
    await ctx.send(
        f"Watching live NBA games every {POLL_SECONDS}s. "
        f"Team filter: {', '.join(sorted(TEAM_FILTER)) or 'none'}. "
        f"Debug stats: {DEBUG_STATS}. "
        f"Odds API: {'set' if bool(ODDS_API_KEY) else 'missing'}."
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
