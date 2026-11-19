"""Weekly usage dashboard: terminal table + self-contained HTML report."""

from __future__ import annotations

import html
import json
import time
from pathlib import Path

from .allocator import Allocation
from .analyzer import Analysis
from .config import Config
from .storage import DAY

VERDICT_TEXT = {
    "OK": "OK",
    "IDLE": "простаивает",
    "TRIM": "сократить prompt",
    "CACHE": "нужен кэш",
    "REDUCE": "урезать",
    "DISABLE": "отключить",
}


def pace(cfg: Config, an: Analysis) -> dict[str, float | None]:
    """Average daily spend over the window and how many days the budget lasts at that pace."""
    span_days = max(1.0, (an.until - an.since) / DAY)
    avg_daily = an.total_cost / span_days
    lasts = cfg.distributable_usd / avg_daily if avg_daily > 0 else None
    return {"avg_daily": avg_daily, "lasts_days": lasts}


# --------------------------------------------------------------------- terminal
def render_terminal(cfg: Config, an: Analysis, allocs: dict[str, Allocation], stopped: bool) -> str:
    p = pace(cfg, an)
    lines = []
    status = "ОСТАНОВЛЕНА" if stopped else "работает"
    lines.append(f"GROK WEEKLY USAGE   {_d(an.since)} — {_d(an.until)}   команда: {status}")
    lines.append(
        f"Потрачено ${an.total_cost:.2f} из ${cfg.distributable_usd:.2f} (бюджет ${cfg.weekly_budget_usd:.2f},"
        f" резерв ${cfg.reserve_usd:.2f}) · потери {an.waste_percent:.1f}%"
    )
    if p["lasts_days"] is not None:
        lines.append(f"Темп ${p['avg_daily']:.2f}/день → бюджета хватит на {p['lasts_days']:.1f} дн. (цель ≥ 7)")
    lines.append("")
    hdr = f"{'Бот':<20}{'Команда':<10}{'Вызовы':>7}{'Токены':>11}{'$':>9}{'Доля':>7}{'Лимит/нед':>11}{'Потери':>8}  Вердикт"
    lines.append(hdr)
    lines.append("-" * len(hdr) + "-" * 12)
    for s in sorted(an.bots.values(), key=lambda s: -s.cost_usd):
        a = allocs.get(s.bot)
        lines.append(
            f"{s.label[:19]:<20}{s.team[:9]:<10}{s.calls:>7}{s.tokens:>11,}{s.cost_usd:>9.2f}"
            f"{s.share_percent:>6.1f}%{(a.weekly_usd if a else 0):>11.2f}{s.waste_percent:>7.0f}%  {s.verdict}"
        )
    lines.append("")
    lines.append("Доля команд: " + ", ".join(
        f"{cfg.teams.get(t, t)} {100 * c / an.total_cost:.1f}%" for t, c in sorted(an.team_cost.items(), key=lambda x: -x[1])
    ) if an.total_cost else "Доля команд: нет расходов за период")
    return "\n".join(lines)


def _d(ts: float) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts))


# ------------------------------------------------------------------------ HTML
def render_html(cfg: Config, an: Analysis, allocs: dict[str, Allocation], stopped: bool,
                demo: bool = False) -> str:
    p = pace(cfg, an)
    e = html.escape
    teams = list(cfg.teams)
    for t in an.team_cost:
        if t not in teams:
            teams.append(t)
    team_slot = {t: i % 8 + 1 for i, t in enumerate(teams)}
    bots_sorted = sorted(an.bots.values(), key=lambda s: -s.cost_usd)
    used_pct = 100 * an.total_cost / cfg.distributable_usd if cfg.distributable_usd else 0
    lasts = p["lasts_days"]
    lasts_txt = "—" if lasts is None else f"{lasts:.1f} дн."
    lasts_status = "good" if lasts is None or lasts >= 7 else ("warning" if lasts >= 5 else "critical")

    # --- bot share bars
    max_share = max([s.share_percent for s in bots_sorted] + [1])
    bar_rows = []
    for s in bots_sorted:
        a = allocs.get(s.bot)
        w = 100 * s.share_percent / max_share
        alloc_share = a.share_percent if a else 0
        tip = (f"{s.label}: {s.share_percent:.1f}% расходов · ${s.cost_usd:.2f} · {s.calls} вызовов · "
               f"лимит {alloc_share:.1f}% (${(a.weekly_usd if a else 0):.2f}/нед)")
        bar_rows.append(
            f'<div class="bar-row" data-tip="{e(tip)}"><span class="bar-label">{e(s.label)}</span>'
            f'<span class="bar-track"><span class="bar" style="width:{w:.2f}%"></span>'
            f'<span class="alloc-mark" style="left:{min(100, 100 * alloc_share / max_share):.2f}%" title="лимит"></span></span>'
            f'<span class="bar-val">{s.share_percent:.1f}%</span></div>'
        )

    # --- team split (single stacked bar)
    team_segs, team_legend = [], []
    for t in teams:
        c = an.team_cost.get(t, 0.0)
        pct = 100 * c / an.total_cost if an.total_cost else 0
        label = cfg.teams.get(t, t)
        if pct > 0:
            team_segs.append(
                f'<span class="seg" style="flex:{pct:.3f};background:var(--series-{team_slot[t]})" '
                f'data-tip="{e(label)}: {pct:.1f}% · ${c:.2f}"></span>')
        team_legend.append(
            f'<span class="lg"><i style="background:var(--series-{team_slot[t]})"></i>{e(label)} '
            f'<b>{pct:.1f}%</b></span>')

    # --- daily stacked columns (SVG)
    days = []
    for i in range(int(round((an.until - an.since) / DAY))):
        days.append(time.strftime("%Y-%m-%d", time.localtime(an.since + (i + 0.5) * DAY)))
    days = sorted(set(days) | set(an.daily_cost))[-max(7, len(days)):]
    daily_cap = sum(a.daily_cap_usd for a in allocs.values())
    totals = [sum(an.daily_cost.get(d, {}).values()) for d in days]
    ymax = max(totals + [daily_cap, 0.01]) * 1.15
    W, H, L, B, T = 640, 220, 44, 28, 10
    pw, ph = W - L - 8, H - B - T
    colw = pw / max(1, len(days))
    bw = min(42, colw * 0.6)
    svg = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Расходы по дням">']
    for k in range(5):
        v = ymax * k / 4
        y = T + ph - ph * v / ymax
        svg.append(f'<line class="grid" x1="{L}" x2="{W - 8}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text class="ax" x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">${v:.2f}</text>')
    for i, d in enumerate(days):
        x = L + colw * i + (colw - bw) / 2
        y0 = T + ph
        parts = an.daily_cost.get(d, {})
        tip_parts = [f"{cfg.teams.get(t, t)} ${parts[t]:.2f}" for t in teams if parts.get(t)]
        tip = f"{d[8:10]}.{d[5:7]}: ${sum(parts.values()):.2f}" + (" — " + ", ".join(tip_parts) if tip_parts else "")
        segs = [(t, parts[t]) for t in teams if parts.get(t, 0) > 0]
        for j, (t, v) in enumerate(segs):
            h = ph * v / ymax
            gap = 2 if j < len(segs) - 1 else 0
            svg.append(f'<rect x="{x:.1f}" y="{y0 - h:.1f}" width="{bw:.1f}" height="{max(0.5, h - gap):.1f}" '
                       f'rx="{4 if j == len(segs) - 1 else 0}" style="fill:var(--series-{team_slot[t]})"/>')
            y0 -= h
        svg.append(f'<rect class="hit" x="{L + colw * i:.1f}" y="{T}" width="{colw:.1f}" height="{ph}" data-tip="{e(tip)}"/>')
        svg.append(f'<text class="ax" x="{L + colw * i + colw / 2:.1f}" y="{H - 8}" text-anchor="middle">{d[8:10]}.{d[5:7]}</text>')
    if daily_cap > 0:
        y = T + ph - ph * daily_cap / ymax
        svg.append(f'<line class="cap" x1="{L}" x2="{W - 8}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text class="ax cap-t" x="{W - 10}" y="{y - 5:.1f}" text-anchor="end">дневной лимит ${daily_cap:.2f}</text>')
    svg.append(f'<line class="base" x1="{L}" x2="{W - 8}" y1="{T + ph}" y2="{T + ph}"/></svg>')

    # --- table
    rows = []
    for s in bots_sorted:
        a = allocs.get(s.bot)
        wk = a.weekly_usd if a else 0
        used = 100 * s.cost_usd / wk if wk else 0
        rows.append(
            "<tr>"
            f"<td>{e(s.label)}</td><td>{e(cfg.teams.get(s.team, s.team))}</td>"
            f"<td class=n>{s.calls}</td><td class=n>{s.tokens:,}</td><td class=n>${s.cost_usd:.2f}</td>"
            f"<td class=n>{s.share_percent:.1f}%</td><td class=n>${wk:.2f}</td><td class=n>${(a.daily_cap_usd if a else 0):.2f}</td>"
            f"<td class=n>{used:.0f}%</td><td class=n>{s.waste_percent:.0f}%</td>"
            f"<td>{_chip(s.verdict)}</td></tr>"
        )
        if s.reasons:
            rows.append(f'<tr class="why"><td colspan="11">↳ {e("; ".join(s.reasons))}</td></tr>')

    banner = ('<div class="banner stop">■ Вся команда остановлена — новые запросы к Grok блокируются. '
              'Запуск: <code>python -m grok_budget resume</code></div>') if stopped else ""
    demo_banner = '<div class="banner demo">ДЕМО-ДАННЫЕ — сгенерированы для примера, не реальные расходы</div>' if demo else ""

    data_json = json.dumps({
        "period": [an.since, an.until], "total_cost": an.total_cost,
        "bots": {s.bot: {"cost": s.cost_usd, "share": s.share_percent, "verdict": s.verdict} for s in bots_sorted},
    }, ensure_ascii=False)

    return TEMPLATE.format(
        period=f"{_d(an.since)} — {_d(an.until)}",
        generated=time.strftime("%d.%m.%Y %H:%M"),
        banner=banner + demo_banner,
        spent=f"${an.total_cost:.2f}",
        budget=f"${cfg.distributable_usd:.2f}",
        used_pct=f"{used_pct:.0f}%",
        used_w=f"{min(100, used_pct):.1f}",
        meter="critical" if used_pct > 100 else "series-1",
        reserve=f"${cfg.reserve_usd:.2f}",
        reserve_pct=f"{cfg.reserve_percent:g}%",
        weekly=f"${cfg.weekly_budget_usd:.2f}",
        waste=f"{an.waste_percent:.1f}%",
        waste_usd=f"${an.total_waste:.2f}",
        avg_daily=f"${p['avg_daily']:.2f}",
        lasts=lasts_txt,
        lasts_status=lasts_status,
        plan_days=f"{cfg.plan_days:g}",
        bars="\n".join(bar_rows),
        team_segs="".join(team_segs) or '<span class="seg empty" style="flex:1"></span>',
        team_legend="".join(team_legend),
        svg="".join(svg),
        rows="\n".join(rows),
        data_json=data_json,
    )


def _chip(verdict: str) -> str:
    status = {"OK": "good", "IDLE": "muted", "TRIM": "warning", "CACHE": "warning",
              "REDUCE": "serious", "DISABLE": "critical"}.get(verdict, "muted")
    icon = {"good": "✓", "warning": "!", "serious": "▼", "critical": "✕", "muted": "○"}[status]
    return f'<span class="chip {status}"><b>{icon}</b>{html.escape(VERDICT_TEXT.get(verdict, verdict))}</span>'


def write_html(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Grok Weekly Usage</title>
<style>
:root{{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--base:#c3c2b7;--ring:rgba(11,11,11,.10);
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100;--series-5:#e87ba4;--series-6:#008300;--series-7:#6250d6;--series-8:#e34948;
--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--grid:#2c2c2a;--base:#383835;--ring:rgba(255,255,255,.10);
--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;--series-6:#0ca30c;--series-7:#9085e9;--series-8:#e66767}}}}
:root[data-theme="dark"]{{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--grid:#2c2c2a;--base:#383835;--ring:rgba(255,255,255,.10);
--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;--series-6:#0ca30c;--series-7:#9085e9;--series-8:#e66767}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:24px 16px 48px}}
h1{{font-size:22px;margin:0 0 2px}} h2{{font-size:15px;margin:0 0 12px}}
.sub{{color:var(--ink2);margin:0 0 18px}}
.banner{{padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}}
.banner.stop{{background:color-mix(in srgb,var(--critical) 15%,var(--surface));border:1px solid var(--critical)}}
.banner.demo{{background:color-mix(in srgb,var(--warning) 18%,var(--surface));border:1px solid var(--warning)}}
code{{font-family:ui-monospace,monospace;font-size:12px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}}
.card{{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:16px}}
.kpi .l{{color:var(--ink2);font-size:12px}} .kpi .v{{font-size:28px;font-weight:650;font-variant-numeric:tabular-nums;margin:2px 0}}
.kpi .s{{color:var(--muted);font-size:12px}}
.meter{{height:6px;border-radius:3px;background:var(--grid);margin-top:8px;overflow:hidden}}
.meter span{{display:block;height:100%;background:var(--series-1);border-radius:3px}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}}
.dot.good{{background:var(--good)}} .dot.warning{{background:var(--warning)}} .dot.critical{{background:var(--critical)}}
.grid2{{display:grid;grid-template-columns:1.1fr 1fr;gap:12px;margin-bottom:12px}}
@media (max-width:820px){{.grid2{{grid-template-columns:1fr}}}}
.bar-row{{display:grid;grid-template-columns:140px 1fr 52px;align-items:center;gap:8px;padding:5px 0;cursor:default}}
.bar-row:hover .bar{{filter:brightness(1.1)}}
.bar-label{{color:var(--ink2);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.bar-track{{position:relative;height:14px}}
.bar{{position:absolute;left:0;top:1px;height:12px;background:var(--series-1);border-radius:0 4px 4px 0}}
.alloc-mark{{position:absolute;top:-2px;width:2px;height:18px;background:var(--ink);opacity:.55}}
.bar-val{{text-align:right;font-variant-numeric:tabular-nums}}
.note{{color:var(--muted);font-size:12px;margin-top:8px}}
.stack{{display:flex;gap:2px;height:22px;margin:4px 0 12px}}
.seg{{border-radius:0}} .seg:first-child{{border-radius:4px 0 0 4px}} .seg:last-child{{border-radius:0 4px 4px 0}}
.seg.empty{{background:var(--grid);border-radius:4px}}
.lg{{display:inline-flex;align-items:center;gap:6px;margin:0 14px 6px 0;color:var(--ink2);font-size:13px}}
.lg i{{width:10px;height:10px;border-radius:2px;display:inline-block}} .lg b{{color:var(--ink)}}
svg{{width:100%;height:auto;display:block}}
svg .grid{{stroke:var(--grid);stroke-width:1}} svg .base{{stroke:var(--base);stroke-width:1}}
svg .ax{{fill:var(--muted);font-size:11px}} svg .cap{{stroke:var(--ink2);stroke-width:1.5;stroke-dasharray:4 4}}
svg .cap-t{{fill:var(--ink2)}} svg .hit{{fill:transparent}} svg .hit:hover{{fill:var(--ink);fill-opacity:.04}}
.tw{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px;border-bottom:1px solid var(--grid);text-align:left;vertical-align:top}}
th{{color:var(--ink2);font-weight:600;white-space:nowrap}} td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
tr.why td{{color:var(--ink2);font-size:12px;padding-top:0}} tr:has(+tr.why) td{{border-bottom:0}}
.chip{{display:inline-flex;gap:5px;align-items:center;white-space:nowrap;padding:2px 8px;border-radius:999px;border:1px solid var(--ring);font-size:12px}}
.chip b{{font-size:11px}} .chip.good b{{color:var(--good)}} .chip.warning b{{color:var(--warning)}}
.chip.serious b{{color:var(--serious)}} .chip.critical b{{color:var(--critical)}} .chip.muted b{{color:var(--muted)}}
#tip{{position:fixed;pointer-events:none;z-index:9;background:var(--surface);color:var(--ink);border:1px solid var(--ring);
border-radius:8px;padding:6px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.15);display:none;max-width:320px}}
</style></head>
<body><main>
<h1>Grok — недельный расход</h1>
<p class="sub">Период {period} · сформировано {generated}</p>
{banner}
<section class="kpis">
 <div class="card kpi"><div class="l">Потрачено из бюджета ботов</div><div class="v">{spent}</div>
  <div class="s">из {budget} · {used_pct}</div><div class="meter"><span style="width:{used_w}%;background:var(--{meter})"></span></div></div>
 <div class="card kpi"><div class="l">Резерв (не раздаётся ботам)</div><div class="v">{reserve}</div>
  <div class="s">{reserve_pct} от недельных {weekly}</div></div>
 <div class="card kpi"><div class="l">Бюджета хватит при текущем темпе</div><div class="v"><span class="dot {lasts_status}"></span>{lasts}</div>
  <div class="s">{avg_daily} в день · план {plan_days} дн.</div></div>
 <div class="card kpi"><div class="l">Потери (ошибки, пустые, повторы, бесполезное)</div><div class="v">{waste}</div>
  <div class="s">{waste_usd} за неделю</div></div>
</section>
<section class="grid2">
 <div class="card"><h2>Доля расходов по ботам</h2>{bars}
  <div class="note">Полоса — доля фактических расходов за 7 дней. Вертикальная метка — выделенный лимит.</div></div>
 <div class="card"><h2>Доля по командам</h2><div class="stack">{team_segs}</div>{team_legend}
  <h2 style="margin-top:18px">Расходы по дням</h2>{svg}</div>
</section>
<section class="card"><h2>Боты: расход, лимиты и рекомендации</h2><div class="tw"><table>
<thead><tr><th>Бот</th><th>Команда</th><th class=n>Вызовы</th><th class=n>Токены</th><th class=n>Расход</th><th class=n>Доля</th>
<th class=n>Лимит/нед</th><th class=n>Лимит/день</th><th class=n>Исп. лимита</th><th class=n>Потери</th><th>Вердикт</th></tr></thead>
<tbody>{rows}</tbody></table></div></section>
</main><div id="tip"></div>
<script type="application/json" id="data">{data_json}</script>
<script>
(function(){{var t=document.getElementById('tip');
document.addEventListener('mousemove',function(ev){{var el=ev.target.closest('[data-tip]');
if(!el){{t.style.display='none';return}}t.textContent=el.getAttribute('data-tip');t.style.display='block';
var x=ev.clientX+14,y=ev.clientY+14;if(x+t.offsetWidth>innerWidth-8)x=ev.clientX-t.offsetWidth-14;
t.style.left=x+'px';t.style.top=y+'px'}});}})();
</script></body></html>
"""
