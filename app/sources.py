"""Second opinions beyond ESPN's projections. All free, no accounts or keys.

- Sleeper projections (matched to ESPN players by Sleeper's espn_id field)
- Sleeper actual weekly scoring for the last 3 weeks (recent form)
- Sleeper "trending adds" (what managers everywhere are picking up today)
- Vegas lines from ESPN's public NFL scoreboard (implied team points)
- Open-Meteo forecast at outdoor stadiums (wind, rain, snow at kickoff)

Every source is optional: if one fails, the others (and ESPN) still work.
Results are cached under DATA_DIR/cache so the NAS isn't hammering anyone.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger("sources")
UA = {"User-Agent": "espn-fantasy-bot (personal use)"}

# ESPN pro team abbreviations -> Sleeper's
TO_SLEEPER = {"WSH": "WAS"}
FROM_SLEEPER = {v: k for k, v in TO_SLEEPER.items()}

# Home stadium location of each team (for weather). Domes are detected from ESPN's scoreboard.
STADIUMS = {
    "ARI": (33.528, -112.263), "ATL": (33.755, -84.401), "BAL": (39.278, -76.623), "BUF": (42.774, -78.787),
    "CAR": (35.226, -80.853), "CHI": (41.862, -87.617), "CIN": (39.095, -84.516), "CLE": (41.506, -81.700),
    "DAL": (32.748, -97.093), "DEN": (39.744, -105.020), "DET": (42.340, -83.046), "GB": (44.501, -88.062),
    "HOU": (29.685, -95.411), "IND": (39.760, -86.164), "JAX": (30.324, -81.637), "KC": (39.049, -94.484),
    "LV": (36.091, -115.184), "LAC": (33.954, -118.339), "LAR": (33.954, -118.339), "MIA": (25.958, -80.239),
    "MIN": (44.974, -93.258), "NE": (42.091, -71.264), "NO": (29.951, -90.081), "NYG": (40.814, -74.074),
    "NYJ": (40.814, -74.074), "PHI": (39.901, -75.168), "PIT": (40.447, -80.016), "SF": (37.403, -121.970),
    "SEA": (47.595, -122.332), "TB": (27.976, -82.503), "TEN": (36.166, -86.771), "WSH": (38.908, -76.864),
}
AVG_TEAM_POINTS = 22.5


@dataclass
class TeamEnv:
    implied: float | None = None       # Vegas implied points for this team
    opp_implied: float | None = None   # ...and for their opponent
    spread: float | None = None        # negative = favoured
    total: float | None = None
    dome: bool = False
    wind_mph: float | None = None
    precip_mm: float | None = None
    temp_c: float | None = None
    snow: bool = False


@dataclass
class Extras:
    sleeper_proj: dict = field(default_factory=dict)   # espn id -> projected points this week
    sleeper_def: dict = field(default_factory=dict)    # ESPN team abbrev -> D/ST projection
    recent: dict = field(default_factory=dict)         # espn id -> [points, last 3 weeks]
    recent_def: dict = field(default_factory=dict)     # ESPN team abbrev -> [D/ST points]
    trending: dict = field(default_factory=dict)       # espn id -> adds in last 24h
    env: dict = field(default_factory=dict)            # ESPN team abbrev -> TeamEnv
    used: list = field(default_factory=list)           # names of sources that worked
    failed: dict = field(default_factory=dict)         # source name -> error


class Sources:
    def __init__(self, data_dir: str):
        self.dir = os.path.join(data_dir, "cache")
        os.makedirs(self.dir, exist_ok=True)
        self.s = requests.Session()
        self.s.headers.update(UA)
        self._players = None

    # ---- tiny file cache ----------------------------------------------------
    def _cached(self, name: str, ttl: float, fetch):
        path = os.path.join(self.dir, name)
        try:
            if os.path.exists(path) and (ttl < 0 or time.time() - os.path.getmtime(path) < ttl):
                with open(path) as f:
                    return json.load(f)
        except (OSError, ValueError):
            pass
        try:
            data = fetch()
        except Exception:
            if os.path.exists(path):  # stale is better than nothing
                with open(path) as f:
                    return json.load(f)
            raise
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return data

    def _get(self, url, params=None, timeout=40):
        r = self.s.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    # ---- Sleeper --------------------------------------------------------------
    def sleeper_players(self) -> dict:
        """sleeper id -> {espn_id, pos, team}. The full file is ~5 MB, so refreshed once a day."""
        if self._players is None or time.time() - self._players[0] > 6 * 3600:
            def fetch():
                raw = self._get("https://api.sleeper.app/v1/players/nfl", timeout=90)
                return {sid: {"espn_id": p.get("espn_id"), "pos": p.get("position"), "team": p.get("team")}
                        for sid, p in raw.items() if p.get("espn_id") or p.get("position") == "DEF"}
            self._players = (time.time(), self._cached("sleeper_players.json", 24 * 3600, fetch))
        return self._players[1]

    def _sleeper_week(self, kind: str, season: int, week: int, final: bool = False) -> dict:
        """sleeper id -> stat dict, for projections ('projections') or actual results ('stats')."""
        def fetch():
            try:  # newer endpoint (list of rows)
                rows = self._get(f"https://api.sleeper.app/{kind}/nfl/{season}/{week}",
                                 params=[("season_type", "regular")] + [("position[]", p) for p in
                                                                         ("QB", "RB", "WR", "TE", "K", "DEF")])
                if isinstance(rows, list) and rows:
                    return {str(r["player_id"]): r.get("stats") or {} for r in rows if r.get("player_id")}
            except Exception as e:
                log.info("sleeper %s new endpoint: %s", kind, e)
            data = self._get(f"https://api.sleeper.app/v1/{kind}/nfl/regular/{season}/{week}")
            return {str(k): v for k, v in (data or {}).items()}
        ttl = -1 if final else 3 * 3600  # finished weeks never change: keep forever
        return self._cached(f"sleeper_{kind}_{season}_{week}.json", ttl, fetch)

    def load(self, season: int, week: int, scoring_key: str, now: float | None = None) -> Extras:
        now = now or time.time()
        ex = Extras()
        try:
            players = self.sleeper_players()
        except Exception as e:
            players = {}
            ex.failed["Sleeper"] = str(e)[:120]

        def espn_id(sid):
            p = players.get(sid) or {}
            e = p.get("espn_id")
            return int(e) if e not in (None, "") and str(e).isdigit() else None

        def def_team(sid):
            p = players.get(sid) or {}
            if p.get("pos") == "DEF" or (sid.isalpha() and sid.isupper()):
                t = p.get("team") or sid
                return FROM_SLEEPER.get(t, t)
            return None

        def pts(stats):
            for k in (scoring_key, "pts_half_ppr", "pts_ppr", "pts_std"):
                if stats.get(k) is not None:
                    return float(stats[k])
            return None

        # projections
        if players:
            try:
                for sid, st in self._sleeper_week("projections", season, week).items():
                    v = pts(st or {})
                    if v is None:
                        continue
                    t = def_team(sid)
                    if t:
                        ex.sleeper_def[t] = v
                    elif (e := espn_id(sid)):
                        ex.sleeper_proj[e] = v
                if ex.sleeper_proj:
                    ex.used.append("Sleeper projections")
            except Exception as e:
                ex.failed["Sleeper projections"] = str(e)[:120]
            # recent form: last 3 completed weeks
            try:
                for w in range(max(1, week - 3), week):
                    # weeks ending more than 2 days ago are final
                    for sid, st in self._sleeper_week("stats", season, w, final=w < week - 1).items():
                        v = pts(st or {})
                        if v is None or not (st or {}).get("gp", 1):
                            continue
                        t = def_team(sid)
                        if t:
                            ex.recent_def.setdefault(t, []).append(v)
                        elif (e := espn_id(sid)):
                            ex.recent.setdefault(e, []).append(v)
                if ex.recent:
                    ex.used.append("recent form")
            except Exception as e:
                ex.failed["recent form"] = str(e)[:120]
            try:
                rows = self._cached("sleeper_trending.json", 2 * 3600, lambda: self._get(
                    "https://api.sleeper.app/v1/players/nfl/trending/add",
                    params={"lookback_hours": 24, "limit": 60}))
                for r in rows:
                    if (e := espn_id(str(r.get("player_id")))):
                        ex.trending[e] = int(r.get("count", 0))
                if ex.trending:
                    ex.used.append("trending adds")
            except Exception as e:
                ex.failed["trending adds"] = str(e)[:120]

        # Vegas lines + domes from ESPN's public scoreboard
        try:
            sb = self._cached(f"scoreboard_{season}_{week}.json", 2 * 3600, lambda: self._get(
                "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
                params={"week": week, "seasontype": 2, "dates": season}))
            self._parse_scoreboard(sb, ex)
            if any(e.implied for e in ex.env.values()):
                ex.used.append("Vegas lines")
        except Exception as e:
            ex.failed["Vegas lines"] = str(e)[:120]

        # weather at outdoor stadiums, for games in the next 7 days
        try:
            n = self._weather(ex, now)
            if n:
                ex.used.append("weather")
        except Exception as e:
            ex.failed["weather"] = str(e)[:120]
        return ex

    # ---- Vegas ------------------------------------------------------------------
    @staticmethod
    def _parse_scoreboard(sb: dict, ex: Extras):
        for ev in sb.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            teams = {}
            for c in comp.get("competitors", []):
                ab = (c.get("team") or {}).get("abbreviation", "")
                teams[c.get("homeAway")] = ab
            if len(teams) < 2:
                continue
            venue = comp.get("venue") or {}
            dome = bool(venue.get("indoor"))
            home, away = teams.get("home"), teams.get("away")
            kickoff = ev.get("date")
            envs = {home: TeamEnv(dome=dome), away: TeamEnv(dome=dome)}
            for e in envs.values():
                e.kickoff = kickoff
                e.home_team = home
                e.neutral = bool(comp.get("neutralSite"))
            odds = (comp.get("odds") or [{}])[0]
            total = odds.get("overUnder")
            details = (odds.get("details") or "").strip()  # e.g. "KC -3.5" or "EVEN"
            if total:
                fav, line = None, 0.0
                parts = details.split()
                if len(parts) == 2:
                    try:
                        fav, line = parts[0], abs(float(parts[1]))
                    except ValueError:
                        fav, line = None, 0.0
                for team, other in ((home, away), (away, home)):
                    s = -line if team == fav else (line if fav else 0.0)
                    envs[team].spread = s
                    envs[team].total = float(total)
                    envs[team].implied = round(float(total) / 2 - s / 2, 1)
                for team, other in ((home, away), (away, home)):
                    envs[team].opp_implied = envs[other].implied
            ex.env.update(envs)

    # ---- weather ----------------------------------------------------------------
    def _weather(self, ex: Extras, now: float) -> int:
        done = 0
        for team, env in list(ex.env.items()):
            if env.dome or getattr(env, "home_team", None) != team or getattr(env, "neutral", False):
                continue
            loc = STADIUMS.get(team)
            ko = getattr(env, "kickoff", None)
            if not loc or not ko:
                continue
            try:
                ko_ts = calendar.timegm(time.strptime(ko[:16], "%Y-%m-%dT%H:%M"))
            except ValueError:
                continue
            if not (now - 6 * 3600 < ko_ts < now + 7 * 86400):
                continue
            hour = time.strftime("%Y-%m-%dT%H:00", time.gmtime(ko_ts))
            data = self._cached(f"wx_{team}_{hour[:13]}.json", 3 * 3600, lambda: self._get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": loc[0], "longitude": loc[1], "timezone": "GMT",
                        "hourly": "temperature_2m,precipitation,snowfall,wind_speed_10m",
                        "wind_speed_unit": "mph", "forecast_days": 8}))
            h = data.get("hourly", {})
            times = h.get("time", [])
            if hour not in times:
                continue
            i = times.index(hour)
            # average over the ~3 hours of the game
            sl = slice(i, min(i + 3, len(times)))
            avg = lambda k: sum((h.get(k) or [0])[sl]) / max(1, len((h.get(k) or [0])[sl]))
            for t in (team,) + tuple(k for k, e in ex.env.items() if getattr(e, "home_team", None) == team and k != team):
                e = ex.env[t]
                e.wind_mph, e.precip_mm, e.temp_c = round(avg("wind_speed_10m"), 1), round(avg("precipitation"), 2), round(avg("temperature_2m"), 1)
                e.snow = avg("snowfall") > 0.1
            done += 1
        return done


# ---------------------------------------------------------------------------
def adjust(p, ex: Extras, notes: bool = True):
    """Blend ESPN with the other sources for one player (changes p in place)."""
    espn = p.week_proj
    p.espn_proj = espn
    p.notes = []
    if p.pos == "D/ST":
        other = ex.sleeper_def.get(p.pro_team)
        recent = ex.recent_def.get(p.pro_team) or []
        w_other = 0.3   # ESPN and Sleeper score defenses differently, so lean on ESPN
    else:
        other = ex.sleeper_proj.get(p.id)
        recent = ex.recent.get(p.id) or []
        w_other = 0.5
    p.sleeper_proj = other
    base = espn
    if other is not None and espn > 0:
        base = (1 - w_other) * espn + w_other * other
    elif other is not None and espn == 0 and not p.bye and not p.is_out:
        base = other * 0.8  # ESPN has no projection; trust Sleeper a bit less
    # recent form: a gentle nudge, not a replacement
    p.recent_avg = round(sum(recent) / len(recent), 1) if len(recent) >= 2 else None
    if p.recent_avg is not None and base > 0:
        base = 0.85 * base + 0.15 * p.recent_avg

    env = ex.env.get(p.pro_team)
    f = 1.0
    if env:
        if env.implied and p.pos in ("QB", "RB", "WR", "TE", "K"):
            f *= max(0.88, min(1.12, (env.implied / AVG_TEAM_POINTS) ** 0.5))
            if notes and abs(env.implied - AVG_TEAM_POINTS) >= 3:
                p.notes.append(f"Vegas: team expected to score {env.implied:.0f}")
        if env.opp_implied and p.pos == "D/ST":
            f *= max(0.85, min(1.18, (AVG_TEAM_POINTS / env.opp_implied) ** 0.6))
            if notes and abs(env.opp_implied - AVG_TEAM_POINTS) >= 3:
                p.notes.append(f"Vegas: opponent expected to score {env.opp_implied:.0f}")
        wf = 1.0
        if env.wind_mph is not None:
            if env.wind_mph >= 20:
                wf = {"K": 0.8, "QB": 0.9, "WR": 0.92, "TE": 0.95, "RB": 1.03, "D/ST": 1.06}.get(p.pos, 1.0)
            elif env.wind_mph >= 15:
                wf = {"K": 0.9, "QB": 0.96, "WR": 0.97, "D/ST": 1.03}.get(p.pos, 1.0)
            if (env.precip_mm or 0) >= 1.5 or env.snow:
                wf *= {"K": 0.92, "QB": 0.95, "WR": 0.95, "TE": 0.97, "D/ST": 1.04}.get(p.pos, 1.0)
            if notes and (env.wind_mph >= 15 or (env.precip_mm or 0) >= 1.5 or env.snow):
                bits = [f"wind {env.wind_mph:.0f} mph"] if env.wind_mph >= 15 else []
                if env.snow:
                    bits.append("snow")
                elif (env.precip_mm or 0) >= 1.5:
                    bits.append("rain")
                p.notes.append("Weather: " + ", ".join(bits))
        f *= wf
    p.env_factor = round(f, 3)
    p.week_proj = round(base * f, 2)
    p.trending = ex.trending.get(p.id, 0)
    return p
