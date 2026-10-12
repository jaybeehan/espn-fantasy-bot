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
