"""Command line: python -m grok_budget <command>"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

from . import analyzer, allocator, cleanup, dashboard
from .config import DEFAULT_CONFIG, load_config
from .control import Control
from .storage import DAY, Storage


def _ctx(args):
    cfg = load_config(args.config, args.state)
    return cfg, Storage(cfg.state_dir), Control(cfg.state_dir)


def _analysis(cfg, storage, days):
    now = time.time()
    return analyzer.analyze(cfg, storage, since=now - days * DAY, until=now)


def cmd_stop(args):
    cfg, _, control = _ctx(args)
    control.stop_all(args.reason)
    print(f"■ СТОП: вся команда остановлена ({args.reason}).")
    print("  Все вызовы GrokClient.chat() теперь бросают BotStopped; should_stop() возвращает True.")
    print("  Возобновить: python -m grok_budget resume")


def cmd_resume(args):
    _, _, control = _ctx(args)
    control.resume_all()
    print("▶ Команда снова работает (паузы отдельных ботов сохранены — см. status).")


def cmd_pause(args):
    _, _, control = _ctx(args)
    control.pause(args.bot, args.reason)
    print(f"⏸ {args.bot} на паузе")


def cmd_unpause(args):
    _, _, control = _ctx(args)
    control.unpause(args.bot)
    print(f"▶ {args.bot} снова активен")


def cmd_status(args):
    cfg, storage, control = _ctx(args)
    st = control.load()
    print("Команда:", "ОСТАНОВЛЕНА — " + st.get("stop_reason", "") if st["global_stop"] else "работает")
    for name, b in cfg.bots.items():
        reason = control.blocked_reason(name)
        spent = storage.spent(time.time() - 7 * DAY, name)
        print(f"  {b.label:<20} {'⛔ ' + reason if reason else '✓ активен':<45} ${spent:.2f} за 7 дн.")


def cmd_dashboard(args):
    cfg, storage, control = _ctx(args)
    an = _analysis(cfg, storage, args.days)
    allocs = allocator.allocate(cfg, an, control.inactive_bots())
    stopped = control.is_stopped()
    print(dashboard.render_terminal(cfg, an, allocs, stopped))
    out = dashboard.write_html(Path(args.out), dashboard.render_html(cfg, an, allocs, stopped, demo=args.demo_label))
    print(f"\nHTML-дашборд: {out.resolve()}")


def cmd_analyze(args):
    cfg, storage, _ = _ctx(args)
    an = _analysis(cfg, storage, args.days)
    print(f"Анализ за {args.days} дн.: ${an.total_cost:.2f}, потери ${an.total_waste:.2f} ({an.waste_percent:.1f}%)\n")
    for s in sorted(an.bots.values(), key=lambda s: -s.waste_cost):
        print(f"[{s.verdict:<7}] {s.label:<20} ${s.cost_usd:7.2f}  доля {s.share_percent:5.1f}%  потери {s.waste_percent:4.0f}%")
        for r in s.reasons:
            print(f"            · {r}")


def cmd_allocate(args):
    cfg, storage, control = _ctx(args)
    an = _analysis(cfg, storage, args.days)
    allocs = allocator.allocate(cfg, an, control.inactive_bots())
    print(f"Недельный бюджет ${cfg.weekly_budget_usd:.2f} → ботам ${cfg.distributable_usd:.2f}, "
          f"резерв ${cfg.reserve_usd:.2f} ({cfg.reserve_percent:g}%), растянуто на {cfg.plan_days:g} дн.\n")
    print(f"{'Бот':<22}{'Доля':>7}{'$/нед':>9}{'$/день':>9}  Обоснование")
    for a in sorted(allocs.values(), key=lambda a: -a.weekly_usd):
        print(f"{cfg.bots[a.bot].label:<22}{a.share_percent:>6.1f}%{a.weekly_usd:>9.2f}{a.daily_cap_usd:>9.2f}  {a.reason}")
    print("\nПо командам:")
    for t, v in allocator.team_totals(allocs).items():
        print(f"  {cfg.teams.get(t, t):<24} ${v:.2f}/нед")
    unalloc = cfg.distributable_usd - sum(a.weekly_usd for a in allocs.values())
    if unalloc > 0.005:
        print(f"  (не распределено, добавлено к резерву: ${unalloc:.2f})")
    if args.dry_run:
        print("\n--dry-run: лимиты НЕ сохранены.")
    else:
        p = allocator.save_allocations(cfg.state_dir, cfg, allocs)
        print(f"\nЛимиты сохранены: {p} — GrokClient применяет их к каждому запросу.")


def cmd_cleanup(args):
    cfg, storage, control = _ctx(args)
    an = _analysis(cfg, storage, args.days)
    plan = cleanup.plan_cleanup(cfg, an, storage, control)
    if plan.empty:
        print("Нечего удалять — всё полезно.")
        return
    print("План очистки:")
    for bot, reason in plan.disable_bots:
        print(f"  ✕ отключить бота {bot}: {reason}")
    if plan.old_usage_rows:
        print(f"  ✕ удалить {plan.old_usage_rows} сырых записей старше {cfg.retention_days} дн. (итоги сохраняются по неделям)")
    if plan.expired_cache:
        print(f"  ✕ удалить {plan.expired_cache} устаревших записей кэша")
    if not args.apply:
        print("\nЭто пробный прогон. Применить: python -m grok_budget cleanup --apply")
        return
    cleanup.apply_cleanup(plan, storage, control)
    print("\nГотово. Отключённых ботов можно вернуть: python -m grok_budget unpause <bot>")


def cmd_rebalance(args):
    """Weekly autopilot: analyze → (optional) cleanup → new limits → dashboard."""
    if args.auto_cleanup:
        args.apply = True
        cmd_cleanup(args)
        print()
    args.dry_run = False
    cmd_allocate(args)
    print()
    cmd_dashboard(args)


def cmd_import_csv(args):
    cfg, storage, _ = _ctx(args)
    n = 0
    with open(args.file, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            bot = row["bot"]
            model = row.get("model") or cfg.model_for(bot) if bot in cfg.bots else (row.get("model") or cfg.default_model)
            pt, ct = int(row.get("prompt_tokens") or 0), int(row.get("completion_tokens") or 0)
            cost = float(row["cost_usd"]) if row.get("cost_usd") else cfg.cost(model, pt, ct)
            ts = row["ts"]
            ts_f = float(ts) if ts.replace(".", "", 1).isdigit() else time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
            team = cfg.bots[bot].team if bot in cfg.bots else row.get("team", "unknown")
            storage.log(bot=bot, team=team, model=model, prompt_tokens=pt, completion_tokens=ct, cost_usd=cost,
                        status=row.get("status") or "ok", prompt_hash=row.get("prompt_hash") or None,
                        outcome=row.get("outcome") or None, ts=ts_f)
            n += 1
    print(f"Импортировано {n} записей из {args.file}")


def cmd_demo(args):
    from . import demo
    cfg, storage, _ = _ctx(args)
    n = demo.generate(cfg, storage)
    print(f"Сгенерировано {n} демо-записей в {cfg.state_dir}/ (используйте отдельный --state, чтобы не смешивать с реальными)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grok_budget", description="Контроль бюджета Grok для AI Trading Desk")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--state", default=None, help="папка состояния (по умолчанию grok_state/)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stop", help="остановить всю команду")
    s.add_argument("--reason", default="ручная остановка для ребалансировки")
    s.set_defaults(fn=cmd_stop)
    sub.add_parser("resume", help="возобновить работу команды").set_defaults(fn=cmd_resume)
    s = sub.add_parser("pause", help="поставить бота на паузу")
    s.add_argument("bot")
    s.add_argument("--reason", default="ручная пауза")
    s.set_defaults(fn=cmd_pause)
    s = sub.add_parser("unpause", help="вернуть бота (снимает паузу и отключение)")
    s.add_argument("bot")
    s.set_defaults(fn=cmd_unpause)
    sub.add_parser("status", help="состояние команды").set_defaults(fn=cmd_status)

    def days(sp):
        sp.add_argument("--days", type=float, default=7)

    s = sub.add_parser("dashboard", help="недельный дашборд (терминал + HTML)")
    days(s)
    s.add_argument("--out", default="reports/weekly_dashboard.html")
    s.add_argument("--demo-label", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_dashboard)
    s = sub.add_parser("analyze", help="найти бесполезные расходы")
    days(s)
    s.set_defaults(fn=cmd_analyze)
    s = sub.add_parser("allocate", help="рассчитать и сохранить недельные лимиты")
    days(s)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_allocate)
    s = sub.add_parser("cleanup", help="удалить бесполезное (по умолчанию пробный прогон)")
    days(s)
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_cleanup)
    s = sub.add_parser("rebalance", help="анализ + лимиты + дашборд (запускать раз в неделю)")
    days(s)
    s.add_argument("--auto-cleanup", action="store_true", help="также отключить бесполезных ботов")
    s.add_argument("--out", default="reports/weekly_dashboard.html")
    s.add_argument("--demo-label", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_rebalance)
    s = sub.add_parser("import-csv", help="импорт расходов из CSV (ts,bot,model,prompt_tokens,completion_tokens[,cost_usd,status,outcome])")
    s.add_argument("file")
    s.set_defaults(fn=cmd_import_csv)
    sub.add_parser("demo", help=argparse.SUPPRESS).set_defaults(fn=cmd_demo)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0
