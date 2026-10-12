"""The decisions: best lineup, free-agent moves, trade ideas.

Everything here is pure Python on the objects from model.py, so it can be tested
without ESPN.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from model import BENCH, IR, SLOT_NAMES, League, Player

# Order slots are filled in: single-position slots first, then the flexible ones.
SLOT_ORDER = [0, 2, 4, 6, 16, 17, 18, 19, 8, 9, 10, 11, 12, 13, 14, 1, 15, 3, 5, 23, 24, 7]
KICK_DEF = {"K", "D/ST"}


@dataclass
class Options:
    q_factor: float = 0.85        # how much to trust a Questionable player's projection
    d_factor: float = 0.25        # same for Doubtful
    min_gain: float = 0.3         # don't bother moving for less than this many points
    max_adds_week: int = 2        # free-agent adds per fantasy week (bot's own cap)
    stream_kdst: bool = True      # swap kicker / defense for a better weekly matchup
    stream_margin: float = 3.0    # ...when the free agent projects this many more points
    hole_margin: float = 3.0      # fill an empty/out starting spot when the FA projects this much more
    upgrade_margin: float = 2.0   # pick up a better player when worth this many more points per game
    upgrades: bool = True         # allow season-long upgrades (not just fixing holes)
    use_ir: bool = True           # move injured players into an open IR spot
    faab_pct: float = 0.05        # bid this share of remaining FAAB on an upgrade claim
    protected: set = field(default_factory=set)       # player ids never to drop
    recent_adds: set = field(default_factory=set)     # players the bot added in the last week
    claimed: set = field(default_factory=set)         # waiver claims already placed this week
    failed: set = field(default_factory=set)          # (kind, player id) ESPN rejected this week
    hot: set = field(default_factory=set)             # most-added players across Sleeper today


def score(p: Player, o: Options) -> float:
    return p.adjusted(o.q_factor, o.d_factor)


def slot_sort_key(sid: int) -> int:
    return SLOT_ORDER.index(sid) if sid in SLOT_ORDER else len(SLOT_ORDER) + sid


def fill(slots: list[int], players: list[Player], key) -> tuple[dict[int, Player | None], list[Player]]:
    """Greedy best-first fill. Returns {slot position index: player} and leftovers.

    Filling the single-position slots before the flex ones gives the best total for
    ESPN-style lineups (FLEX/OP can always take whoever is best among what's left).
    """
    order = sorted(range(len(slots)), key=lambda i: slot_sort_key(slots[i]))
    left = sorted(players, key=key, reverse=True)
    out: dict[int, Player | None] = {}
    for i in order:
        sid = slots[i]
        pick = next((p for p in left if sid in p.eligible), None)
        out[i] = pick
        if pick:
            left.remove(pick)
    return out, left


# ---------------------------------------------------------------------------
@dataclass
class LineupPlan:
    moves: list[tuple[int, int, int]]          # (player id, from slot, to slot)
    ir_moves: list[tuple[int, int, int]]
    before: float
    after: float
    reasons: list[str]
    starters: dict[int, int]                   # player id -> slot after the plan
    warnings: list[str] = field(default_factory=list)


def plan_lineup(lg: League, o: Options, roster: list[Player] | None = None) -> LineupPlan:
    roster = list(roster if roster is not None else lg.me.roster)
    reasons, warnings, ir_moves = [], [], []
    starting = lg.starting_slots
    cur = {p.id: p.slot for p in roster}

    # 1. IR housekeeping (sent separately so a rejected IR move can't block the lineup)
    in_ir = [p for p in roster if p.slot == IR]
    ir_free = lg.ir_slots - len(in_ir)
    for p in in_ir:
        if p.injury not in ("INJURY_RESERVE", "OUT"):
            warnings.append(f"{p.name} is in your IR spot but is no longer injured. ESPN blocks "
                            "pickups until he's moved out.")
    if o.use_ir and ir_free > 0:
        for p in sorted(roster, key=lambda x: x.value, reverse=True):
            if ir_free <= 0:
                break
            if p.slot != IR and not p.locked and p.injury == "INJURY_RESERVE" and IR in p.eligible \
                    and ("ir", p.id) not in o.failed:
                ir_moves.append((p.id, p.slot, IR))
                cur[p.id] = IR
                ir_free -= 1
                reasons.append(f"Moved {p.name} to IR (on injured reserve) to free a bench spot.")

    # 2. Lineup: locked players stay put, everyone else competes for what's left.
    fixed_slots = [cur[p.id] for p in roster if p.locked and cur[p.id] not in (BENCH, IR)]
    open_slots = list(starting)
    for s in fixed_slots:
        if s in open_slots:
            open_slots.remove(s)
    free = [p for p in roster if not p.locked and cur[p.id] != IR]
    picks, bench = fill(open_slots, free, key=lambda p: (score(p, o), p.value))

    # Late-game flexibility: keep the player with the latest kickoff in the FLEX-type slot,
    # so a Sunday-morning scratch can still be swapped without breaking the lineup.
    flexy = {3, 5, 7, 23, 24}
    for i in picks:
        if open_slots[i] not in flexy:
            continue
        for j in picks:
            a, b = picks[i], picks[j]
            if not a or not b or open_slots[j] in flexy or a.pos != b.pos:
                continue
            if (b.game_time or 0) > (a.game_time or 0):
                picks[i], picks[j] = b, a

    new = {p.id: cur[p.id] for p in roster}
    for i, p in picks.items():
        if p:
            new[p.id] = open_slots[i]
    for p in bench:
        new[p.id] = BENCH
    # Drop meaningless moves: a player whose score is 0 sitting in a slot with no better option
    before = sum(score(p, o) for p in roster if cur[p.id] not in (BENCH, IR) and p.slot not in (BENCH, IR))
    after = sum(score(p, o) for p in roster if new[p.id] not in (BENCH, IR))
    moves = [(p.id, p.slot, new[p.id]) for p in roster
             if new[p.id] != p.slot and p.slot != IR and (p.id, p.slot, IR) not in ir_moves]

    if moves:
        same_starters = {pid for pid, _, t in moves if t not in (BENCH, IR)} == \
            {pid for pid, f, _ in moves if f not in (BENCH, IR)}
        if after < before - 0.01:
            moves = []
        elif after - before >= o.min_gain or _fixes_dead_starter(roster, moves, o):
            pass
        elif not same_starters:
            moves = []  # a change, but too small to bother with
    if moves:
        by = {p.id: p for p in roster}
        for pid, a, b in moves:
            p = by[pid]
            if b == BENCH and a not in (BENCH, IR):
                why = _why_sit(p, o)
                reasons.append(f"Benched {p.name}{why}.")
            elif a == BENCH and b != BENCH:
                reasons.append(f"Started {p.name} at {SLOT_NAMES.get(b, b)} ({score(p, o):.1f} projected).")
    starters = {pid: s for pid, s in new.items()}
    return LineupPlan(moves, ir_moves, round(before, 1), round(after, 1), reasons, starters, warnings)


def _by_id(roster, pid):
    return next(p for p in roster if p.id == pid)


def _fixes_dead_starter(roster, moves, o):
    by = {p.id: p for p in roster}
    return any(b == BENCH and a not in (BENCH, IR) and score(by[pid], o) == 0 for pid, a, b in moves)


def _why_sit(p: Player, o: Options) -> str:
    if p.bye:
        return " (bye week)"
    if p.is_out:
        return f" ({p.injury.replace('_', ' ').lower()})"
    if p.injury in ("QUESTIONABLE", "DOUBTFUL"):
        return f" ({p.injury.lower()}, {score(p, o):.1f} projected)"
    return f" ({score(p, o):.1f} projected)"


def lineup_total(lg: League, roster: list[Player], o: Options, by="week") -> float:
    """Best possible starting total for a roster (by this week's projection or season value)."""
    key = (lambda p: score(p, o)) if by == "week" else (lambda p: p.value)
    picks, _ = fill(lg.starting_slots, [p for p in roster if p.slot != IR], key)
    return sum(key(p) for p in picks.values() if p)


def can_fill_lineup(lg: League, roster: list[Player]) -> bool:
    picks, _ = fill(lg.starting_slots, [p for p in roster if p.slot != IR], key=lambda p: 1)
    return all(picks.values())


# ---------------------------------------------------------------------------
@dataclass
class Move:
    kind: str                 # "FREEAGENT" (instant) or "WAIVER" (claim)
    add: Player
    drop: Player | None
    reason: str
    bid: int = 0
    gain: float = 0.0
    upgrade: bool = False     # a season-long upgrade (vs. fixing this week's lineup)


def _room(lg: League, roster: list[Player]) -> int:
    return lg.roster_size - sum(1 for p in roster if p.slot != IR)


def _within_limits(lg: League, roster: list[Player], add: Player, drop: Player | None) -> bool:
    limit = lg.position_limits.get(add.pos_id, 0)
    if not limit or limit <= 0:
        return True
    n = sum(1 for p in roster if p.pos_id == add.pos_id and (not drop or p.id != drop.id))
    return n + 1 <= limit


def _drop_candidates(lg: League, roster: list[Player], o: Options, keep: set) -> list[Player]:
    c = [p for p in roster
         if p.slot != IR and not p.locked and p.droppable and p.id not in o.protected
         and p.id not in o.recent_adds and p.id not in keep]
    return sorted(c, key=lambda p: (p.value, p.owned))


def plan_free_agents(lg: League, fas: list[Player], o: Options, adds_this_week: int,
                     allow_upgrades: bool, now: float | None = None) -> list[Move]:
    """At most one move per run; the loop runs again soon after, with fresh data."""
    now = now or time.time()
    if adds_this_week >= o.max_adds_week:
        return []
    if lg.acq_limit_per_matchup:
        used = lg.me.acquisitions_by_period.get(str(lg.matchup_period), 0)
        if used >= lg.acq_limit_per_matchup:
            return []
    roster = [p for p in lg.me.roster]
    plan = plan_lineup(lg, o, roster)
    starters = {pid for pid, s in plan.starters.items() if s not in (BENCH, IR)}
    rostered = {p.id for t in lg.teams.values() for p in t.roster}
    usable = [f for f in fas if not f.locked and ("add", f.id) not in o.failed and f.id not in rostered]
    instant = [f for f in usable if f.status == "FREEAGENT"]

    # 1. Holes: a starting spot that will score 0 (out, bye, empty) with no bench fix.
    base = lineup_total(lg, roster, o)
    best: Move | None = None
    shortlist = []
    for pos in {f.pos for f in instant}:  # best few at every position (kickers rank low overall)
        shortlist += sorted([f for f in instant if f.pos == pos], key=lambda p: score(p, o), reverse=True)[:10]
    for f in sorted(shortlist, key=lambda p: score(p, o), reverse=True):
        if score(f, o) < 1:
            continue
        for d in [None] + _drop_candidates(lg, roster, o, keep=set()):
            if d is None and _room(lg, roster) <= 0:
                continue
            if d and d.id in starters and score(d, o) > 0:
                continue  # don't cut a healthy starter to fill a hole
            new = [p for p in roster if not d or p.id != d.id] + [f]
            if not _within_limits(lg, roster, f, d) or not can_fill_lineup(lg, new):
                continue
            # d is the cheapest workable drop for this free agent: judge it, then stop looking.
            gain = lineup_total(lg, new, o) - base
            same_kd = d is not None and d.pos in KICK_DEF and d.pos == f.pos
            long_term_ok = d is None or same_kd or d.value <= f.value + 1.5
            kd_ok = f.pos not in KICK_DEF or o.stream_kdst or _starter_dead(plan, lg, f.pos)
            if gain >= o.hole_margin and long_term_ok and kd_ok and (best is None or gain > best.gain):
                if d is None:
                    why = "used an open roster spot"
                elif d.bye or d.is_out:
                    why = f"dropped {d.name} ({_why_sit(d, o).strip(' ()')})"
                else:
                    why = f"dropped {d.name} ({d.value:.1f} pts/game)"
                best = Move("FREEAGENT", f, d, f"Added {f.name} ({f.pos}, {f.pro_team}) for this week: "
                            f"+{gain:.1f} projected points; {why}.", gain=round(gain, 1))
            break
    if best:
        return [best]

    # 2. Season-long upgrade (not on game days, at most once a day — caller decides).
    if not (allow_upgrades and o.upgrades):
        return []
    skill = [f for f in usable if f.pos not in KICK_DEF and f.id not in o.claimed and f.value > 0]
    drops = [d for d in _drop_candidates(lg, roster, o, keep=set()) if d.pos not in KICK_DEF and d.id not in starters]
    base_v = lineup_total(lg, roster, o, by="value")
    for f in sorted(skill, key=lambda p: p.value, reverse=True)[:30]:
        margin = o.upgrade_margin - (0.5 if f.id in o.hot else 0.0)  # the crowd agrees: lower the bar a bit
        for d in drops:
            if f.value < d.value + margin:
                break
            new = [p for p in roster if p.id != d.id] + [f]
            if not _within_limits(lg, roster, f, d) or not can_fill_lineup(lg, new):
                continue
            gain_v = lineup_total(lg, new, o, by="value") - base_v
            if gain_v <= 0.05 and f.pos != d.pos:
                continue  # e.g. a third QB who'd never start isn't an upgrade over a bench RB
            bid = 0
            if f.status == "WAIVERS" and lg.faab:
                bid = max(lg.min_bid, round(lg.faab_left * o.faab_pct))
                bid = min(bid, lg.faab_left)
            return [Move("WAIVER" if f.status == "WAIVERS" else "FREEAGENT", f, d,
                         f"{'Claimed' if f.status == 'WAIVERS' else 'Added'} {f.name} ({f.pos}, {f.pro_team}, "
                         f"{f.value:.1f} pts/game) for {d.name} ({d.value:.1f} pts/game)"
                         + (", one of today's most-added players" if f.id in o.hot else "")
                         + (f", ${bid} bid" if bid else "") + ".", bid=bid, gain=round(max(gain_v, f.value - d.value), 1),
                         upgrade=True)]
    return []


def _starter_dead(plan: LineupPlan, lg: League, pos: str) -> bool:
    by = {p.id: p for p in lg.me.roster}
    return any(by[pid].pos == pos and s not in (BENCH, IR) and (by[pid].bye or by[pid].is_out)
               for pid, s in plan.starters.items())


# ---------------------------------------------------------------------------
def trade_ideas(lg: League, o: Options, limit: int = 6) -> list[dict]:
    """1-for-1 and 2-for-1 trades that make both teams' best lineups better (by season value).

    Win-win trades are the ones other managers actually accept.
    """
    me = lg.me
    mine = [p for p in me.roster if p.pos not in KICK_DEF and p.injury != "INJURY_RESERVE"]
    my_base = lineup_total(lg, me.roster, o, by="value")
    ideas = []
    for t in lg.teams.values():
        if t.id == me.id:
            continue
        theirs = [p for p in t.roster if p.pos not in KICK_DEF and p.injury != "INJURY_RESERVE"]
        their_base = lineup_total(lg, t.roster, o, by="value")
        for a in mine:
            for b in theirs:
                if abs(a.value - b.value) > 4 or a.pos == b.pos and abs(a.value - b.value) < 0.5:
                    continue
                gm = lineup_total(lg, [p for p in me.roster if p.id != a.id] + [b], o, by="value") - my_base
                if gm < 1.0:
                    continue
                gt = lineup_total(lg, [p for p in t.roster if p.id != b.id] + [a], o, by="value") - their_base
                if gt < 0.3:
                    continue
                ideas.append({"team": t.name, "team_id": t.id, "give": [a.to_dict()], "get": [b.to_dict()],
                              "gain_me": round(gm, 1), "gain_them": round(gt, 1),
                              "why": _trade_why(a, b, gm, gt)})
        # 2-for-1: consolidate two of my decent players into one better one
        pairs = sorted(mine, key=lambda p: p.value, reverse=True)[:10]
        for i, a1 in enumerate(pairs):
            for a2 in pairs[i + 1:]:
                for b in sorted(theirs, key=lambda p: p.value, reverse=True)[:6]:
                    if b.value < max(a1.value, a2.value) + 2:
                        continue
                    gm = lineup_total(lg, [p for p in me.roster if p.id not in (a1.id, a2.id)] + [b], o, by="value") - my_base
                    if gm < 1.5:
                        continue
                    gt = lineup_total(lg, [p for p in t.roster if p.id != b.id] + [a1, a2], o, by="value") - their_base
                    if gt < 0.5:
                        continue
                    ideas.append({"team": t.name, "team_id": t.id, "give": [a1.to_dict(), a2.to_dict()],
                                  "get": [b.to_dict()], "gain_me": round(gm, 1), "gain_them": round(gt, 1),
                                  "why": f"Two-for-one: you consolidate into a stronger starter (+{gm:.1f}/game); "
                                         f"they fill holes (+{gt:.1f}/game). They'll need to drop someone."})
    ideas.sort(key=lambda x: x["gain_me"] + 0.5 * x["gain_them"], reverse=True)
    out, used = [], set()
    for idea in ideas:
        k = (idea["team_id"], tuple(p["id"] for p in idea["get"]))
        g = tuple(p["id"] for p in idea["give"])
        if k in used or (g, "give") in used:
            continue
        used.add(k)
        used.add((g, "give"))
        out.append(idea)
        if len(out) >= limit:
            break
    return out


def _trade_why(a: Player, b: Player, gm: float, gt: float) -> str:
    if a.pos != b.pos:
        return (f"You have depth at {a.pos} and need a {b.pos}; they're the opposite. "
                f"Your lineup +{gm:.1f}/game, theirs +{gt:.1f}/game.")
    return f"Both lineups improve: you +{gm:.1f}/game, them +{gt:.1f}/game."


# ---------------------------------------------------------------------------
# Incoming trade offers
def evaluate_offer(lg: League, o: Options, other_id: int, give_ids: list[int], get_ids: list[int]) -> dict:
    """Score a proposed trade for me (and them), by season value and by this week."""
    me, them = lg.me, lg.teams.get(other_id)
    if not give_ids and not get_ids:
        return {"verdict": "unknown", "why": "Couldn't read the players in this offer."}
    pool = {p.id: p for t in lg.teams.values() for p in t.roster}
    give = [pool[i] for i in give_ids if i in pool]
    get = [pool[i] for i in get_ids if i in pool]
    if len(give) != len(give_ids) or len(get) != len(get_ids) or them is None:
        return {"verdict": "unknown", "why": "Some players in this offer aren't on the expected rosters anymore."}

    def after(roster, out, inn, size):
        new = [p for p in roster if p.id not in {x.id for x in out}] + inn
        dropped = []
        extra = sum(1 for p in new if p.slot != IR) - size
        if extra > 0:  # they'd have to drop someone: assume the weakest bench players go
            cands = sorted([p for p in new if p not in inn and p.slot != IR], key=lambda p: p.value)
            dropped = cands[:extra]
            new = [p for p in new if p not in dropped]
        return new, dropped

    mine_new, my_drops = after(me.roster, give, get, lg.roster_size)
    their_new, _ = after(them.roster, get, give, lg.roster_size)
    gm = lineup_total(lg, mine_new, o, by="value") - lineup_total(lg, me.roster, o, by="value")
    gw = lineup_total(lg, mine_new, o) - lineup_total(lg, me.roster, o)
    gt = lineup_total(lg, their_new, o, by="value") - lineup_total(lg, them.roster, o, by="value")
    fillable = can_fill_lineup(lg, mine_new)
    if not fillable:
        verdict, head = "decline", "Decline: you couldn't field a full lineup after this trade"
    elif gm >= 1.0:
        verdict, head = "accept", f"Accept: your lineup gets better by {gm:.1f} pts/game"
    elif gm > -0.5:
        if abs(gm) < 0.05 and abs(gw) < 0.5:
            verdict, head = "close", "Close call: it doesn't change your starting lineup either way"
        else:
            verdict, head = "close", f"Close call: about even for you ({gm:+.1f} pts/game)"
    else:
        verdict, head = "decline", f"Decline: your lineup gets worse by {abs(gm):.1f} pts/game"
    bits = [head + "."]
    if abs(gw) >= 1:
        bits.append(f"This week: {gw:+.1f} projected.")
    if gt > 1 and verdict != "accept":
        bits.append(f"It helps them more ({gt:+.1f}/game).")
    if my_drops:
        bits.append("You'd have to drop " + ", ".join(p.name for p in my_drops) + ".")
    return {"verdict": verdict, "gain_me": round(gm, 1), "gain_week": round(gw, 1), "gain_them": round(gt, 1),
            "why": " ".join(bits), "give": [p.to_dict() for p in give], "get": [p.to_dict() for p in get],
            "team": them.name, "team_id": them.id}


# ---------------------------------------------------------------------------
# Bye weeks ahead
def _week_lineup(lg: League, roster: list[Player], byes: dict, week: int):
    """Best lineup for a future week by season value, skipping players on bye that week."""
    usable = [p for p in roster if p.slot != IR and byes.get(p.pro_team) != week and p.injury != "INJURY_RESERVE"]
    return fill(lg.starting_slots, usable, key=lambda p: p.value)


def bye_outlook(lg: League, byes: dict, ahead: int = 3) -> list[dict]:
    """For each of the next few weeks: which usual starters are on bye, and any slot nobody can fill."""
    roster = lg.me.roster
    usual, _ = fill(lg.starting_slots, [p for p in roster if p.slot != IR and p.injury != "INJURY_RESERVE"],
                    key=lambda p: p.value)
    starters = [p for p in usual.values() if p]
    out = []
    for w in range(lg.week + 1, min(lg.week + ahead, lg.final_week) + 1):
        off = [p for p in starters if byes.get(p.pro_team) == w]
        if not off:
            continue
        picks, _ = _week_lineup(lg, roster, byes, w)
        empty = [SLOT_NAMES.get(lg.starting_slots[i], "?") for i, p in picks.items() if p is None]
        out.append({"week": w, "off": [f"{p.name} ({p.pos})" for p in off], "empty": empty})
    return out


def plan_bye_cover(lg: League, fas: list[Player], o: Options, byes: dict, adds_this_week: int) -> list[Move]:
    """Next week a starting spot would be empty because of byes: grab cover now, before others do.

    Kicker and defense are left to the normal weekly streaming.
    """
    if adds_this_week >= o.max_adds_week:
        return []
    nxt = lg.week + 1
    if nxt > lg.final_week:
        return []
    roster = lg.me.roster
    picks, _ = _week_lineup(lg, roster, byes, nxt)
    empty = [lg.starting_slots[i] for i, p in picks.items() if p is None]
    empty = [s for s in empty if SLOT_NAMES.get(s) not in KICK_DEF]
    if not empty:
        return []
    slot = empty[0]
    this_week = plan_lineup(lg, o, roster)
    starting_now = {pid for pid, s in this_week.starters.items() if s not in (BENCH, IR)}
    next_starters = {p.id for p in picks.values() if p}
    rostered = {p.id for t in lg.teams.values() for p in t.roster}
    cands = [f for f in fas if f.status == "FREEAGENT" and not f.locked and f.id not in rostered
             and ("add", f.id) not in o.failed and slot in f.eligible and byes.get(f.pro_team) != nxt
             and not f.is_out and f.value > 0]
    if not cands:
        return []
    f = max(cands, key=lambda p: p.value)
    drops = [d for d in _drop_candidates(lg, roster, o, keep=starting_now | next_starters)
             if d.pos not in KICK_DEF and d.value <= f.value + 1.0]
    room = _room(lg, roster) > 0
    d = None if room else (drops[0] if drops else None)
    if not room and d is None:
        return []
    new = [p for p in roster if not d or p.id != d.id] + [f]
    if not _within_limits(lg, roster, f, d) or not can_fill_lineup(lg, new):
        return []
    sname = SLOT_NAMES.get(slot, "?")
    why = "open roster spot" if d is None else f"dropped {d.name} ({d.value:.1f} pts/game)"
    return [Move("FREEAGENT", f, d, f"Bye-week cover: added {f.name} ({f.pos}, {f.pro_team}) because nobody "
                 f"on your roster can play {sname} in week {nxt}; {why}.", gain=round(f.value, 1))]


# ---------------------------------------------------------------------------
# Fantasy playoff schedule
POS_KEY = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "D/ST": "DEF"}


def playoff_weight(lg: League) -> float:
    """0 until about mid-season, rising to 0.5 by the last regular-season week."""
    pw = [w for w in lg.playoff_weeks if w >= lg.week]
    if not pw:
        return 0.0
    until = pw[0] - lg.week
    if lg.week in lg.playoff_weeks:
        return 0.5
    return round(max(0.0, min(1.0, (9 - until) / 8)) * 0.5, 3)


def playoff_factor(p: Player, lg: League, opps: dict, byes: dict, allowed: dict, avg: dict) -> tuple[float, str]:
    """How good his fantasy-playoff matchups are: 1.0 = average, <1 tough or a bye."""
    weeks = [w for w in lg.playoff_weeks if w > lg.week]
    key = POS_KEY.get(p.pos)
    if not weeks or not key or not p.pro_team or p.pro_team == "FA":
        return 1.0, ""
    vals, tough, easy, bye = [], [], [], []
    for w in weeks:
        if byes.get(p.pro_team) == w:
            vals.append(0.0)
            bye.append(w)
            continue
        opp = (opps.get(p.pro_team) or {}).get(w)
        a = allowed.get(opp, {}).get(key)
        if opp is None or a is None or not avg.get(key):
            vals.append(1.0)
            continue
        raw = a / avg[key]
        f = max(0.8, min(1.2, 1 + 0.5 * (raw - 1)))  # regress toward average; samples are small
        vals.append(f)
        (easy if f >= 1.06 else tough if f <= 0.94 else []).append(f"wk{w} {opp}")
    f = sum(vals) / len(vals)
    note = ""
    if bye:
        note = f"Playoffs: on bye week {', '.join(map(str, bye))}"
    elif f >= 1.04:
        note = "Playoffs: easy schedule (" + ", ".join(easy) + ")"
    elif f <= 0.96:
        note = "Playoffs: tough schedule (" + ", ".join(tough) + ")"
    return f, note
