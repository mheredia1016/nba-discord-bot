import asyncio
import io
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

import aiohttp
from nba_api.live.nba.endpoints import scoreboard
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

def american_to_decimal(price: Optional[int]) -> Optional[float]:
    if price is None:
        return None
    try:
        price = int(price)
    except Exception:
        return None
    if price > 0:
        return 1 + (price / 100)
    if price < 0:
        return 1 + (100 / abs(price))
    return None

def decimal_to_american(decimal_odds: float) -> int:
    if decimal_odds >= 2:
        return int(round((decimal_odds - 1) * 100))
    return int(round(-100 / (decimal_odds - 1)))

def combined_american_odds(prices: List[int]) -> Optional[int]:
    decimal_total = 1.0
    for price in prices:
        dec = american_to_decimal(price)
        if dec is None:
            return None
        decimal_total *= dec
    return decimal_to_american(decimal_total)

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
DEBUG_STATS = os.getenv("DEBUG_STATS", "false").lower() == "true"
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports/basketball_nba"

TEAM_FILTER = {team.strip().upper() for team in os.getenv("TEAM_FILTER", "").split(",") if team.strip()}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
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
class PropLeg:
    player_name: str
    market_key: str
    label: str
    side: str
    line: Optional[float]
    price: Optional[int]
    book: str
    link: Optional[str] = None

    def display(self) -> str:
        line_txt = "" if self.line is None else f" {self.line:g}"
        price_txt = "" if self.price is None else f" ({self.price:+d})"
        book_txt = f"[{self.book}]({self.link})" if self.link else self.book
        return f"• **{self.player_name}** {self.side}{line_txt} {self.label}{price_txt} — {book_txt}"

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
        title, stat_line, subtitle = "👀 Early Watch", f"**{hit.assists} AST • {hit.rebounds} REB • {hit.points} PTS**", "Q1 signal"
    elif hit.alert_type == "triple-double-watch":
        title, stat_line, subtitle = "👀 Triple-Double Watch", f"**{hit.points} PTS • {hit.rebounds} REB • {hit.assists} AST**", "Halftime"
    elif hit.alert_type == "hes-on-fire":
        title, stat_line, subtitle = "🔥 He's On Fire", f"**{hit.points} PTS • {hit.threes_made} 3PM**", "Hot shooting start"
    else:
        title, stat_line, subtitle = "🚨 Double-Double Watch 🚨", f"**{hit.assists} AST • {hit.rebounds} REB • {hit.points} PTS**", "Halftime"

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
    return embed

def build_parlay_embed(matchup: str, book: str, groups: Dict[str, List[PropLeg]], game_status: str = "") -> discord.Embed:
    embed = discord.Embed(
        title="🎟️ Halftime Same-Book Parlay Builder",
        description=f"**{matchup}**\nBook: **{book}**\n\nUse this as an SGP builder. Links open individual legs/markets when available.",
        color=0xF5A623,
    )
    for tier_name, legs in groups.items():
        if not legs:
            continue
        prices = [leg.price for leg in legs if leg.price is not None]
        est = combined_american_odds(prices) if len(prices) == len(legs) else None
        odds_text = f" — est. {est:+d}" if est is not None else ""
        embed.add_field(name=f"{tier_name}{odds_text}", value="\n".join(leg.display() for leg in legs)[:1024], inline=False)
    if game_status:
        embed.set_footer(text=game_status)
    return embed

class HalftimeAlertBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None
        self.alerted: Set[str] = set()
        self.parlay_alerted_games: Set[str] = set()

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

            await self.maybe_send_parlay_builders(channel, hits)

        except Exception:
            log.exception("poll_live_games crashed this cycle")

    async def send_player_alert(self, channel, hit: PlayerHit) -> None:
        embed = build_alert_embed(hit)
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

    async def maybe_send_parlay_builders(self, channel, hits: List[PlayerHit]) -> None:
        if not ODDS_API_KEY:
            log.info("ODDS_API_KEY is not set. Skipping parlay builder.")
            return

        hits_by_game: Dict[str, List[PlayerHit]] = {}
        for hit in hits:
            hits_by_game.setdefault(hit.game_id, []).append(hit)

        for game_id, game_hits in hits_by_game.items():
            parlay_key = f"{game_id}:halftime-parlay-builder"
            if parlay_key in self.parlay_alerted_games:
                continue
            if not any(hit.game_period == 2 for hit in game_hits):
                continue

            first_hit = game_hits[0]
            try:
                embed = await self.build_same_book_parlay_embed(first_hit, game_hits)
            except Exception:
                log.exception("Could not build parlay embed for game %s", game_id)
                continue

            if embed:
                try:
                    await channel.send(embed=embed)
                    self.parlay_alerted_games.add(parlay_key)
                    log.info("Sent parlay builder for game %s", game_id)
                except Exception:
                    log.exception("Failed sending parlay builder for game %s", game_id)

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
        log.info("About to fetch live scoreboard")
        def get_games() -> List[dict]:
            board = scoreboard.ScoreBoard()
            return board.get_dict().get("scoreboard", {}).get("games", [])
        try:
            games = await asyncio.wait_for(asyncio.to_thread(get_games), timeout=20)
            log.info("Fetched %s live games from scoreboard", len(games))
            return games
        except asyncio.TimeoutError:
            log.exception("Timed out fetching live scoreboard")
            return []
        except Exception:
            log.exception("Failed fetching live scoreboard")
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

            for side, team_abbr, opponent_abbr in ((home, home_abbr, away_abbr), (away, away_abbr, home_abbr)):
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
                        log.info("%s | %s | Q%s | PTS=%s REB=%s AST=%s 3PM=%s", matchup, player_name, period, pts, reb, ast, threes_made)

                    early_watch = (period == 1 and ast >= 3 and reb >= 3)
                    double_double_watch = (period == 2 and ast >= 4 and reb >= 4)
                    triple_double_watch = (period == 2 and pts >= 5 and reb >= 4 and ast >= 4)
                    hes_on_fire = ((period == 1 and threes_made >= 2 and pts >= 8) or (period == 2 and threes_made >= 3 and pts >= 12))

                    if not early_watch and not double_double_watch and not triple_double_watch and not hes_on_fire:
                        continue

                    common_data = dict(game_id=game_id, player_id=player_id, player_name=player_name, team_abbr=team_abbr,
                                       opponent_abbr=opponent_abbr, assists=ast, rebounds=reb, points=pts,
                                       threes_made=threes_made, minutes=str(stats.get("minutes", "") or "0"),
                                       game_status=status, game_period=period, game_clock=clock, matchup=matchup)

                    if early_watch:
                        hits.append(PlayerHit(**common_data, alert_type="early-watch"))
                    if double_double_watch:
                        hits.append(PlayerHit(**common_data, alert_type="double-double-watch"))
                    if triple_double_watch:
                        hits.append(PlayerHit(**common_data, alert_type="triple-double-watch"))
                    if hes_on_fire:
                        hits.append(PlayerHit(**common_data, alert_type="hes-on-fire"))
        return hits

    async def fetch_odds_events(self) -> List[dict]:
        assert self.session is not None
        if not ODDS_API_KEY:
            return []
        url = f"{ODDS_API_BASE}/odds"
        params = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h", "oddsFormat": "american", "includeLinks": "true"}
        async with self.session.get(url, params=params) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Odds API events error {resp.status}: {text[:300]}")
            return await resp.json()

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

    async def fetch_event_props(self, event_id: str) -> dict:
        assert self.session is not None
        url = f"{ODDS_API_BASE}/events/{event_id}/odds"
        markets = ",".join([
            "player_points", "player_rebounds", "player_assists", "player_threes",
            "player_double_double", "player_points_rebounds_assists",
            "player_points_rebounds", "player_points_assists", "player_rebounds_assists",
        ])
        params = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": markets, "oddsFormat": "american", "includeLinks": "true"}
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

    def collect_book_legs(self, odds_data: dict, players: Set[str]) -> Dict[str, List[PropLeg]]:
        by_book: Dict[str, List[PropLeg]] = {}
        market_labels = {
            "player_points": "PTS", "player_rebounds": "REB", "player_assists": "AST",
            "player_threes": "3PM", "player_double_double": "Double-Double",
            "player_points_rebounds_assists": "PRA", "player_points_rebounds": "PTS+REB",
            "player_points_assists": "PTS+AST", "player_rebounds_assists": "REB+AST",
        }
        for bookmaker in odds_data.get("bookmakers", []) or []:
            book = bookmaker.get("title") or bookmaker.get("key") or "Sportsbook"
            for market in bookmaker.get("markets", []) or []:
                market_key = market.get("key", "")
                if market_key not in market_labels:
                    continue
                for outcome in market.get("outcomes", []) or []:
                    desc = str(outcome.get("description", "") or "").strip()
                    if desc not in players:
                        continue
                    side = str(outcome.get("name", "") or "").strip()
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
                    leg = PropLeg(desc, market_key, market_labels[market_key], side, line, price, book, self.extract_link(bookmaker, market, outcome))
                    by_book.setdefault(book, []).append(leg)
        return by_book

    def choose_best_leg(self, legs: List[PropLeg], player_name: str, market_keys: List[str]) -> Optional[PropLeg]:
        candidates = [leg for leg in legs if leg.player_name == player_name and leg.market_key in market_keys and leg.price is not None]
        if not candidates:
            return None
        return sorted(candidates, key=lambda x: (x.price or -9999, -(x.line or 0)), reverse=True)[0]

    def build_groups_for_book(self, legs: List[PropLeg], game_hits: List[PlayerHit]) -> Dict[str, List[PropLeg]]:
        players = {hit.player_name: hit for hit in game_hits}
        builder, risky, bomb = [], [], []

        for name, hit in players.items():
            if len(builder) < 3:
                keys = ["player_double_double", "player_rebounds_assists", "player_assists", "player_rebounds"]
                if hit.alert_type == "hes-on-fire":
                    keys = ["player_threes", "player_points"]
                elif hit.alert_type == "triple-double-watch":
                    keys = ["player_points_rebounds_assists", "player_double_double"]
                leg = self.choose_best_leg(legs, name, keys)
                if leg:
                    builder.append(leg)

        for name in players:
            if len(risky) < 3:
                leg = self.choose_best_leg(legs, name, ["player_points_rebounds_assists", "player_double_double", "player_threes", "player_assists", "player_rebounds"])
                if leg and leg not in risky:
                    risky.append(leg)

        for name in players:
            if len(bomb) < 3:
                leg = self.choose_best_leg(legs, name, ["player_double_double", "player_points_rebounds_assists", "player_threes"])
                if leg and leg not in bomb:
                    bomb.append(leg)

        all_player_legs = sorted([leg for leg in legs if leg.player_name in players and leg.price is not None], key=lambda x: x.price or -9999, reverse=True)

        def fill(bucket: List[PropLeg], target: int = 3) -> None:
            for leg in all_player_legs:
                if len(bucket) >= target:
                    return
                if leg not in bucket:
                    bucket.append(leg)

        fill(builder, 3)
        fill(risky, 3)
        fill(bomb, 3)
        return {"🧱 Builder Target +500": builder[:3], "🔥 Risky Target +1000": risky[:3], "💣 Bomb Target +3000": bomb[:3]}

    async def build_same_book_parlay_embed(self, first_hit: PlayerHit, game_hits: List[PlayerHit]) -> Optional[discord.Embed]:
        events = await self.fetch_odds_events()
        event = self.find_matching_odds_event(events, first_hit)
        if not event:
            log.info("No matching odds event found for %s", first_hit.matchup)
            return None
        odds_data = await self.fetch_event_props(event["id"])
        player_names = {hit.player_name for hit in game_hits}
        book_legs = self.collect_book_legs(odds_data, player_names)
        if not book_legs:
            log.info("No player prop legs found for %s", first_hit.matchup)
            return None

        best_book, best_groups, best_score = None, None, -1
        for book, legs in book_legs.items():
            groups = self.build_groups_for_book(legs, game_hits)
            total_legs = sum(len(v) for v in groups.values())
            unique_tiers = sum(1 for v in groups.values() if len(v) >= 2)
            score = total_legs + unique_tiers * 3
            if score > best_score:
                best_score, best_book, best_groups = score, book, groups

        if not best_book or not best_groups:
            return None
        return build_parlay_embed(first_hit.matchup, best_book, best_groups, first_hit.game_status)


bot = HalftimeAlertBot()

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

@bot.command()
async def testparlay(ctx: commands.Context) -> None:
    groups = {
        "🧱 Builder Target +500": [
            PropLeg("Nikola Jokic", "player_assists", "AST", "Over", 7.5, -115, "FanDuel", "https://sportsbook.fanduel.com/"),
            PropLeg("Jamal Murray", "player_points", "PTS", "Over", 20.5, -110, "FanDuel", "https://sportsbook.fanduel.com/"),
            PropLeg("Michael Porter Jr.", "player_threes", "3PM", "Over", 2.5, +135, "FanDuel", "https://sportsbook.fanduel.com/"),
        ],
        "🔥 Risky Target +1000": [
            PropLeg("Nikola Jokic", "player_points_rebounds_assists", "PRA", "Over", 45.5, +105, "FanDuel", "https://sportsbook.fanduel.com/"),
            PropLeg("Jamal Murray", "player_assists", "AST", "Over", 5.5, +120, "FanDuel", "https://sportsbook.fanduel.com/"),
            PropLeg("Aaron Gordon", "player_rebounds", "REB", "Over", 6.5, +125, "FanDuel", "https://sportsbook.fanduel.com/"),
        ],
        "💣 Bomb Target +3000": [
            PropLeg("Nikola Jokic", "player_double_double", "Double-Double", "Yes", None, +160, "FanDuel", "https://sportsbook.fanduel.com/"),
            PropLeg("Jamal Murray", "player_threes", "3PM", "Over", 3.5, +210, "FanDuel", "https://sportsbook.fanduel.com/"),
            PropLeg("Michael Porter Jr.", "player_points", "PTS", "Over", 24.5, +190, "FanDuel", "https://sportsbook.fanduel.com/"),
        ],
    }
    await ctx.send(embed=build_parlay_embed("DEN @ LAL", "FanDuel", groups, "TEST MODE"))

if __name__ == "__main__":
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not DISCORD_CHANNEL_ID:
        missing.append("DISCORD_CHANNEL_ID")
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
    bot.run(DISCORD_TOKEN)
