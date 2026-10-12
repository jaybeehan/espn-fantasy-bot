"""A fake league in ESPN's JSON format, for trying the page without an ESPN login (DEMO=1)."""
from __future__ import annotations

import random
import time

FIRST = ["Jalen", "Marcus", "Tyler", "DeShawn", "Cole", "Brandon", "Isaiah", "Kyle", "Andre", "Trey",
         "Josh", "Devin", "Malik", "Chris", "Jordan", "Derrick", "Zach", "Caleb", "Nate", "Rashad",
         "Logan", "Darius", "Evan", "Terrell", "Mason", "Quentin", "Aaron", "Bryce", "Garrett", "Omar"]
LAST = ["Whitfield", "Carver", "Holloway", "Pruitt", "Ambrose", "Kincaid", "Lockett", "Mabry", "Odom",
        "Rainey", "Sutton", "Tolliver", "Vance", "Winslow", "Yates", "Barlow", "Crenshaw", "Dorsey",
        "Easley", "Fairley", "Gaines", "Hargrove", "Ingram", "Jessup", "Kearse", "Ledbetter", "Mayfield",
        "Norwood", "Overstreet", "Pettis", "Quarles", "Redding", "Stallworth", "Truitt", "Underhill"]
PRO = {1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET", 9: "GB",
       10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO",
       19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB",
       28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU"}
ELIG = {1: [0, 7, 20, 21], 2: [2, 3, 23, 7, 20, 21], 3: [4, 3, 5, 23, 7, 20, 21],
        4: [6, 5, 23, 7, 20, 21], 5: [17, 20, 21], 16: [16, 20, 21]}
DEF_NAMES = {1: "Falcons", 2: "Bills", 3: "Bears", 4: "Bengals", 5: "Browns", 6: "Cowboys", 7: "Broncos",
             8: "Lions", 9: "Packers", 10: "Titans", 11: "Colts", 12: "Chiefs", 13: "Raiders", 14: "Rams",
             15: "Dolphins", 16: "Vikings", 17: "Patriots", 18: "Saints", 19: "Giants", 20: "Jets",
             21: "Eagles", 22: "Cardinals", 23: "Steelers", 24: "Chargers", 25: "49ers", 26: "Seahawks",
             27: "Buccaneers", 28: "Commanders", 29: "Panthers", 30: "Jaguars", 33: "Ravens", 34: "Texans"}
TEAM_NAMES = ["Hancock's Heroes", "Turf Toe Truckers", "The Waiver Wire", "Bye Week Blues",
              "Gridiron Gurus", "Fumble Factory", "Red Zone Rejects", "Sunday Scaries",
              "Hail Mary Hopefuls", "Taco Corp"]
SLOTS = {"0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 7, "21": 1, "23": 1}


class DemoLeague:
    def __init__(self, season: int, team_id: int = 1, seed: int = 7):
        self.season, self.team_id = season, team_id
        self.week = 6
        rnd = random.Random(seed)
        self.rnd = rnd
        now = time.time()
        # this week's kickoffs: two games already played (Thursday), the rest in the future
        teams = list(PRO)
        rnd.shuffle(teams)
        self.bye = set(teams[:4])
        # byes for the rest of the season: 4 teams a week, weeks 5-14
        self.bye_week = {}
        order = teams[4:] + teams[:4]
        for i, t in enumerate(order):
            self.bye_week[t] = 7 + i // 4 if i < 28 else self.week
        for t in teams[:4]:
            self.bye_week[t] = self.week
        # opponents for every week (random pairs among teams not on bye)
        self.season_games = {}
        for w in range(1, 18):
            pool = [t for t in PRO if self.bye_week.get(t) != w]
            rnd.shuffle(pool)
            for i in range(0, len(pool) - 1, 2):
                self.season_games.setdefault(w, {})[pool[i]] = pool[i + 1]
                self.season_games[w][pool[i + 1]] = pool[i]
        playing = teams[4:]
        self.games = {}
        times = [now - 2 * 86400] * 2 + [now + 3600 * 14] * 18 + [now + 3600 * 17] * 6 + [now + 3600 * 21] * 2 + [now + 86400 + 3600 * 21] * 2
        for i in range(0, len(playing), 2):
            a, b = playing[i], playing[i + 1]
            t = times[i]
            self.games[a] = (t, b, True)
            self.games[b] = (t, a, False)
        self.players = {}
        pid = 100
        used = set()

        def name():
            while True:
                n = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
                if n not in used:
                    used.add(n)
                    return n
        tiers = {1: (24, 9), 2: (20, 6), 3: (19, 6), 4: (14, 4), 5: (9, 1.5), 16: (8.5, 2)}
        counts = {1: 34, 2: 80, 3: 100, 4: 40, 5: 32}
        for pos, n in counts.items():
            top, spread = tiers[pos]
            for i in range(n):
                avg = max(1.0, top - (top * 0.85) * (i / n) + rnd.gauss(0, spread / 3))
                self._add(pid, name(), pos, rnd.choice(teams), avg)
                pid += 1
        for t, nm in DEF_NAMES.items():
            self._add(pid, f"{nm} D/ST", 16, t, max(3.0, rnd.gauss(7, 2)))
            pid += 1
        # injuries
        for p in rnd.sample(list(self.players.values()), 30):
            p["injuryStatus"] = rnd.choice(["QUESTIONABLE", "QUESTIONABLE", "OUT", "DOUBTFUL", "INJURY_RESERVE"])
        # snake draft 10 teams x 16
        by_value = sorted(self.players.values(), key=lambda p: -p["_avg"] * (0.55 if p["defaultPositionId"] in (5, 16) else 1))
        self.rosters = {t: [] for t in range(1, 11)}
        need = {t: {1: 2, 2: 5, 3: 5, 4: 2, 5: 1, 16: 1} for t in self.rosters}
        order = list(range(1, 11))
        rnum = 0
        pool = list(by_value)
        while any(len(r) < 16 for r in self.rosters.values()):
            seq = order if rnum % 2 == 0 else order[::-1]
            for t in seq:
                if len(self.rosters[t]) >= 16:
                    continue
                pick = next((p for p in pool if need[t].get(p["defaultPositionId"], 0) > 0), None) or pool[0]
                pool.remove(pick)
                need[t][pick["defaultPositionId"]] = need[t].get(pick["defaultPositionId"], 0) - 1
                self.rosters[t].append({"id": pick["id"], "slot": 20})
            rnum += 1
        # give every team a sensible (but not perfect) starting lineup
        for t, r in self.rosters.items():
            self._default_lineup(t)
        self.log = []

    def _add(self, pid, name, pos, team, avg):
        self.players[pid] = {"id": pid, "fullName": name, "defaultPositionId": pos, "proTeamId": team,
                             "eligibleSlots": ELIG[pos], "injuryStatus": "ACTIVE", "droppable": True, "_avg": avg}

    def _default_lineup(self, t):
        r = self.rosters[t]
        for e in r:
            e["slot"] = 20
        want = [0, 2, 2, 4, 4, 6, 16, 17, 23]
        ordered = sorted(r, key=lambda e: -self.players[e["id"]]["_avg"] + self.rnd.random() * 6)
        for s in want:
            for e in ordered:
                if e["slot"] == 20 and s in self.players[e["id"]]["eligibleSlots"]:
                    e["slot"] = s
                    break

    # ---- ESPN-shaped output ---------------------------------------------
    def _pjson(self, pid, w=None):
        p = dict(self.players[pid])
        avg = p.pop("_avg")
        y, w = self.season, (w or self.week)
        team = p["proTeamId"]
        wk = 0.0 if team in self.bye else max(0.0, avg + ((pid * 7919 + w * 104729) % 100 - 50) / 25.0)
        if p["injuryStatus"] in ("OUT", "INJURY_RESERVE"):
            wk = 0.0
        played = 5
        actual = avg + ((pid * 104723) % 100 - 50) / 20.0
        rank = sorted(self.players.values(), key=lambda q: -q["_avg"]).index(self.players[pid])
        p["ownership"] = {"percentOwned": max(0.1, 99.5 - rank * 0.35), "percentStarted": max(0.1, 90 - rank * 0.45)}
        p["stats"] = [
            {"seasonId": y, "scoringPeriodId": 0, "statSourceId": 1, "statSplitTypeId": 0,
             "appliedTotal": avg * 17, "appliedAverage": avg},
            {"seasonId": y, "scoringPeriodId": 0, "statSourceId": 0, "statSplitTypeId": 0,
             "appliedTotal": actual * played, "appliedAverage": actual},
            {"seasonId": y, "scoringPeriodId": w, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": round(wk, 2)},
        ]
        if w < self.week:  # a finished week: actual points
            p["stats"].append({"seasonId": y, "scoringPeriodId": w, "statSourceId": 0, "statSplitTypeId": 1,
                               "appliedTotal": round(max(0, wk + ((pid * 31) % 13 - 6)), 1)})
            return p
        g = self.games.get(team)
        if g and g[0] < time.time():
            p["stats"].append({"seasonId": y, "scoringPeriodId": w, "statSourceId": 0, "statSplitTypeId": 1,
                               "appliedTotal": round(wk * 1.1, 1)})
        return p

    def _locked(self, pid):
        g = self.games.get(self.players[pid]["proTeamId"])
        return bool(g and g[0] <= time.time())

    def current_week(self):
        return self.week

    def league(self, week=None):
        teams = []
        for t, r in self.rosters.items():
            w = (t * 37) % 5
            teams.append({
                "id": t, "name": TEAM_NAMES[t - 1], "abbrev": TEAM_NAMES[t - 1][:4].upper(),
                "record": {"overall": {"wins": w, "losses": 5 - w, "ties": 0, "pointsFor": 480 + t * 13.7}},
                "transactionCounter": {"acquisitionBudgetSpent": 12 * (t % 3), "matchupAcquisitionTotals": {}},
                "roster": {"entries": [{"playerId": e["id"], "lineupSlotId": e["slot"], "playerPoolEntry": {
                    "id": e["id"], "lineupLocked": self._locked(e["id"]), "rosterLocked": self._locked(e["id"]),
                    "player": self._pjson(e["id"], week)}} for e in r]},
            })
        sched = []
        ids = list(self.rosters)
        for i in range(0, 10, 2):
            sched.append({"matchupPeriodId": self.week, "home": {"teamId": ids[i]}, "away": {"teamId": ids[i + 1]},
                          "winner": "UNDECIDED"})
            a, b = 101.3 + i * 4.1, 96.8 + i * 3.3
            sched.append({"matchupPeriodId": self.week - 1, "home": {"teamId": ids[i], "totalPoints": a},
                          "away": {"teamId": ids[(i + 3) % 10], "totalPoints": b}, "winner": "HOME" if a > b else "AWAY"})
        return {
            "id": 534812081, "scoringPeriodId": self.week, "seasonId": self.season,
            "status": {"currentMatchupPeriod": self.week, "finalScoringPeriod": 17, "latestScoringPeriod": self.week},
            "settings": {"name": "Demo League (fake data)",
                         "rosterSettings": {"lineupSlotCounts": SLOTS,
                                            "positionLimits": {"1": 4, "2": 8, "3": 8, "4": 3, "5": 3, "16": 3}},
                         "acquisitionSettings": {"isUsingAcquisitionBudget": True, "acquisitionBudget": 100, "minimumBid": 0},
                         "scoringSettings": {"scoringType": "H2H_POINTS"},
                         "scheduleSettings": {"matchupPeriods": {str(w): [w] for w in range(1, 18)},
                                              "matchupPeriodCount": 14, "playoffTeamCount": 4},
                         "tradeSettings": {"deadlineDate": (time.time() + 30 * 86400) * 1000}},
            "teams": teams, "schedule": sched,
        }

    def pro_schedule(self):
        out = []
        for t in PRO:
            g = self.games.get(t)
            pg = {}
            for w, games in self.season_games.items():
                if w != self.week and t in games:
                    pg[str(w)] = [{"date": (time.time() + (w - self.week) * 7 * 86400) * 1000,
                                   "homeProTeamId": t, "awayProTeamId": games[t]}]
            if g:
                pg[str(self.week)] = [{"date": g[0] * 1000, "homeProTeamId": t if g[2] else g[1],
                                       "awayProTeamId": g[1] if g[2] else t}]
            out.append({"id": t, "abbrev": PRO[t], "byeWeek": self.bye_week.get(t, 0),
                        "proGamesByScoringPeriod": pg})
        return {"settings": {"proTeams": out}}

    def available_players(self, week, slot_ids=None, size=60):
        taken = {e["id"] for r in self.rosters.values() for e in r}
        free = [pid for pid in self.players if pid not in taken]
        free.sort(key=lambda pid: -self.players[pid]["_avg"])
        out = []
        for i, pid in enumerate(free[:size]):
            out.append({"id": pid, "onTeamId": 0, "status": "WAIVERS" if i % 7 == 3 else "FREEAGENT",
                        "lineupLocked": self._locked(pid), "rosterLocked": self._locked(pid),
                        "player": self._pjson(pid)})
        return out

    def trade_proposals(self, week):
        """One fake offer from team 3: one of their starters for two of mine."""
        mine = sorted(self.rosters[self.team_id], key=lambda e: -self.players[e["id"]]["_avg"])
        theirs = sorted(self.rosters[3], key=lambda e: -self.players[e["id"]]["_avg"])
        give = [e["id"] for e in mine if self.players[e["id"]]["defaultPositionId"] == 3][1:2]
        get = [e["id"] for e in theirs if self.players[e["id"]]["defaultPositionId"] == 2][1:2]
        items = [{"type": "TRADE", "playerId": p, "fromTeamId": self.team_id, "toTeamId": 3} for p in give]
        items += [{"type": "TRADE", "playerId": p, "fromTeamId": 3, "toTeamId": self.team_id} for p in get]
        return [{"id": "demo-offer-1", "type": "TRADE_PROPOSAL", "status": "PENDING", "teamId": 3,
                 "items": items, "expirationDate": (time.time() + 2 * 86400) * 1000}]

    # ---- writes (applied to the fake league) ------------------------------
    def set_lineup(self, week, moves):
        r = {e["id"]: e for e in self.rosters[self.team_id]}
        for pid, a, b in moves:
            if self._locked(pid):
                raise RuntimeError("player is locked")
            r[pid]["slot"] = b
        return {"ok": True}

    def add_drop(self, week, add_id, drop_id):
        r = self.rosters[self.team_id]
        if drop_id:
            r[:] = [e for e in r if e["id"] != drop_id]
        r.append({"id": add_id, "slot": 20})
        return {"ok": True}

    def waiver_claim(self, week, add_id, drop_id, bid=0):
        return {"ok": True, "pending": True}


def demo_extras(dl: "DemoLeague", lg):
    """Fake second opinions so the page shows what the real sources look like."""
    from sources import Extras, TeamEnv
    ex = Extras()
    rnd = random.Random(lg.week * 13)
    players = list(lg.me.roster) + [p for t in lg.teams.values() for p in t.roster]
    for pid, p in dl.players.items():
        base = p["_avg"]
        if p["defaultPositionId"] == 16:
            ex.sleeper_def[PRO[p["proTeamId"]]] = round(base + rnd.uniform(-2, 2), 1)
            ex.recent_def[PRO[p["proTeamId"]]] = [round(base + rnd.uniform(-5, 5), 1) for _ in range(3)]
        else:
            ex.sleeper_proj[pid] = round(max(0, base + rnd.uniform(-3, 3)), 1)
            ex.recent[pid] = [round(max(0, base + rnd.uniform(-7, 7)), 1) for _ in range(3)]
    taken = {e["id"] for r in dl.rosters.values() for e in r}
    free = sorted((pid for pid in dl.players if pid not in taken), key=lambda i: -dl.players[i]["_avg"])
    for i, pid in enumerate(free[:20]):
        ex.trending[pid] = 9000 - i * 400
    seen = set()
    for t, (ko, opp, home) in dl.games.items():
        if t in seen:
            continue
        seen.update({t, opp})
        total = rnd.choice([38.5, 41.5, 44.5, 47.5, 51.5])
        spread = rnd.choice([1.5, 3, 3.5, 6.5, 9.5])
        a, b = PRO[t], PRO[opp]
        ex.env[a] = TeamEnv(implied=total / 2 + spread / 2, opp_implied=total / 2 - spread / 2, total=total, spread=-spread)
        ex.env[b] = TeamEnv(implied=total / 2 - spread / 2, opp_implied=total / 2 + spread / 2, total=total, spread=spread)
        if rnd.random() < 0.2:
            for x in (a, b):
                ex.env[x].wind_mph, ex.env[x].precip_mm = 22.0, 0.4
    ex.used = ["Sleeper projections", "recent form", "trending adds", "Vegas lines", "weather"]
    return ex


def demo_allowed(opps):
    """Fake 'points allowed by defense' table so playoff planning shows up in demo mode."""
    rnd = random.Random(99)
    base = {"QB": 18.0, "RB": 24.0, "WR": 33.0, "TE": 9.0, "K": 8.0, "DEF": 7.0}
    allowed = {t: {pos: v * rnd.uniform(0.75, 1.25) for pos, v in base.items()} for t in PRO.values()}
    return allowed, base, 5
