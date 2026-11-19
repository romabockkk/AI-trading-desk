import tempfile
import time
import unittest
from pathlib import Path

from grok_budget import BotStopped, BudgetExceeded, GrokClient, load_config
from grok_budget import allocator, analyzer, cleanup
from grok_budget.control import Control
from grok_budget.storage import DAY, Storage

ROOT = Path(__file__).resolve().parent.parent


def fake_api(content="ok answer", pt=1000, ct=200):
    calls = []

    def transport(body):
        calls.append(body)
        return {"choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct}}
    transport.calls = calls
    return transport


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "grok_config.toml", self.tmp.name)
        self.storage = Storage(self.cfg.state_dir)
        self.control = Control(self.cfg.state_dir)

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def log(self, bot, n, cost=0.01, status="ok", outcome=None, same_hash=False, ts=None):
        for i in range(n):
            self.storage.log(bot=bot, team=self.cfg.bots[bot].team, model="grok-4", prompt_tokens=100,
                             completion_tokens=10, cost_usd=cost, status=status, outcome=outcome,
                             prompt_hash="h" if same_hash else f"{bot}{i}{status}", ts=ts or time.time() - 3600)


class TestKillSwitch(Base):
    def test_stop_blocks_every_bot_and_resume_unblocks(self):
        api = fake_api()
        c = GrokClient("risk_manager", self.cfg, transport=api)
        self.control.stop_all("test")
        self.assertTrue(c.should_stop())
        with self.assertRaises(BotStopped):
            c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(api.calls, [])
        self.control.resume_all()
        self.assertEqual(c.chat([{"role": "user", "content": "hi"}]), "ok answer")

    def test_pause_single_bot(self):
        self.control.pause("news_analyst")
        with self.assertRaises(BotStopped):
            GrokClient("news_analyst", self.cfg, transport=fake_api()).chat([{"role": "user", "content": "x"}])
        GrokClient("quant_analyst", self.cfg, transport=fake_api()).chat([{"role": "user", "content": "x"}])


class TestClient(Base):
    def test_logs_cost_caps_tokens_and_caches(self):
        api = fake_api()
        c = GrokClient("devils_advocate", self.cfg, transport=api)
        msgs = [{"role": "user", "content": "same"}]
        c.chat(msgs, max_tokens=99999)
        self.assertEqual(api.calls[0]["max_tokens"], 1000)          # capped by config
        self.assertEqual(api.calls[0]["model"], "grok-3-mini")       # per-bot model
        c.chat(msgs, max_tokens=99999)
        self.assertEqual(len(api.calls), 1)                          # second answered from cache
        rows = self.storage.rows(0)
        self.assertEqual([r.status for r in rows], ["ok", "cache_hit"])
        self.assertAlmostEqual(rows[0].cost_usd, (1000 * 0.30 + 200 * 0.50) / 1e6)

    def test_hard_budget_limit(self):
        an = analyzer.analyze(self.cfg, self.storage)
        allocator.save_allocations(self.cfg.state_dir, self.cfg, allocator.allocate(self.cfg, an))
        daily = allocator.load_allocations(self.cfg.state_dir)["bots"]["head_trader"]["daily_cap_usd"]
        self.storage.log(bot="head_trader", team="control", model="grok-4", cost_usd=daily + 0.01, status="ok")
        with self.assertRaises(BudgetExceeded):
            GrokClient("head_trader", self.cfg, transport=fake_api()).chat([{"role": "user", "content": "x"}])

    def test_empty_answer_is_marked(self):
        GrokClient("quant_analyst", self.cfg, transport=fake_api(content="  ")).chat([{"role": "user", "content": "x"}])
        self.assertEqual(self.storage.rows(0)[0].status, "empty")


class TestAnalysisAndAllocation(Base):
    def test_useless_bot_gets_disable_and_zero_budget(self):
        self.log("devils_advocate", 20, outcome="useless")
        self.log("head_trader", 20, outcome="useful")
        an = analyzer.analyze(self.cfg, self.storage)
        self.assertEqual(an.bots["devils_advocate"].verdict, "DISABLE")
        allocs = allocator.allocate(self.cfg, an)
        self.assertEqual(allocs["devils_advocate"].weekly_usd, 0)

    def test_protected_bot_never_disabled(self):
        self.log("risk_manager", 20, status="error")
        an = analyzer.analyze(self.cfg, self.storage)
        self.assertEqual(an.bots["risk_manager"].verdict, "REDUCE")
        plan = cleanup.plan_cleanup(self.cfg, an, self.storage, self.control)
        self.assertNotIn("risk_manager", [b for b, _ in plan.disable_bots])

    def test_allocation_respects_reserve_and_bounds(self):
        self.log("technical_analyst", 200, cost=0.5)   # one hungry bot
        self.log("head_trader", 5)
        an = analyzer.analyze(self.cfg, self.storage)
        allocs = allocator.allocate(self.cfg, an)
        total = sum(a.weekly_usd for a in allocs.values())
        self.assertLessEqual(total, self.cfg.distributable_usd + 1e-9)
        self.assertLess(self.cfg.distributable_usd, self.cfg.weekly_budget_usd)   # reserve kept
        for name, a in allocs.items():
            b = self.cfg.bots[name]
            self.assertGreaterEqual(a.share_percent + 1e-6, b.min_share)
            self.assertLessEqual(a.share_percent, b.max_share + 1e-6)
            self.assertAlmostEqual(a.daily_cap_usd * self.cfg.plan_days, a.weekly_usd)

    def test_clamp_shares_sums_to_100(self):
        shares = allocator._clamp_shares({"a": 10, "b": 1, "c": 1}, {"a": (0, 50), "b": (10, 100), "c": (0, 100)})
        self.assertAlmostEqual(sum(shares.values()), 100)
        self.assertEqual(shares["a"], 50)

    def test_duplicates_count_as_waste(self):
        self.log("news_analyst", 12, same_hash=True)
        st = analyzer.analyze(self.cfg, self.storage).bots["news_analyst"]
        self.assertEqual(st.duplicates, 11)
        self.assertEqual(st.verdict, "DISABLE")  # 92% waste, nothing useful


class TestCleanup(Base):
    def test_cleanup_archives_old_rows_and_disables(self):
        self.log("head_trader", 3, ts=time.time() - 60 * DAY)
        self.log("devils_advocate", 20, outcome="useless")
        an = analyzer.analyze(self.cfg, self.storage)
        plan = cleanup.plan_cleanup(self.cfg, an, self.storage, self.control)
        self.assertEqual(plan.old_usage_rows, 3)
        cleanup.apply_cleanup(plan, self.storage, self.control)
        self.assertEqual(self.storage.count_older(time.time() - 30 * DAY), 0)
        summary = self.storage.conn.execute("SELECT SUM(calls) FROM weekly_summary").fetchone()[0]
        self.assertEqual(summary, 3)
        self.assertIn("devils_advocate", self.control.inactive_bots())


if __name__ == "__main__":
    unittest.main()
