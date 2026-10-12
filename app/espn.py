"""Talks to ESPN's fantasy football API (the same unofficial API the ESPN website uses).

Reads go to lm-api-reads, changes (lineup moves, adds/drops, waiver claims) go to
lm-api-writes. Private leagues need the two login cookies espn_s2 and SWID.
"""
from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger("espn")

READS = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
WRITES = "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json",
    "Origin": "https://fantasy.espn.com",
    "Referer": "https://fantasy.espn.com/",
    "X-Fantasy-Source": "kona",
    "X-Fantasy-Platform": "kona-PROD",
}


class EspnError(Exception):
    pass


class AuthError(EspnError):
    """Cookies missing, expired, or not allowed to see/change this league."""


class EspnClient:
    def __init__(self, league_id: int, team_id: int, season: int, espn_s2: str, swid: str):
        self.league_id = int(league_id)
        self.team_id = int(team_id)
        self.season = int(season)
        swid = (swid or "").strip()
        if swid and not swid.startswith("{"):
            swid = "{" + swid.strip("{}") + "}"
        self.swid = swid
        self.cookies = {"espn_s2": (espn_s2 or "").strip(), "SWID": swid}
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    # ---- low level -------------------------------------------------------
    @property
    def league_url(self) -> str:
        return f"{READS}/seasons/{self.season}/segments/0/leagues/{self.league_id}"

    def _get(self, url, params=None, fantasy_filter=None):
        headers = {}
        if fantasy_filter is not None:
            headers["X-Fantasy-Filter"] = json.dumps(fantasy_filter)
        for attempt in range(3):
            try:
                r = self.s.get(url, params=params, headers=headers, cookies=self.cookies, timeout=30)
            except requests.RequestException as e:
                if attempt == 2:
                    raise EspnError(f"Couldn't reach ESPN: {e}") from e
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (401, 403):
                raise AuthError("ESPN refused access. The espn_s2 / SWID codes are missing or expired.")
            if r.status_code == 404:
                raise EspnError("ESPN says this league doesn't exist. Check LEAGUE_ID and SEASON.")
            if r.status_code >= 500 and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise EspnError(f"ESPN returned HTTP {r.status_code}")
            data = r.json()
            return data[0] if isinstance(data, list) and data else data
        raise EspnError("ESPN didn't answer")

    # ---- reads -----------------------------------------------------------
    def league(self, week: int | None = None) -> dict:
        """Settings, every team, every roster (with this week's projections) and matchups."""
        params = [("view", v) for v in ("mSettings", "mTeam", "mRoster", "mMatchup", "mStatus")]
        if week:
            params.append(("scoringPeriodId", week))
        return self._get(self.league_url, params=params)

    def current_week(self) -> int:
        d = self._get(self.league_url, params={"view": "mStatus"})
        st = d.get("status", {})
        return int(d.get("scoringPeriodId") or st.get("latestScoringPeriod") or st.get("currentMatchupPeriod") or 1)

    def pro_schedule(self) -> dict:
        return self._get(f"{READS}/seasons/{self.season}", params={"view": "proTeamSchedules_wl"})

    def available_players(self, week: int, slot_ids: list[int] | None = None, size: int = 60) -> list[dict]:
        """Free agents and players on waivers, most-owned first, with season and week stats."""
        y = self.season
        f = {"players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "limit": size,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            "filterStatsForTopScoringPeriodIds": {
                "value": 2, "additionalValue": [f"00{y}", f"10{y}", f"11{y}{week}", f"01{y}{week}"]},
        }}
        if slot_ids:
            f["players"]["filterSlotIds"] = {"value": slot_ids}
        data = self._get(self.league_url, params={"view": "kona_player_info", "scoringPeriodId": week},
                         fantasy_filter=f)
        return data.get("players", []) if isinstance(data, dict) else []

    # ---- writes ----------------------------------------------------------
    def _transaction(self, payload: dict) -> dict:
        url = f"{WRITES}/seasons/{self.season}/segments/0/leagues/{self.league_id}/transactions/"
        try:
            r = self.s.post(url, json=payload, cookies=self.cookies, timeout=30,
                            headers={"Content-Type": "application/json"})
        except requests.RequestException as e:
            raise EspnError(f"Couldn't reach ESPN: {e}") from e
        if r.status_code in (401, 403):
            raise AuthError("ESPN refused the change. The espn_s2 / SWID codes are missing or expired.")
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:300]}
        if r.status_code >= 300:
            msg = ""
            if isinstance(body, dict):
                det = body.get("details") or body.get("messages") or []
                if det and isinstance(det, list):
                    msg = "; ".join(str(d.get("message", d)) if isinstance(d, dict) else str(d) for d in det)
            raise EspnError(f"ESPN rejected the change (HTTP {r.status_code}) {msg}".strip())
        return body

    def _base(self, kind: str, week: int) -> dict:
        return {"isLeagueManager": False, "teamId": self.team_id, "type": kind,
                "memberId": self.swid, "scoringPeriodId": week, "executionType": "EXECUTE"}

    def set_lineup(self, week: int, moves: list[tuple[int, int, int]]) -> dict:
        """moves = [(playerId, fromSlotId, toSlotId), ...], sent together as one change."""
        p = self._base("ROSTER", week)
        p["items"] = [{"playerId": pid, "type": "LINEUP", "fromLineupSlotId": a, "toLineupSlotId": b}
                      for pid, a, b in moves]
        return self._transaction(p)

    def add_drop(self, week: int, add_id: int, drop_id: int | None) -> dict:
        p = self._base("FREEAGENT", week)
        p["items"] = [{"playerId": add_id, "type": "ADD", "toTeamId": self.team_id}]
        if drop_id:
            p["items"].append({"playerId": drop_id, "type": "DROP", "fromTeamId": self.team_id})
        return self._transaction(p)

    def waiver_claim(self, week: int, add_id: int, drop_id: int | None, bid: int = 0) -> dict:
        p = self._base("WAIVER", week)
        p["bidAmount"] = int(bid)
        p["items"] = [{"playerId": add_id, "type": "ADD", "toTeamId": self.team_id}]
        if drop_id:
            p["items"].append({"playerId": drop_id, "type": "DROP", "fromTeamId": self.team_id})
        return self._transaction(p)
