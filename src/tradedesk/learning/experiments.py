"""Improvement pipeline: PROPOSAL -> BACKTEST -> OUT_OF_SAMPLE -> BASELINE_COMPARISON -> AWAITING_APPROVAL
-> (OWNER) APPROVED -> new strategy version (APPROVED_PAPER) -> (OWNER) activation = paper rollout.

Nothing here changes active rules automatically.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta

from ..backtest.engine import BacktestConfig, walk_forward
from ..db import database as db
from ..strategies.library import ALL_STRATEGIES
from . import registry

MIN_OOS_TRADES = 30


def _summary(wf: dict) -> dict:
    return {"oos_metrics": wf.get("oos_metrics"), "bootstrap": wf.get("bootstrap"), "monte_carlo": wf.get("monte_carlo"),
            "positive_folds": wf.get("positive_folds"), "n_folds": len(wf.get("folds", [])),
            "folds": [{k: f[k] for k in ("fold", "test_from", "test_to", "chosen_params", "train_expectancy_r")} |
                      {"test_expectancy_r": f["test_metrics"]["expectancy_r"], "test_n": f["test_metrics"]["n_trades"]}
                      for f in wf.get("folds", [])],
            "error": wf.get("error")}


def run_evidence(conn: sqlite3.Connection, data_service, strategy_id: str, version: str, symbol: str,
                 end_utc: datetime, days: int = 150, folds: int = 4) -> dict:
    """Walk-forward OOS evidence for an existing version (no parameter change)."""
    row = conn.execute("SELECT params FROM strategy_versions WHERE strategy_id=? AND version=?", (strategy_id, version)).fetchone()
    if row is None:
        raise registry.RegistryError(f"unknown version {strategy_id}@{version}")
    params = json.loads(row["params"])
    m5 = data_service.m5(symbol, end_utc - timedelta(days=days), end_utc)
    spec = data_service.spec(symbol)
    cls = ALL_STRATEGIES[strategy_id]
    # evidence run uses the version's fixed params: grid disabled by an empty grid on a subclass
    fixed = type(f"{cls.__name__}Fixed", (cls,), {"param_grid": {}})
    wf = walk_forward(fixed, m5, spec, BacktestConfig(), folds=folds, base_params=params, data_is_mock=spec.source.is_mock)
    summ = _summary(wf)
    eid = f"exp-{uuid.uuid4().hex[:10]}"
    conn.execute("INSERT INTO experiments(experiment_id, created_utc, hypothesis, stage, strategy_id, baseline_version,"
                 " candidate_version, dataset, oos_metrics, conclusion) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (eid, db.utc_now_iso(), f"evidence: walk-forward OOS of {strategy_id}@{version}", "OUT_OF_SAMPLE",
                  strategy_id, version, version,
                  json.dumps({"symbol": symbol, "days": days, "end_utc": end_utc.isoformat(), "source": data_service.source,
                              "is_mock": spec.source.is_mock, "folds": folds}),
                  json.dumps(summ), _conclusion(summ)))
    db.audit(conn, "QUANT", "EXPERIMENT_EVIDENCE", entity="experiments", entity_id=eid, details={"strategy": strategy_id})
    return {"experiment_id": eid, **summ, "conclusion": _conclusion(summ)}


def _conclusion(summ: dict) -> str:
    o = summ.get("oos_metrics") or {}
    if summ.get("error"):
        return f"INCONCLUSIVE: {summ['error']}"
    n = o.get("n_trades") or 0
    ci = (summ.get("bootstrap") or {}).get("ci95")
    mock = " (MOCK DATA — not evidence about real markets)" if o.get("data_is_mock") else ""
    if n < MIN_OOS_TRADES:
        return f"INSUFFICIENT SAMPLE: {n} OOS trades < {MIN_OOS_TRADES}{mock}"
    if ci and ci[0] > 0:
        return f"OOS expectancy {o.get('expectancy_r')}R, CI95 {ci} above 0{mock}"
    return f"NO EDGE DEMONSTRATED: OOS expectancy {o.get('expectancy_r')}R, CI95 {ci}{mock}"


def latest_evidence(conn: sqlite3.Connection, strategy_id: str, version: str) -> tuple[str | None, dict | None]:
    r = conn.execute("SELECT experiment_id, oos_metrics FROM experiments WHERE strategy_id=? AND candidate_version=?"
                     " AND oos_metrics IS NOT NULL ORDER BY id DESC LIMIT 1", (strategy_id, version)).fetchone()
    return (r["experiment_id"], json.loads(r["oos_metrics"])) if r else (None, None)


def propose(conn: sqlite3.Connection, data_service, strategy_id: str, baseline_version: str, candidate_params: dict,
            hypothesis: str, symbol: str, end_utc: datetime, days: int = 150, folds: int = 4) -> dict:
    """Compare a parameter change against the baseline on identical data, both walk-forward OOS."""
    cls = ALL_STRATEGIES[strategy_id]
    cls(**candidate_params)
    base = conn.execute("SELECT params FROM strategy_versions WHERE strategy_id=? AND version=?",
                        (strategy_id, baseline_version)).fetchone()
    if base is None:
        raise registry.RegistryError("unknown baseline version")
    eid = f"exp-{uuid.uuid4().hex[:10]}"
    conn.execute("INSERT INTO experiments(experiment_id, created_utc, hypothesis, stage, strategy_id, baseline_version,"
                 " dataset) VALUES (?,?,?,?,?,?,?)", (eid, db.utc_now_iso(), hypothesis, "PROPOSAL", strategy_id,
                                                      baseline_version, json.dumps({"candidate_params": candidate_params})))
    m5 = data_service.m5(symbol, end_utc - timedelta(days=days), end_utc)
    spec = data_service.spec(symbol)
    fixed = type(f"{cls.__name__}Fixed", (cls,), {"param_grid": {}})
    base_wf = _summary(walk_forward(fixed, m5, spec, folds=folds, base_params=json.loads(base["params"]),
                                    data_is_mock=spec.source.is_mock))
    cand_wf = _summary(walk_forward(fixed, m5, spec, folds=folds, base_params=candidate_params,
                                    data_is_mock=spec.source.is_mock))
    bo, co = base_wf.get("oos_metrics") or {}, cand_wf.get("oos_metrics") or {}
    better = (co.get("n_trades") or 0) >= MIN_OOS_TRADES and (co.get("expectancy_r") or -9) > (bo.get("expectancy_r") or -9) \
        and ((cand_wf.get("bootstrap") or {}).get("ci95") or [-1])[0] > 0
    stage = "AWAITING_APPROVAL" if better else "REJECTED"
    concl = ("candidate beats baseline OOS; awaiting OWNER approval" if better else
             "candidate does not beat baseline with sufficient OOS evidence") + \
        (" (MOCK DATA)" if spec.source.is_mock else "")
    conn.execute("UPDATE experiments SET stage=?, oos_metrics=?, baseline_metrics=?, conclusion=?, dataset=? WHERE experiment_id=?",
                 (stage, json.dumps(cand_wf), json.dumps(base_wf), concl,
                  json.dumps({"candidate_params": candidate_params, "symbol": symbol, "days": days,
                              "end_utc": end_utc.isoformat(), "is_mock": spec.source.is_mock}), eid))
    db.audit(conn, "QUANT", "EXPERIMENT_EVALUATED", entity="experiments", entity_id=eid, details={"stage": stage})
    return {"experiment_id": eid, "stage": stage, "conclusion": concl, "baseline": bo, "candidate": co}


def approve(conn: sqlite3.Connection, experiment_id: str, new_version: str, actor: str) -> dict:
    if actor != registry.OWNER:
        raise registry.RegistryError("only the OWNER may approve experiments")
    e = conn.execute("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
    if e is None or e["stage"] != "AWAITING_APPROVAL":
        raise registry.RegistryError("experiment is not awaiting approval")
    params = json.loads(e["dataset"])["candidate_params"]
    registry.add_version(conn, e["strategy_id"], new_version, params, e["baseline_version"],
                         f"from {experiment_id}: {e['hypothesis']}", actor)
    conn.execute("UPDATE experiments SET stage='APPROVED', candidate_version=?, approved_by=?, approved_utc=? WHERE experiment_id=?",
                 (new_version, actor, db.utc_now_iso(), experiment_id))
    return {"experiment_id": experiment_id, "strategy_id": e["strategy_id"], "new_version": new_version,
            "status": "APPROVED_PAPER (activate explicitly to roll out)"}
