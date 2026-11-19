"""Synthetic usage generator so the dashboard can be previewed before real bots run."""

from __future__ import annotations

import random
import time

from .config import Config
from .storage import DAY, Storage

# relative activity of each bot in the demo (calls per day, avg prompt, avg completion, waste rate)
PROFILE = {
    "head_trader": (40, 3500, 900, 0.05),
    "risk_manager": (35, 2500, 600, 0.03),
    "trade_auditor": (15, 3000, 800, 0.05),
    "technical_analyst": (60, 6000, 1200, 0.12),
    "quant_analyst": (45, 5000, 1000, 0.10),
    "news_analyst": (90, 9500, 700, 0.45),        # long prompts + many repeats
    "strategy_researcher": (25, 7000, 1800, 0.15),
    "devils_advocate": (30, 2000, 400, 0.90),     # almost nothing useful
}


def generate(cfg: Config, storage: Storage, days: int = 7, seed: int = 7) -> int:
    rng = random.Random(seed)
    now = time.time()
    n = 0
    for bot, b in cfg.bots.items():
        calls_per_day, pt_avg, ct_avg, waste = PROFILE.get(bot, (20, 3000, 600, 0.1))
        model = cfg.model_for(bot)
        repeats = [f"{bot}-repeat-{i}" for i in range(5)]
        for _ in range(int(calls_per_day * days * rng.uniform(0.8, 1.2))):
            ts = now - rng.uniform(0, days * DAY)
            pt = max(50, int(rng.gauss(pt_avg, pt_avg * 0.25)))
            ct = max(10, int(rng.gauss(ct_avg, ct_avg * 0.3)))
            r = rng.random()
            status, outcome, h = "ok", None, f"{bot}-{rng.random()}"
            if r < waste * 0.25:
                status, ct = "error", 0
            elif r < waste * 0.45:
                status, ct = "empty", 0
            elif r < waste * 0.7:
                h = rng.choice(repeats)
            elif r < waste:
                outcome = "useless"
            elif rng.random() < 0.5:
                outcome = "useful"
            if bot == "devils_advocate" and outcome == "useful":
                outcome = None
            storage.log(bot=bot, team=b.team, model=model, prompt_tokens=pt, completion_tokens=ct,
                        cost_usd=cfg.cost(model, pt, ct), status=status, prompt_hash=h, outcome=outcome, ts=ts)
            n += 1
    return n
