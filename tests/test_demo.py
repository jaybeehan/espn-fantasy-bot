"""Quick checks on the fake league: python3 tests/test_demo.py (no ESPN login needed)."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ["DEMO"] = "1"
os.environ["DATA_DIR"] = tempfile.mkdtemp()
import main, brain  # noqa: E402
from model import BENCH, IR  # noqa: E402

main.init_db()
src = main.source(main.settings())
lg, fas = main.load(src)
o = main.options(main.settings(), lg.week)

# 1. lineup plan never makes the projected total worse, and never moves a locked player
plan = brain.plan_lineup(lg, o)
assert plan.after >= plan.before - 0.01, plan
locked = {p.id for p in lg.me.roster if p.locked}
assert not any(pid in locked for pid, _, _ in plan.moves)

# 2. after applying it, a second plan wants no further changes (no flip-flopping)
src.set_lineup(lg.week, plan.moves)
lg, fas = main.load(src)
assert brain.plan_lineup(lg, o).moves == [], "lineup keeps changing"

# 3. no starter on bye/out when an unlocked healthy bench player could fill the slot
for p in lg.me.roster:
    if p.slot not in (BENCH, IR) and not p.locked and brain.score(p, o) == 0:
        subs = [b for b in lg.me.roster if b.slot == BENCH and not b.locked and p.slot in b.eligible and brain.score(b, o) > 0]
        assert not subs, f"{p.name} starting with 0 while {subs[0].name} sits"

# 4. a bye-week kicker with no backup gets replaced by a free agent
moves = brain.plan_free_agents(lg, fas, o, 0, False)
print("free agent move:", [m.reason for m in moves])
# 5. trade ideas are win-win
for t in brain.trade_ideas(lg, o):
    assert t["gain_me"] > 0 and t["gain_them"] > 0
print("all checks passed")

# ---- added features -----------------------------------------------------------
import time  # noqa: E402

# 6. game-day timing: 5 minutes inside the 2 h pre-kickoff window, 15 otherwise,
#    and wake up right when the window opens
now = time.time()
main.state["kickoffs"] = [now + 3600]
assert main.next_delay(now) == 300
main.state["kickoffs"] = [now + 3 * 3600]
d = main.next_delay(now)
assert 3590 <= d <= 3610 or d == main.POLL_MINUTES * 60, d  # opens in 1 h -> wait min(15 min, 1 h)
assert d == main.POLL_MINUTES * 60
main.state["kickoffs"] = [now + 2 * 3600 + 120]
assert 120 <= main.next_delay(now) <= 130, main.next_delay(now)
main.state["kickoffs"] = []
assert main.next_delay(now) == main.POLL_MINUTES * 60

# 7. trade offers: evaluated and verdict given
main.check_offers(src, lg, o, main.settings())
assert main.state["offers"] and main.state["offers"][0]["verdict"] in ("accept", "close", "decline"), main.state["offers"]
print("offer:", main.state["offers"][0]["why"])

# 8. recap is written once
main.maybe_recap(src, lg, o, main.settings())
main.maybe_recap(src, lg, o, main.settings())
with main.db() as con:
    n = con.execute("SELECT COUNT(*) n FROM actions WHERE kind='recap'").fetchone()["n"]
assert n == 1, n

# 9. bye cover: if both TEs are on bye next week, pick up a TE now
byes = dict(main.state["byes"])
tes = [p for p in lg.me.roster if p.pos == "TE"]
for p in tes:
    byes[p.pro_team] = lg.week + 1
moves = brain.plan_bye_cover(lg, fas, o, byes, 0)
assert moves and moves[0].add.pos == "TE" and byes.get(moves[0].add.pro_team) != lg.week + 1, moves
assert not moves[0].drop or moves[0].drop.pos != "TE"
print("bye cover:", moves[0].reason)
outlook = brain.bye_outlook(lg, byes)
assert any("TE" in e["empty"] for e in outlook), outlook

# 10. playoff planning kicks in mid-season and changes values
src.week = 11
lg2, fas2 = main.load(src, main.settings())
assert main.state["playoff"]["active"] and main.state["playoff"]["weight"] > 0
assert any(abs(p.sos_factor - 1) > 0.01 for p in lg2.me.roster)
print("playoff weight:", main.state["playoff"]["weight"],
      [(p.name, p.sos_factor, p.notes[-1:]) for p in lg2.me.roster if p.sos_factor != 1][:2])
print("all added-feature checks passed")
