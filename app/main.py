"""ESPN fantasy football autopilot: background loop + SQLite log + small web page."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from functools import wraps

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import brain
from demo import DemoLeague
from espn import AuthError, EspnClient, EspnError
from model import BENCH, IR, SLOT_NAMES, parse_free_agents, parse_league, parse_schedule, season_schedule
from sources import Sources, adjust

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")


def _default_season() -> int:
    t = time.localtime()
    return t.tm_year if t.tm_mon >= 3 else t.tm_year - 1


# ---- configuration (environment variables) --------------------------------
DEMO = os.environ.get("DEMO", "0") == "1"
LEAGUE_ID = int(os.environ.get("LEAGUE_ID", "0") or 0)
TEAM_ID = int(os.environ.get("TEAM_ID", "0") or 0)
SEASON = int(os.environ.get("SEASON", "0") or 0) or _default_season()
POLL_MINUTES = max(5, int(os.environ.get("POLL_MINUTES", "15")))
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
NTFY_URL = os.environ.get("NTFY_URL", "")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "fantasy.db")

DEFAULTS = {
    "auto_lineup": True,       # make lineup changes automatically
    "auto_fa": True,           # make free-agent adds/drops automatically
    "stream_kdst": True,
    "upgrades": True,
    "use_ir": True,
    "max_adds_week": 2,
    "questionable": "cautious",  # start / cautious / sit
    "faab_pct": 5,
    "notify_moves": True,
    "notify_trades": True,
    "notify_offers": True,     # push a verdict when someone sends me a trade
    "notify_recap": True,      # weekly recap push
    "bye_cover": True,         # pick up cover a few days early when byes leave a hole
    "playoff_plan": True,      # weigh fantasy-playoff matchups from mid-season
    "protected": [],
    "espn_s2": os.environ.get("ESPN_S2", ""),
    "swid": os.environ.get("SWID", ""),
}
Q_FACTORS = {"start": 1.0, "cautious": 0.85, "sit": 0.0}

# ---- database ---------------------------------------------------------------
_db_lock = threading.Lock()


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _db_lock, db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, season INTEGER, week INTEGER,
            kind TEXT, text TEXT, player_id INTEGER, ok INTEGER DEFAULT 1, auto INTEGER DEFAULT 1);
        CREATE INDEX IF NOT EXISTS actions_ts ON actions(ts);
        CREATE TABLE IF NOT EXISTS marks (season INTEGER, week INTEGER, kind TEXT, pid INTEGER,
            PRIMARY KEY (season, week, kind, pid));
        """)


def meta_get(k, default=None):
    with db() as con:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def meta_set(k, v):
    with _db_lock, db() as con:
        con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, json.dumps(v)))


def settings() -> dict:
    s = dict(DEFAULTS)
    s.update(meta_get("settings", {}) or {})
    return s


def log_action(week, kind, text, pid=None, ok=True, auto=True):
    log.info("[%s] %s", kind, text)
    with _db_lock, db() as con:
        con.execute("INSERT INTO actions (ts, season, week, kind, text, player_id, ok, auto) VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), SEASON, week, kind, text, pid, int(ok), int(auto)))


def mark(week, kind, pid):
    with _db_lock, db() as con:
        con.execute("INSERT OR IGNORE INTO marks VALUES (?,?,?,?)", (SEASON, week, kind, pid))


def marks(week, kind) -> set:
    with db() as con:
        return {r["pid"] for r in con.execute("SELECT pid FROM marks WHERE season=? AND week=? AND kind=?",
                                               (SEASON, week, kind))}


def adds_this_week(week) -> int:
    with db() as con:
        return con.execute("SELECT COUNT(*) n FROM actions WHERE season=? AND week=? AND kind IN ('add','claim') "
                           "AND ok=1 AND auto=1", (SEASON, week)).fetchone()["n"]


def recent_adds() -> set:
    with db() as con:
        return {r["player_id"] for r in con.execute(
            "SELECT player_id FROM actions WHERE kind IN ('add','claim') AND ok=1 AND ts>?", (time.time() - 7 * 86400,))}


# ---- notifications ----------------------------------------------------------
def notify(title, message, tags="football", priority="default", dedupe_key=None, every=12 * 3600):
    if not NTFY_URL:
        return False
    if dedupe_key:
        k = "ntfy:" + hashlib.sha1(dedupe_key.encode()).hexdigest()[:16]
        last = meta_get(k, 0)
        if time.time() - last < every:
            return False
        meta_set(k, time.time())
    try:
        requests.post(NTFY_URL, data=message.encode("utf-8"), timeout=10,
                      headers={"Title": title.encode("utf-8"), "Tags": tags, "Priority": priority})
        return True
    except requests.RequestException as e:
        log.warning("ntfy failed: %s", e)
        return False


# ---- ESPN source ------------------------------------------------------------
_demo: DemoLeague | None = None


def source(cfg):
    global _demo
    if DEMO:
        if _demo is None:
            _demo = DemoLeague(SEASON, TEAM_ID or 1)
        return _demo
    if not LEAGUE_ID or not TEAM_ID:
        raise EspnError("LEAGUE_ID and TEAM_ID aren't set in the compose file.")
    if not cfg.get("espn_s2") or not cfg.get("swid"):
        raise AuthError("Add your ESPN codes (espn_s2 and SWID) on the Settings tab.")
    return EspnClient(LEAGUE_ID, TEAM_ID, SEASON, cfg["espn_s2"], cfg["swid"])


_sched_cache = {"day": None, "data": None}


def pro_schedule(src):
    day = time.strftime("%Y-%m-%d")
    if _sched_cache["day"] != day or DEMO:
        try:
            _sched_cache["data"] = src.pro_schedule()
            _sched_cache["day"] = day
        except EspnError as e:
            log.warning("schedule: %s", e)
            if _sched_cache["data"] is None:
                _sched_cache["data"] = {}
    return _sched_cache["data"]


EXTRA = Sources(DATA_DIR)
USE_EXTRAS = os.environ.get("EXTRA_SOURCES", "1") != "0"


def extras_for(lg):
    """Sleeper, Vegas, weather, recent form. Cached; any piece can fail independently."""
    key = "pts_ppr" if lg.ppr >= 1 else "pts_half_ppr" if lg.ppr >= 0.5 else "pts_std"
    if DEMO:
        from demo import demo_extras
        return demo_extras(_demo, lg)
    try:
        return EXTRA.load(SEASON, lg.week, key)
    except Exception as e:
        log.warning("extra sources: %s", e)
        return None


_allowed_cache = {"key": None, "data": None}


def defense_table(lg, opps):
    """Points each NFL defense allows per position (for playoff schedule strength). Once a day."""
    key = (SEASON, lg.week, time.strftime("%Y-%m-%d"))
    if _allowed_cache["key"] != key:
        if DEMO:
            from demo import demo_allowed
            _allowed_cache["data"] = demo_allowed(opps)
        else:
            sk = "pts_ppr" if lg.ppr >= 1 else "pts_half_ppr" if lg.ppr >= 0.5 else "pts_std"
            try:
                _allowed_cache["data"] = EXTRA.points_allowed(SEASON, lg.week, sk, opps)
            except Exception as e:
                log.warning("points allowed: %s", e)
                _allowed_cache["data"] = ({}, {}, 0)
        _allowed_cache["key"] = key
    return _allowed_cache["data"]


def apply_playoff_plan(lg, fas, cfg, opps, byes):
    state["playoff"] = None
    if not cfg.get("playoff_plan") or not lg.playoff_weeks:
        return
    w = brain.playoff_weight(lg)
    future = [x for x in lg.playoff_weeks if x > lg.week]
    if w <= 0 or not future:
        state["playoff"] = {"weeks": lg.playoff_weeks, "weight": 0, "active": False}
        return
    allowed, avg, nweeks = defense_table(lg, opps)
    if nweeks < 3 and not DEMO:
        allowed, avg = {}, {}  # too little data: only byes count
    players = [p for t in lg.teams.values() for p in t.roster] + list(fas)
    for p in players:
        f, note = brain.playoff_factor(p, lg, opps, byes, allowed, avg)
        p.sos_factor = round(1 + w * (f - 1), 3)
        if note:
            p.notes.append(note)
    state["playoff"] = {"weeks": lg.playoff_weeks, "weight": w, "active": True}


def load(src, cfg=None):
    cfg = cfg or settings()
    week = src.current_week()
    data = src.league(week)
    sched = pro_schedule(src)
    lg = parse_league(data, sched, TEAM_ID or 1, SEASON)
    games = parse_schedule(sched, lg.week)
    opps, byes = season_schedule(sched)
    state["byes"], state["opps"] = byes, opps
    state["kickoffs"] = sorted({g[0] for g in games.values() if g[0]})
    fas = parse_free_agents(src.available_players(lg.week, size=100), SEASON, lg.week, games)
    ex = extras_for(lg) if USE_EXTRAS else None
    if ex:
        for t in lg.teams.values():
            for p in t.roster:
                adjust(p, ex)
        for p in fas:
            adjust(p, ex)
        state["sources"] = {"used": ["ESPN projections"] + ex.used, "failed": ex.failed}
        ranked = sorted(ex.trending.items(), key=lambda kv: -kv[1])[:15]
        state["hot"] = {pid for pid, _ in ranked}
    else:
        state["sources"] = {"used": ["ESPN projections"], "failed": {} if not USE_EXTRAS else {"extras": "unavailable"}}
        state["hot"] = set()
    try:
        apply_playoff_plan(lg, fas, cfg, opps, byes)
    except Exception:
        log.exception("playoff planning")
    return lg, fas


def options(cfg, week) -> brain.Options:
    return brain.Options(
        q_factor=Q_FACTORS.get(cfg.get("questionable"), 0.85),
        max_adds_week=int(cfg.get("max_adds_week", 2)),
        stream_kdst=bool(cfg.get("stream_kdst")), upgrades=bool(cfg.get("upgrades")),
        use_ir=bool(cfg.get("use_ir")), faab_pct=float(cfg.get("faab_pct", 5)) / 100.0,
        protected=set(cfg.get("protected") or []), recent_adds=recent_adds(),
        claimed=marks(week, "claim"),
        failed={(k, p) for k in ("add", "ir", "lineup") for p in marks(week, "fail-" + k)},
        hot=set(state.get("hot") or ()),
    )


# ---- one cycle --------------------------------------------------------------
state = {"last_run": None, "next_run": None, "error": None, "running": False, "snapshot": None,
         "trades": [], "trades_ts": 0, "sources": None, "hot": set(), "byes": {}, "opps": {},
         "kickoffs": [], "offers": [], "offers_ts": 0, "offers_error": None, "playoff": None}
_run_lock = threading.Lock()
_run_now = threading.Event()


def _upgrade_window(lg) -> bool:
    """Season-long upgrades only when no game starts within 3 hours, and once a day."""
    now = time.time()
    soon = [p.game_time for p in lg.me.roster if p.game_time and now - 4 * 3600 < p.game_time < now + 3 * 3600]
    if soon:
        return False
    return now - (meta_get("last_upgrade_ts", 0) or 0) > 20 * 3600


def run_cycle():
    if not _run_lock.acquire(blocking=False):
        return
    state["running"] = True
    try:
        _cycle()
        state["error"] = None
    except AuthError as e:
        state["error"] = str(e)
        notify("ESPN login needs attention", f"{e} Nothing can change until it's fixed.", tags="warning",
               dedupe_key="auth", every=24 * 3600)
    except Exception as e:  # keep the loop alive no matter what
        log.exception("cycle failed")
        state["error"] = f"{type(e).__name__}: {e}"
        notify("Fantasy bot problem", state["error"], tags="warning", dedupe_key="err:" + state["error"][:80])
    finally:
        state["last_run"] = time.time()
        state["running"] = False
        _run_lock.release()


def _cycle():
    cfg = settings()
    src = source(cfg)
    lg, fas = load(src, cfg)
    week = lg.week
    if not meta_get("first_week"):
        meta_set("first_week", week)
    if week > lg.final_week:
        state["snapshot"] = snapshot(lg, fas, cfg, None, [])
        return
    o = options(cfg, week)
    changed = False

    # 1) free agents first (they can fix a hole the lineup can't)
    allow_up = _upgrade_window(lg)
    fa_moves = brain.plan_free_agents(lg, fas, o, adds_this_week(week), allow_up)
    suggestions = []
    for m in fa_moves:
        if not cfg.get("auto_fa"):
            suggestions.append(m.reason.replace("Added", "Add").replace("Claimed", "Claim").replace("dropped", "drop"))
            continue
        try:
            if m.kind == "WAIVER":
                src.waiver_claim(week, m.add.id, m.drop.id if m.drop else None, m.bid)
                mark(week, "claim", m.add.id)
                log_action(week, "claim", m.reason, m.add.id)
            else:
                src.add_drop(week, m.add.id, m.drop.id if m.drop else None)
                log_action(week, "add", m.reason, m.add.id)
                changed = True
            if m.upgrade:
                meta_set("last_upgrade_ts", time.time())
            if cfg.get("notify_moves"):
                notify("Roster move", m.reason, tags="arrows_counterclockwise")
        except EspnError as e:
            mark(week, "fail-add", m.add.id)
            log_action(week, "error", f"Couldn't add {m.add.name}: {e}", m.add.id, ok=False)
            notify("Pickup failed", f"Couldn't add {m.add.name}: {e}", tags="warning", dedupe_key=f"fa{m.add.id}")
    if allow_up and not fa_moves:
        meta_set("last_upgrade_ts", time.time())  # looked today, nothing worth doing

    # 1b) bye-week cover for next week (Tue-Fri, away from game time)
    if cfg.get("bye_cover") and not fa_moves and _bye_window(lg):
        for m in brain.plan_bye_cover(lg, fas, o, state["byes"], adds_this_week(week)):
            if not cfg.get("auto_fa"):
                suggestions.append(m.reason.replace("added", "add").replace("dropped", "drop"))
                continue
            try:
                src.add_drop(week, m.add.id, m.drop.id if m.drop else None)
                log_action(week, "add", m.reason, m.add.id)
                changed = True
                if cfg.get("notify_moves"):
                    notify("Bye-week cover", m.reason, tags="calendar")
            except EspnError as e:
                mark(week, "fail-add", m.add.id)
                log_action(week, "error", f"Couldn't add {m.add.name}: {e}", m.add.id, ok=False)
    if changed:
        lg, fas = load(src, cfg)
        o = options(cfg, week)

    # 2) lineup
    plan = brain.plan_lineup(lg, o)
    by = {p.id: p for p in lg.me.roster}
    if cfg.get("auto_lineup"):
        if plan.ir_moves:
            try:
                src.set_lineup(week, plan.ir_moves)
                for pid, _, _ in plan.ir_moves:
                    log_action(week, "ir", f"Moved {by[pid].name} to IR.", pid)
                changed = True
            except EspnError as e:
                for pid, _, _ in plan.ir_moves:
                    mark(week, "fail-ir", pid)
                log_action(week, "error", f"ESPN wouldn't move to IR: {e}", ok=False)
        if plan.moves:
            try:
                src.set_lineup(week, plan.moves)
                text = " ".join(r for r in plan.reasons if not r.startswith("Moved")) or "Rearranged lineup."
                text += f" Projected {plan.before:.1f} → {plan.after:.1f}."
                log_action(week, "lineup", text)
                changed = True
                if cfg.get("notify_moves") and plan.after - plan.before >= 0.5:
                    notify("Lineup updated", text, tags="football")
            except EspnError as e:
                log_action(week, "error", f"ESPN rejected the lineup change: {e}", ok=False)
                notify("Lineup change failed", str(e), tags="warning", dedupe_key="lineup" + str(week), every=6 * 3600)
        if changed:
            lg, fas = load(src, cfg)
            plan = brain.plan_lineup(lg, o)
    elif plan.moves or plan.ir_moves:
        suggestions += [r.replace("Benched", "Bench").replace("Started", "Start").replace("Moved", "Move")
                        for r in plan.reasons]
    for w in plan.warnings:
        notify("Roster needs attention", w, tags="warning", dedupe_key=w, every=24 * 3600)
    if suggestions:
        key = "sugg" + str(week) + "|".join(suggestions)
        notify("Suggested moves", "\n".join(suggestions), tags="bulb", dedupe_key=key, every=7 * 86400)

    # 3) trade offers sent to me (every ~20 minutes)
    if time.time() - state["offers_ts"] > 20 * 60:
        check_offers(src, lg, o, cfg)

    # 4) weekly recap once last week is final
    try:
        maybe_recap(src, lg, o, cfg)
    except Exception:
        log.exception("recap")

    # 5) trade ideas (every 6 hours is plenty)
    if time.time() - state["trades_ts"] > 6 * 3600 or changed or not state["trades"]:
        try:
            state["trades"] = brain.trade_ideas(lg, o)
        except Exception:
            log.exception("trade ideas")
        state["trades_ts"] = time.time()
        lt = time.localtime()
        if cfg.get("notify_trades") and state["trades"] and lt.tm_wday == 1 and lt.tm_hour >= 9:
            t = state["trades"][0]
            notify("Trade idea", f"Offer {', '.join(p['name'] for p in t['give'])} to {t['team']} for "
                   f"{', '.join(p['name'] for p in t['get'])}. {t['why']}", tags="handshake",
                   dedupe_key=f"trade-week{week}", every=6 * 86400)
    state["snapshot"] = snapshot(lg, fas, cfg, plan, suggestions)


def _bye_window(lg) -> bool:
    lt = time.localtime()
    if lt.tm_wday not in (1, 2, 3, 4):  # Tue-Fri
        return False
    now = time.time()
    return not any(p.game_time and now - 4 * 3600 < p.game_time < now + 3 * 3600 for p in lg.me.roster)


def check_offers(src, lg, o, cfg):
    state["offers_ts"] = time.time()
    try:
        raw = src.trade_proposals(lg.week)
        state["offers_error"] = None
    except AuthError:
        raise
    except Exception as e:
        state["offers_error"] = str(e)[:160]
        log.warning("trade offers: %s", e)
        return
    offers = []
    me_id = lg.my_team_id
    for t in raw:
        items = [i for i in t.get("items") or [] if i.get("playerId")]
        give = [i["playerId"] for i in items if i.get("fromTeamId") == me_id]
        get = [i["playerId"] for i in items if i.get("toTeamId") == me_id]
        others = {i.get("fromTeamId") for i in items} | {i.get("toTeamId") for i in items}
        others.discard(me_id)
        others.discard(None)
        if not others:
            continue
        other = others.pop()
        ev = brain.evaluate_offer(lg, o, other, give, get)
        ev["id"] = t.get("id")
        ev["incoming"] = t.get("teamId") != me_id
        ev["expires"] = (t.get("expirationDate") or 0) / 1000 or None
        offers.append(ev)
        if ev["incoming"] and cfg.get("notify_offers") and ev["verdict"] != "unknown":
            gv = ", ".join(p["name"] for p in ev["give"]) or "nothing"
            gt = ", ".join(p["name"] for p in ev["get"]) or "nothing"
            icon = {"accept": "white_check_mark", "close": "scales", "decline": "x"}.get(ev["verdict"], "handshake")
            sent = notify(f"Trade offer from {ev['team']}", f"You give: {gv}\nYou get: {gt}\n{ev['why']}\n"
                          "Accept or decline in the ESPN app.", tags=icon, priority="high",
                          dedupe_key=f"offer-{ev['id']}", every=30 * 86400)
            if sent:
                log_action(lg.week, "offer", f"Trade offer from {ev['team']}: you give {gv}, get {gt}. {ev['why']}")
    state["offers"] = offers


def maybe_recap(src, lg, o, cfg):
    prev = lg.week - 1
    if prev < 1 or meta_get("recap_done") == f"{SEASON}-{prev}":
        return
    period = lg.period_of(prev)
    weeks = lg.matchup_periods.get(period) or [prev]
    if prev != max(weeks):  # two-week playoff rounds: recap at the end of the round
        meta_set("recap_done", f"{SEASON}-{prev}")
        return
    m = lg.my_matchup(period)
    if not m or m.get("winner") in (None, "", "UNDECIDED"):
        return
    my_pts, opp_pts = float(m["me"].get("totalPoints") or 0), float(m["opp"].get("totalPoints") or 0)
    opp = lg.teams.get(m["opp"].get("teamId"))
    res = "Won" if my_pts > opp_pts else "Lost" if my_pts < opp_pts else "Tied"
    lines = [f"{res} {my_pts:.1f}–{opp_pts:.1f}" + (f" vs {opp.name}" if opp else "") + "."]
    # points left on the bench (by actual points)
    try:
        old = parse_league(src.league(prev), pro_schedule(src), TEAM_ID or 1, SEASON, week=prev)
        ros = [p for p in old.me.roster if p.slot != IR]
        pts = lambda p: p.week_points or 0.0
        started = sum(pts(p) for p in ros if p.slot not in (BENCH, IR))
        picks, bench = brain.fill(old.starting_slots, ros, key=pts)
        best = sum(pts(p) for p in picks.values() if p)
        left = best - started
        top = max(ros, key=pts, default=None)
        if top and pts(top) > 0:
            lines.append(f"Top scorer: {top.name} {pts(top):.1f}.")
        if left >= 0.5:
            missed = max((p for p in ros if p.slot == BENCH), key=pts, default=None)
            lines.append(f"Left {left:.1f} on the bench" + (f" ({missed.name} scored {pts(missed):.1f})." if missed else "."))
        else:
            lines.append("Best possible lineup was started.")
    except Exception as e:
        log.warning("recap bench check: %s", e)
    with db() as con:
        rows = con.execute("SELECT kind, COUNT(*) n FROM actions WHERE season=? AND week IN (%s) AND ok=1 AND auto=1 "
                           "GROUP BY kind" % ",".join("?" * len(weeks)), (SEASON, *weeks)).fetchall()
    c = {r["kind"]: r["n"] for r in rows}
    did = []
    if c.get("lineup"):
        did.append(f"{c['lineup']} lineup change{'s' if c['lineup'] > 1 else ''}")
    if c.get("add") or c.get("claim"):
        n = c.get("add", 0) + c.get("claim", 0)
        did.append(f"{n} pickup{'s' if n > 1 else ''}")
    if (meta_get("first_week") or 0) <= prev:  # skip for weeks before the bot was running
        lines.append("Bot made " + (" and ".join(did) if did else "no changes") + ".")
    me = lg.me
    standings = sorted(lg.teams.values(), key=lambda t: (t.wins, t.points_for), reverse=True)
    rank = [t.id for t in standings].index(me.id) + 1
    lines.append(f"Record {me.wins}-{me.losses}{'-' + str(me.ties) if me.ties else ''}, {rank} of {len(lg.teams)}.")
    text = " ".join(lines)
    meta_set("recap_done", f"{SEASON}-{prev}")
    log_action(prev, "recap", f"Week {prev}: {text}")
    if cfg.get("notify_recap"):
        notify(f"Week {prev} recap: {res}", text, tags="bar_chart", dedupe_key=f"recap-{SEASON}-{prev}",
               every=30 * 86400)


# ---- what the page shows ----------------------------------------------------
def snapshot(lg, fas, cfg, plan, suggestions):
    o = options(cfg, lg.week)
    me = lg.me
    roster = []
    for p in sorted(me.roster, key=lambda p: (brain.slot_sort_key(p.slot) if p.slot not in (BENCH, IR) else 100 + (p.slot == IR),
                                              -brain.score(p, o))):
        d = p.to_dict()
        d["adj"] = round(brain.score(p, o), 1)
        d["protected"] = p.id in o.protected
        d["starting"] = p.slot not in (BENCH, IR)
        roster.append(d)
    opp = lg.teams.get(lg.opponent_id) if lg.opponent_id else None
    def live(team):
        """Points already scored by started players + projections for those still to play."""
        tot = 0.0
        for p in team.roster:
            if p.slot in (BENCH, IR):
                continue
            tot += p.week_points if (p.locked and p.week_points is not None) else brain.score(p, o)
        return round(tot, 1)
    opp_proj = live(opp) if opp else None
    my_proj = live(me)
    rostered = {p.id for t in lg.teams.values() for p in t.roster}
    avail = [f for f in fas if f.id not in rostered]
    by_pos = []
    for pos in ("QB", "RB", "WR", "TE", "K", "D/ST"):
        key = (lambda p: (brain.score(p, o), p.value)) if pos in brain.KICK_DEF else (lambda p: (p.value, p.week_proj))
        lst = sorted([f for f in avail if f.pos == pos], key=key, reverse=True)[:8]
        by_pos.append([pos, [dict(f.to_dict(), adj=round(brain.score(f, o), 1)) for f in lst]])
    standings = sorted(lg.teams.values(), key=lambda t: (t.wins, t.points_for), reverse=True)
    return {
        "league": lg.name, "season": lg.season, "week": lg.week, "team": me.name,
        "record": f"{me.wins}-{me.losses}" + (f"-{me.ties}" if me.ties else ""),
        "rank": [t.id for t in standings].index(me.id) + 1, "teams": len(lg.teams),
        "opponent": opp.name if opp else None, "opp_proj": opp_proj, "my_proj": my_proj,
        "faab": lg.faab, "faab_left": lg.faab_left if lg.faab else None,
        "roster": roster, "free_agents": by_pos, "suggestions": suggestions,
        "warnings": plan.warnings if plan else [],
        "adds_this_week": adds_this_week(lg.week), "max_adds_week": o.max_adds_week,
        "slots": [SLOT_NAMES.get(s, str(s)) for s in lg.starting_slots],
        "standings": [{"name": t.name, "rec": f"{t.wins}-{t.losses}", "pf": round(t.points_for, 1), "me": t.id == me.id}
                      for t in standings],
        "byes": brain.bye_outlook(lg, state.get("byes") or {}),
        "playoff": state.get("playoff"),
    }


GAMEDAY_MINUTES = 5
PREGAME_WINDOW = 2 * 3600   # inactives come out ~90 minutes before kickoff


def next_delay(now=None) -> float:
    """15 minutes normally; every 5 minutes in the 2 hours before any kickoff (and just after)."""
    now = now or time.time()
    normal = POLL_MINUTES * 60
    ks = state.get("kickoffs") or []
    if any(k - PREGAME_WINDOW <= now <= k + 10 * 60 for k in ks):
        return min(normal, GAMEDAY_MINUTES * 60)
    upcoming = [k - PREGAME_WINDOW for k in ks if k - PREGAME_WINDOW > now]
    if upcoming:
        return max(60, min(normal, min(upcoming) - now + 5))
    return normal


def loop():
    time.sleep(3)
    while True:
        run_cycle()
        d = next_delay()
        state["next_run"] = time.time() + d
        _run_now.wait(d)
        _run_now.clear()


# ---- web ----------------------------------------------------------------------
app = Flask(__name__, static_folder="static")


def auth(f):
    @wraps(f)
    def w(*a, **k):
        if DASH_PASSWORD:
            au = request.authorization
            if not au or au.password != DASH_PASSWORD:
                return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="Fantasy"'})
        return f(*a, **k)
    return w


@app.get("/")
@auth
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/state")
@auth
def api_state():
    cfg = settings()
    with db() as con:
        acts = [dict(r) for r in con.execute(
            "SELECT ts, week, kind, text, ok, auto FROM actions ORDER BY id DESC LIMIT 60")]
    pub = {k: v for k, v in cfg.items() if k not in ("espn_s2", "swid")}
    pub["has_cookies"] = bool(cfg.get("espn_s2") and cfg.get("swid"))
    pub["swid_hint"] = (cfg.get("swid") or "")[:6]
    return jsonify({
        "demo": DEMO, "league_id": LEAGUE_ID, "team_id": TEAM_ID, "season": SEASON,
        "poll_minutes": POLL_MINUTES, "ntfy": bool(NTFY_URL),
        "last_run": state["last_run"], "next_run": state["next_run"], "running": state["running"],
        "error": state["error"], "snapshot": state["snapshot"], "trades": state["trades"],
        "actions": acts, "settings": pub, "sources": state["sources"],
        "offers": state["offers"], "offers_error": state["offers_error"],
    })


@app.post("/api/settings")
@auth
def api_settings():
    body = request.get_json(force=True) or {}
    cur = meta_get("settings", {}) or {}
    for k in ("auto_lineup", "auto_fa", "stream_kdst", "upgrades", "use_ir", "notify_moves", "notify_trades",
              "notify_offers", "notify_recap", "bye_cover", "playoff_plan"):
        if k in body:
            cur[k] = bool(body[k])
    if "max_adds_week" in body:
        cur["max_adds_week"] = max(0, min(10, int(body["max_adds_week"])))
    if "faab_pct" in body:
        cur["faab_pct"] = max(0, min(50, float(body["faab_pct"])))
    if body.get("questionable") in Q_FACTORS:
        cur["questionable"] = body["questionable"]
    cookies_changed = False
    for k in ("espn_s2", "swid"):
        v = (body.get(k) or "").strip().strip('"').strip()
        if v:
            if k == "espn_s2" and "%" not in v and len(v) < 50:
                return jsonify({"ok": False, "error": "That espn_s2 looks too short. It's usually 200+ characters."}), 400
            cur[k] = v
            cookies_changed = True
    meta_set("settings", cur)
    if cookies_changed:
        _run_now.set()
    return jsonify({"ok": True})


@app.post("/api/protect")
@auth
def api_protect():
    body = request.get_json(force=True) or {}
    cur = meta_get("settings", {}) or {}
    prot = set(cur.get("protected") or [])
    pid = int(body["id"])
    if body.get("on"):
        prot.add(pid)
    else:
        prot.discard(pid)
    cur["protected"] = sorted(prot)
    meta_set("settings", cur)
    if state["snapshot"]:
        for p in state["snapshot"]["roster"]:
            if p["id"] == pid:
                p["protected"] = bool(body.get("on"))
    return jsonify({"ok": True})


@app.post("/api/run")
@auth
def api_run():
    _run_now.set()
    return jsonify({"ok": True})


@app.post("/api/test-notify")
@auth
def api_test_notify():
    if not NTFY_URL:
        return jsonify({"ok": False, "error": "NTFY_URL isn't set in the compose file."})
    ok = notify("Fantasy bot", "Test notification. Alerts are working.", tags="white_check_mark")
    return jsonify({"ok": ok})


def main():
    init_db()
    threading.Thread(target=loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=4)
    except ImportError:
        app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
