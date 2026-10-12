"""Turns ESPN's raw JSON into simple Python objects the rest of the app uses."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# Lineup slot ids ESPN uses
SLOT_NAMES = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE", 7: "OP",
    8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S", 14: "DB", 15: "DP",
    16: "D/ST", 17: "K", 18: "P", 19: "HC", 20: "BE", 21: "IR", 23: "FLEX", 24: "ER",
}
BENCH, IR = 20, 21
# A player's main position (defaultPositionId) -> name
POS_NAMES = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 7: "P", 9: "DT", 10: "DE", 11: "LB",
             12: "CB", 13: "S", 14: "HC", 16: "D/ST"}
# Main position -> the slot id that holds only that position (used for roster position limits)
POS_SLOT = {1: 0, 2: 2, 3: 4, 4: 6, 5: 17, 16: 16}

PRO_TEAMS = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET",
    9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN",
    17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC",
    25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}

INJURY_LABELS = {
    "ACTIVE": "", "NORMAL": "", "PROBABLE": "P", "QUESTIONABLE": "Q", "DOUBTFUL": "D",
    "OUT": "O", "INJURY_RESERVE": "IR", "SUSPENSION": "SSPD", "DAY_TO_DAY": "DTD",
}
OUT_STATUSES = {"OUT", "INJURY_RESERVE", "SUSPENSION"}


@dataclass
class Player:
    id: int
    name: str
    pos: str
    pos_id: int
    pro_team: str
    pro_team_id: int
    eligible: list[int]
    injury: str = "ACTIVE"
    slot: int | None = None          # current lineup slot (None if not on a fantasy team)
    on_team_id: int = 0
    status: str = "ONTEAM"           # ONTEAM / FREEAGENT / WAIVERS
    week_proj: float = 0.0           # ESPN projection for this week
    week_points: float | None = None  # actual points this week so far
    proj_avg: float = 0.0            # ESPN's projected points per game for the season
    actual_avg: float = 0.0          # actual points per game so far
    games: int = 0
    owned: float = 0.0               # % of ESPN leagues that roster him
    started: float = 0.0
    locked: bool = False             # his game has started: can't be moved/added/dropped
    droppable: bool = True
    game_time: float | None = None   # kickoff (epoch seconds) this week, None = bye
    sched_known: bool = True         # False if the NFL schedule couldn't be loaded
    # filled in by sources.adjust() when the extra sources are available
    espn_proj: float | None = None
    sleeper_proj: float | None = None
    recent_avg: float | None = None  # actual points per game, last 3 weeks
    env_factor: float = 1.0          # Vegas + weather multiplier
    trending: int = 0                # adds across Sleeper leagues in the last 24h
    notes: list = field(default_factory=list)
    sos_factor: float = 1.0          # fantasy-playoff schedule multiplier on season value
    opponent: str = ""
    home: bool = True

    @property
    def bye(self) -> bool:
        return self.sched_known and self.game_time is None and self.pro_team_id != 0

    @property
    def is_out(self) -> bool:
        return self.injury in OUT_STATUSES

    @property
    def injury_label(self) -> str:
        return INJURY_LABELS.get(self.injury, self.injury[:3] if self.injury else "")

    def adjusted(self, q_factor: float = 0.85, d_factor: float = 0.25) -> float:
        """This week's projection, discounted for injury risk; 0 if out or on bye."""
        if self.bye or self.is_out or (self.pro_team_id == 0):
            return 0.0
        p = self.week_proj
        if self.injury == "QUESTIONABLE":
            p *= q_factor
        elif self.injury == "DOUBTFUL":
            p *= d_factor
        return round(p, 2)

    @property
    def value(self) -> float:
        """Rest-of-season worth per game: ESPN projection blended with how he's actually scored."""
        if self.games >= 3 and self.proj_avg:
            v = 0.6 * self.proj_avg + 0.4 * self.actual_avg
        else:
            v = self.proj_avg or self.actual_avg
        if self.recent_avg is not None and v:
            v = 0.8 * v + 0.2 * self.recent_avg   # last 3 weeks count a little extra
        v *= self.sos_factor
        if self.injury == "INJURY_RESERVE":
            v *= 0.3
        elif self.injury in ("OUT", "SUSPENSION"):
            v *= 0.8
        return round(v, 2)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "pos": self.pos, "team": self.pro_team,
            "slot": SLOT_NAMES.get(self.slot, "") if self.slot is not None else "",
            "slot_id": self.slot, "injury": self.injury_label, "injury_full": self.injury,
            "proj": round(self.week_proj, 1), "points": self.week_points,
            "value": round(self.value, 1), "owned": round(self.owned, 1),
            "locked": self.locked, "bye": self.bye, "opp": self.opponent, "home": self.home,
            "kickoff": self.game_time, "status": self.status,
            "espn": None if self.espn_proj is None else round(self.espn_proj, 1),
            "sleeper": None if self.sleeper_proj is None else round(self.sleeper_proj, 1),
            "recent": self.recent_avg, "env": self.env_factor, "trending": self.trending, "notes": self.notes,
            "sos": round(self.sos_factor, 3),
        }


@dataclass
class Team:
    id: int
    name: str
    abbrev: str
    roster: list[Player] = field(default_factory=list)
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    faab_spent: int = 0
    acquisitions_by_period: dict = field(default_factory=dict)


@dataclass
class League:
    id: int
    season: int
    week: int
    name: str
    teams: dict[int, Team]
    my_team_id: int
    slot_counts: dict[int, int]       # starting + bench + IR slot counts
    position_limits: dict[int, int]   # main position id (1 QB, 2 RB…) -> max on roster (<=0 = no limit)
    faab: bool = False
    faab_budget: int = 0
    min_bid: int = 0
    scoring: str = ""
    opponent_id: int | None = None
    trade_deadline: float = 0.0
    final_week: int = 18
    matchup_period: int = 0
    ppr: float = 0.0                  # points per reception (1 = PPR, 0.5 = half)
    reg_periods: int = 0              # regular-season matchup periods
    matchup_periods: dict = field(default_factory=dict)  # period -> [scoring weeks]
    schedule: list = field(default_factory=list)         # raw ESPN matchups (scores, winners)
    acq_limit_per_matchup: int = 0
    raw_status: dict = field(default_factory=dict)

    @property
    def me(self) -> Team:
        return self.teams[self.my_team_id]

    @property
    def starting_slots(self) -> list[int]:
        """One entry per starting spot, e.g. [0, 2, 2, 4, 4, 6, 23, 16, 17]."""
        out = []
        for sid, n in sorted(self.slot_counts.items()):
            if sid in (BENCH, IR) or n <= 0:
                continue
            out += [sid] * n
        return out

    @property
    def roster_size(self) -> int:
        return sum(n for s, n in self.slot_counts.items() if s != IR)

    @property
    def ir_slots(self) -> int:
        return self.slot_counts.get(IR, 0)

    def period_of(self, week: int) -> int:
        for mp, weeks in self.matchup_periods.items():
            if week in weeks:
                return mp
        return week

    @property
    def playoff_weeks(self) -> list[int]:
        """Scoring weeks of the fantasy playoffs (all periods after the regular season)."""
        if not self.reg_periods or not self.matchup_periods:
            return []
        return sorted(w for mp, ws in self.matchup_periods.items() if mp > self.reg_periods for w in ws)

    def my_matchup(self, period: int) -> dict | None:
        for m in self.schedule:
            if m.get("matchupPeriodId") != period:
                continue
            h, a = m.get("home") or {}, m.get("away") or {}
            if h.get("teamId") == self.my_team_id:
                return {"me": h, "opp": a, "winner": m.get("winner"), "home": True}
            if a.get("teamId") == self.my_team_id:
                return {"me": a, "opp": h, "winner": m.get("winner"), "home": False}
        return None

    @property
    def faab_left(self) -> int:
        return max(0, self.faab_budget - self.me.faab_spent)


# ---------------------------------------------------------------------------
def _stats(player_json: dict, season: int, week: int) -> dict:
    out = {"week_proj": 0.0, "week_points": None, "proj_avg": 0.0, "actual_avg": 0.0, "games": 0}
    for s in player_json.get("stats", []) or []:
        if s.get("seasonId") != season:
            continue
        src, per, split = s.get("statSourceId"), s.get("scoringPeriodId"), s.get("statSplitTypeId")
        total = s.get("appliedTotal") or 0.0
        avg = s.get("appliedAverage")
        if per == week and split == 1:
            if src == 1:
                out["week_proj"] = float(total)
            elif src == 0:
                out["week_points"] = round(float(total), 1)
        elif per == 0 and split == 0:
            if src == 1:
                out["proj_avg"] = float(avg or 0.0)
            elif src == 0:
                out["actual_avg"] = float(avg or 0.0)
                if avg:
                    out["games"] = int(round(total / avg))
    return out


def parse_schedule(sched_json: dict, week: int) -> dict[int, tuple[float, int, bool]]:
    """pro team id -> (kickoff epoch seconds, opponent pro team id, is home) for this week."""
    games = {}
    for t in (sched_json or {}).get("settings", {}).get("proTeams", []):
        gl = (t.get("proGamesByScoringPeriod") or {}).get(str(week)) or []
        if gl:
            g = gl[0]
            home = g.get("homeProTeamId") == t["id"]
            opp = g.get("awayProTeamId") if home else g.get("homeProTeamId")
            games[t["id"]] = (g.get("date", 0) / 1000.0, opp, home)
    return games


def parse_player(pj: dict, season: int, week: int, games: dict, now: float, *,
                 slot=None, on_team_id=0, status="ONTEAM", pool: dict | None = None) -> Player:
    pool = pool or {}
    st = _stats(pj, season, week)
    pro_id = pj.get("proTeamId", 0)
    g = games.get(pro_id)
    kickoff, opp, home = (g if g else (None, None, True))
    locked = bool(pool.get("lineupLocked")) or (kickoff is not None and kickoff <= now)
    own = pj.get("ownership") or {}
    return Player(
        id=pj.get("id"), name=pj.get("fullName", "?"),
        pos=POS_NAMES.get(pj.get("defaultPositionId"), "?"), pos_id=pj.get("defaultPositionId", 0),
        pro_team=PRO_TEAMS.get(pro_id, "?"), pro_team_id=pro_id,
        eligible=list(pj.get("eligibleSlots", [])), injury=pj.get("injuryStatus") or "ACTIVE",
        slot=slot, on_team_id=on_team_id, status=status,
        owned=own.get("percentOwned", 0.0) or 0.0, started=own.get("percentStarted", 0.0) or 0.0,
        locked=locked, droppable=pj.get("droppable", True),
        game_time=kickoff, opponent=PRO_TEAMS.get(opp, "") if opp else "", home=home,
        sched_known=bool(games), **st,
    )


def parse_league(data: dict, sched: dict, my_team_id: int, season: int, now: float | None = None,
                 week: int | None = None) -> League:
    now = now or time.time()
    week = week or data.get("scoringPeriodId") or data.get("status", {}).get("currentMatchupPeriod") or 1
    games = parse_schedule(sched, week)
    st = data.get("settings", {})
    rs = st.get("rosterSettings", {})
    acq = st.get("acquisitionSettings", {})
    teams: dict[int, Team] = {}
    for t in data.get("teams", []):
        name = t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip() or f"Team {t['id']}"
        rec = (t.get("record") or {}).get("overall") or {}
        team = Team(id=t["id"], name=name, abbrev=t.get("abbrev", ""),
                    wins=rec.get("wins", 0), losses=rec.get("losses", 0), ties=rec.get("ties", 0),
                    points_for=rec.get("pointsFor", 0.0),
                    faab_spent=(t.get("transactionCounter") or {}).get("acquisitionBudgetSpent", 0),
                    acquisitions_by_period=(t.get("transactionCounter") or {}).get("matchupAcquisitionTotals") or {})
        for e in (t.get("roster") or {}).get("entries", []):
            pool = e.get("playerPoolEntry", {})
            pj = pool.get("player", {})
            if not pj:
                continue
            team.roster.append(parse_player(pj, season, week, games, now, slot=e.get("lineupSlotId"),
                                            on_team_id=t["id"], pool=pool))
        teams[t["id"]] = team
    if my_team_id not in teams:
        raise ValueError(f"Team {my_team_id} isn't in this league (teams: {sorted(teams)})")

    # this week's opponent
    period_for_week = None
    for mp, weeks in (st.get("scheduleSettings", {}).get("matchupPeriods") or {}).items():
        if week in weeks:
            period_for_week = int(mp)
    period_for_week = period_for_week or data.get("status", {}).get("currentMatchupPeriod") or week
    opp = None
    for m in data.get("schedule", []):
        if m.get("matchupPeriodId") != period_for_week:
            continue
        h, a = (m.get("home") or {}).get("teamId"), (m.get("away") or {}).get("teamId")
        if h == my_team_id:
            opp = a
        elif a == my_team_id:
            opp = h
    final_week = data.get("status", {}).get("finalScoringPeriod") or 18
    return League(
        id=data.get("id", 0), season=season, week=week, name=st.get("name", "League"), teams=teams,
        my_team_id=my_team_id,
        slot_counts={int(k): v for k, v in (rs.get("lineupSlotCounts") or {}).items()},
        position_limits={int(k): v for k, v in (rs.get("positionLimits") or {}).items()},
        faab=bool(acq.get("isUsingAcquisitionBudget")), faab_budget=acq.get("acquisitionBudget", 0),
        min_bid=acq.get("minimumBid", 0), scoring=st.get("scoringSettings", {}).get("scoringType", ""),
        opponent_id=opp, trade_deadline=(st.get("tradeSettings", {}).get("deadlineDate") or 0) / 1000.0,
        final_week=final_week, matchup_period=period_for_week,
        reg_periods=int(st.get("scheduleSettings", {}).get("matchupPeriodCount") or 0),
        matchup_periods={int(k): list(v) for k, v in (st.get("scheduleSettings", {}).get("matchupPeriods") or {}).items()},
        schedule=data.get("schedule", []) or [],
        ppr=next((float(i.get("points", 0)) for i in st.get("scoringSettings", {}).get("scoringItems", [])
                  if i.get("statId") == 53), 0.0),
        acq_limit_per_matchup=max(0, int(acq.get("matchupAcquisitionLimit") or 0)),
        raw_status=data.get("status", {}),
    )


def parse_free_agents(players_json: list[dict], season: int, week: int, games: dict, now: float | None = None) -> list[Player]:
    now = now or time.time()
    out = []
    for w in players_json:
        pj = w.get("player") or {}
        if not pj:
            continue
        p = parse_player(pj, season, week, games, now, status=w.get("status", "FREEAGENT"),
                         on_team_id=w.get("onTeamId", 0), pool=w)
        if w.get("rosterLocked"):
            p.locked = True
        out.append(p)
    return out


def season_schedule(sched_json: dict) -> tuple[dict, dict]:
    """({team abbrev: {week: opponent abbrev}}, {team abbrev: bye week}) for the whole NFL season."""
    opps: dict[str, dict[int, str]] = {}
    byes: dict[str, int] = {}
    for t in (sched_json or {}).get("settings", {}).get("proTeams", []):
        ab = PRO_TEAMS.get(t.get("id"), t.get("abbrev", "?"))
        if t.get("byeWeek"):
            byes[ab] = int(t["byeWeek"])
        for wk, games in (t.get("proGamesByScoringPeriod") or {}).items():
            if not games:
                continue
            g = games[0]
            other = g.get("awayProTeamId") if g.get("homeProTeamId") == t.get("id") else g.get("homeProTeamId")
            opps.setdefault(ab, {})[int(wk)] = PRO_TEAMS.get(other, "?")
    return opps, byes
