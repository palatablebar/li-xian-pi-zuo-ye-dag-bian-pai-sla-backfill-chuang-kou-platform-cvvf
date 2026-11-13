"""依赖体检：对声明依赖逐条下结论，为漏登记的作业对找出补边候选。

判定口径（阈值集中在 constants/limits.py，算法版本 CHECK_ALGO_VERSION）：

  现有依赖边（按链路 + 业务日期配对有效记录）：
    冲突日期占比 > CHECK_CONFLICT_RATIO_PENDING → 待核（时间证据冲突）
    可配对样本 < CHECK_MIN_SAMPLE_DAYS          → 待核（样本不足）
    遵守率 ≥ CHECK_OBEY_RATIO_EFFECTIVE          → 有效
    违背率 ≥ CHECK_VIOLATE_RATIO_LONG_TERM       → 长期违背
    其余（遵守与违背混杂）                        → 证据不足
  遵守 = 下游起跑时刻 ≥ 上游结束时刻 + lag_minutes。

  补边候选（同一链路、声明图中双向不可达的作业对）：
    样本不足 / 冲突超限                          → 待核（保留，不下结论）
    相接占比 ≥ CHECK_ADJACENT_RATIO_SUGGEST      → 建议补边，附逐日反例
    其余                                          → 不稳定，不列出
  相接 = 下游起跑 − 上游结束 ∈ [0, CHECK_ADJACENT_GAP_MINUTES] 分钟。

批次生命周期：草稿 → 整批发布（事务，失败回滚保留旧版）；
同指纹重复发起复用旧批次；上游记录变化后旧结论失效，需重新检查。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CHECK_BATCH_STATUS_LABELS,
    DEP_TYPE_LABELS,
    REJECT_REASON_LABELS,
    VERDICT_LABELS,
    CheckBatchStatus,
    CheckItemKind,
    CheckVerdict,
    JobStatus,
)
from src.constants.limits import (
    CHECK_ADJACENT_GAP_MINUTES,
    CHECK_ADJACENT_RATIO_SUGGEST,
    CHECK_ALGO_VERSION,
    CHECK_CONFLICT_RATIO_PENDING,
    CHECK_MIN_SAMPLE_DAYS,
    CHECK_OBEY_RATIO_EFFECTIVE,
    CHECK_VIOLATE_RATIO_LONG_TERM,
    CHECK_WINDOW_DAYS,
)
from src.models.dep_check import DepCheckBatch, DepCheckItem
from src.models.dependency import Dependency
from src.models.job import Job
from src.models.job_run_record import JobRunRecord
from src.models.pipeline import Pipeline
from src.services import run_fact_service
from src.utils.errors import AppError
from src.utils.timeutil import local_now, utcnow


# ---------------------------------------------------------------- 指纹


def compute_fingerprint(db: Session) -> str:
    """上游记录 + 依赖配置 + 作业状态 + 算法版本的稳定哈希。

    任何一项变化都意味着旧结论失效：记录新增/修正、依赖增删、作业上下线、
    算法口径升级（CHECK_ALGO_VERSION 递增）。
    """
    jobs = db.scalars(select(Job).order_by(Job.id)).all()
    edges = db.scalars(select(Dependency).order_by(Dependency.id)).all()
    records = db.scalars(select(JobRunRecord).order_by(JobRunRecord.id)).all()
    payload = {
        "algo": CHECK_ALGO_VERSION,
        "window_days": CHECK_WINDOW_DAYS,
        "jobs": [(job.id, job.code, job.status) for job in jobs],
        "edges": [
            (edge.id, edge.pipeline_id, edge.upstream_job_id, edge.downstream_job_id, edge.dep_type, edge.lag_minutes)
            for edge in edges
        ],
        "records": [
            (
                rec.id, rec.job_id, rec.biz_date.isoformat(), rec.attempt, rec.record_version,
                rec.status, _stamp(rec.start_at), _stamp(rec.end_at), _stamp(rec.ingested_at),
            )
            for rec in records
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _stamp(value) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


# ---------------------------------------------------------------- 图工具


def _reachability(edge_pairs: list[tuple[str, str]]) -> dict[str, set[str]]:
    """声明图的传递闭包：reach[a] = 从 a 沿声明路径可达的作业编码集合。"""
    adjacency: dict[str, set[str]] = {}
    nodes: set[str] = set()
    for upstream, downstream in edge_pairs:
        adjacency.setdefault(upstream, set()).add(downstream)
        nodes.add(upstream)
        nodes.add(downstream)
    reach: dict[str, set[str]] = {node: set() for node in nodes}
    for start in nodes:
        stack = list(adjacency.get(start, ()))
        while stack:
            node = stack.pop()
            if node in reach[start]:
                continue
            reach[start].add(node)
            stack.extend(adjacency.get(node, ()))
    return reach


def _clock(value) -> str:
    return value.strftime("%H:%M") if value else "--:--"


# ---------------------------------------------------------------- 边评估


def _window_dates(facts: dict, conflicts: dict, fallback_today: date) -> list[date]:
    """回看窗口：记录里实际出现的最近 CHECK_WINDOW_DAYS 个业务日期（降序）。"""
    seen = {biz_date for _, biz_date in facts} | {biz_date for _, biz_date in conflicts}
    dates = sorted(seen, reverse=True)[:CHECK_WINDOW_DAYS]
    return dates or [fallback_today]


def evaluate_edges(
    pipeline_code: str,
    jobs: list[dict],
    edges: list[dict],
    facts: dict,
    conflicts: dict,
    window_dates: list[date],
) -> list[dict]:
    """对每条声明边给出 有效 / 长期违背 / 证据不足 / 待核。"""
    code_of = {job["id"]: job["code"] for job in jobs}
    items: list[dict] = []
    for edge in edges:
        up_code = code_of.get(edge["upstream_id"])
        down_code = code_of.get(edge["downstream_id"])
        if not up_code or not down_code:
            continue
        up_id, down_id = edge["upstream_id"], edge["downstream_id"]
        lag = int(edge.get("lag_minutes") or 0)
        conflict_dates = [
            day for day in window_dates
            if (up_id, day) in conflicts or (down_id, day) in conflicts
        ]
        comparable = [
            day for day in window_dates
            if (up_id, day) in facts and (down_id, day) in facts
            and (up_id, day) not in conflicts and (down_id, day) not in conflicts
        ]
        pairs = []
        for day in comparable:
            up_end = facts[(up_id, day)]["record"].end_at
            down_start = facts[(down_id, day)]["record"].start_at
            gap = round((down_start - up_end).total_seconds() / 60)
            pairs.append(
                {
                    "biz_date": day.isoformat(),
                    "up_end": _clock(up_end),
                    "down_start": _clock(down_start),
                    "gap_minutes": gap,
                    "required_lag_minutes": lag,
                    "obey": gap >= lag,
                }
            )
        sample = len(pairs)
        obey_days = sum(1 for pair in pairs if pair["obey"])
        base = {
            "item_kind": CheckItemKind.EDGE.value,
            "pipeline_code": pipeline_code,
            "upstream_code": up_code,
            "downstream_code": down_code,
            "dep_type": edge.get("dep_type"),
            "lag_minutes": lag,
            "sample_days": sample,
            "support_days": obey_days,
            "evidence": pairs,
            "counterexamples": [],
            "conflict_dates": [day.isoformat() for day in conflict_dates],
        }

        conflict_ratio = len(conflict_dates) / max(1, len(window_dates))
        if conflict_ratio > CHECK_CONFLICT_RATIO_PENDING:
            base["verdict"] = CheckVerdict.PENDING.value
            base["reason"] = (
                f"{len(conflict_dates)}/{len(window_dates)} 个业务日期存在时间证据冲突"
                f"（{'、'.join(day.isoformat() for day in conflict_dates[:3])}等），保留待核"
            )
        elif sample < CHECK_MIN_SAMPLE_DAYS:
            base["verdict"] = CheckVerdict.PENDING.value
            base["reason"] = (
                f"仅 {sample} 个可配对业务日期（要求 ≥{CHECK_MIN_SAMPLE_DAYS}），样本不足，保留待核"
            )
        else:
            obey_ratio = obey_days / sample
            violate_ratio = 1 - obey_ratio
            if obey_ratio >= CHECK_OBEY_RATIO_EFFECTIVE:
                base["verdict"] = CheckVerdict.EFFECTIVE.value
                base["reason"] = f"{sample} 天样本中 {obey_days} 天遵守先后约束（{obey_ratio:.0%}）"
            elif violate_ratio >= CHECK_VIOLATE_RATIO_LONG_TERM:
                base["verdict"] = CheckVerdict.VIOLATED.value
                violated_dates = [pair["biz_date"] for pair in pairs if not pair["obey"]]
                base["reason"] = (
                    f"{sample} 天样本中 {sample - obey_days} 天下游抢跑（{violate_ratio:.0%}），"
                    f"长期违背：{'、'.join(violated_dates[:5])}等"
                )
            else:
                base["verdict"] = CheckVerdict.INSUFFICIENT.value
                base["reason"] = (
                    f"遵守 {obey_days} 天 / 违背 {sample - obey_days} 天，"
                    f"两种证据混杂，无法定性为有效或长期违背"
                )
        items.append(base)
    return items


# ---------------------------------------------------------------- 候选评估


def evaluate_candidates(
    pipeline_code: str,
    jobs: list[dict],
    edges: list[dict],
    facts: dict,
    conflicts: dict,
    window_dates: list[date],
) -> tuple[list[dict], dict]:
    """无声明路径却稳定前后相接的作业对 → 补边候选（附反例）。

    返回 (items, stats)：stats 记录评估过的对数与因声明路径/成环跳过的对数。
    """
    active_jobs = [job for job in jobs if job.get("status") != JobStatus.DEPRECATED.value]
    edge_pairs = [(edge["upstream_code"], edge["downstream_code"]) for edge in edges]
    reach = _reachability(edge_pairs)
    id_by_code = {job["code"]: job["id"] for job in active_jobs}
    codes = sorted(id_by_code)

    items: list[dict] = []
    evaluated = skipped_declared = 0
    for index, code_a in enumerate(codes):
        for code_b in codes[index + 1:]:
            # 任一方向已有声明路径 → 不是「漏登记」，跳过
            if code_b in reach.get(code_a, set()) or code_a in reach.get(code_b, set()):
                skipped_declared += 1
                continue
            evaluated += 1
            directions = []
            for upstream, downstream in ((code_a, code_b), (code_b, code_a)):
                up_id, down_id = id_by_code[upstream], id_by_code[downstream]
                conflict_dates = [
                    day for day in window_dates
                    if (up_id, day) in conflicts or (down_id, day) in conflicts
                ]
                comparable = [
                    day for day in window_dates
                    if (up_id, day) in facts and (down_id, day) in facts
                    and (up_id, day) not in conflicts and (down_id, day) not in conflicts
                ]
                pairs = []
                for day in comparable:
                    up_end = facts[(up_id, day)]["record"].end_at
                    down_start = facts[(down_id, day)]["record"].start_at
                    gap = round((down_start - up_end).total_seconds() / 60)
                    pairs.append(
                        {
                            "biz_date": day.isoformat(),
                            "up_end": _clock(up_end),
                            "down_start": _clock(down_start),
                            "gap_minutes": gap,
                            "adjacent": 0 <= gap <= CHECK_ADJACENT_GAP_MINUTES,
                        }
                    )
                adjacent_days = sum(1 for pair in pairs if pair["adjacent"])
                ratio = adjacent_days / len(pairs) if pairs else 0.0
                directions.append(
                    {
                        "upstream": upstream,
                        "downstream": downstream,
                        "pairs": pairs,
                        "adjacent_days": adjacent_days,
                        "ratio": ratio,
                        "conflict_dates": conflict_dates,
                    }
                )
            best = max(directions, key=lambda item: (item["ratio"], item["adjacent_days"]))
            sample = len(best["pairs"])
            conflict_ratio = len(best["conflict_dates"]) / max(1, len(window_dates))
            counterexamples = [
                {
                    "biz_date": pair["biz_date"],
                    "gap_minutes": pair["gap_minutes"],
                    "reason": "order_reversed" if pair["gap_minutes"] < 0 else "gap_too_large",
                    "detail": (
                        f"{best['downstream']} 起跑早于 {best['upstream']} 结束 {-pair['gap_minutes']} 分钟"
                        if pair["gap_minutes"] < 0
                        else f"间隔 {pair['gap_minutes']} 分钟，超出相接窗口 {CHECK_ADJACENT_GAP_MINUTES} 分钟"
                    ),
                }
                for pair in best["pairs"]
                if not pair["adjacent"]
            ]
            base = {
                "item_kind": CheckItemKind.CANDIDATE.value,
                "pipeline_code": pipeline_code,
                "upstream_code": best["upstream"],
                "downstream_code": best["downstream"],
                "dep_type": None,
                "lag_minutes": 0,
                "sample_days": sample,
                "support_days": best["adjacent_days"],
                "evidence": best["pairs"],
                "counterexamples": counterexamples,
                "conflict_dates": [day.isoformat() for day in best["conflict_dates"]],
            }
            if conflict_ratio > CHECK_CONFLICT_RATIO_PENDING or sample < CHECK_MIN_SAMPLE_DAYS:
                why = "时间证据冲突" if conflict_ratio > CHECK_CONFLICT_RATIO_PENDING else f"仅 {sample} 天样本"
                base["verdict"] = CheckVerdict.PENDING.value
                base["reason"] = f"{why}，无法判断是否稳定相接，保留待核"
                items.append(base)
            elif best["ratio"] >= CHECK_ADJACENT_RATIO_SUGGEST:
                base["verdict"] = CheckVerdict.SUGGESTED.value
                base["reason"] = (
                    f"无声明路径，但 {sample} 天样本中 {best['adjacent_days']} 天"
                    f"（{best['ratio']:.0%}）前后相接（间隔 ≤{CHECK_ADJACENT_GAP_MINUTES} 分钟），"
                    f"疑似漏登记；反例 {len(counterexamples)} 天见明细"
                )
                items.append(base)
            # 相接不稳定的对不列出（不是候选，也不留结论）
    stats = {
        "pairs_evaluated": evaluated,
        "pairs_skipped_declared": skipped_declared,
    }
    return items, stats


# ---------------------------------------------------------------- 批次装配


def _load_check_inputs(db: Session) -> list[dict]:
    """按链路装配体检输入：作业、声明边、整理后的有效记录与冲突。"""
    pipelines = db.scalars(select(Pipeline).order_by(Pipeline.sort_order, Pipeline.id)).all()
    jobs_all = {job.id: job for job in db.scalars(select(Job)).all()}
    bundles = []
    for pipeline in pipelines:
        jobs = [
            {
                "id": job.id,
                "code": job.code,
                "name": job.name,
                "status": job.status,
            }
            for job in jobs_all.values()
            if job.pipeline_id == pipeline.id
        ]
        edge_rows = db.scalars(
            select(Dependency).where(Dependency.pipeline_id == pipeline.id).order_by(Dependency.id)
        ).all()
        code_of = {job["id"]: job["code"] for job in jobs}
        edges = [
            {
                "upstream_id": edge.upstream_job_id,
                "downstream_id": edge.downstream_job_id,
                "upstream_code": code_of.get(edge.upstream_job_id, ""),
                "downstream_code": code_of.get(edge.downstream_job_id, ""),
                "dep_type": edge.dep_type,
                "lag_minutes": int(edge.lag_minutes or 0),
            }
            for edge in edge_rows
        ]
        _, records = run_fact_service.load_pipeline_records(db, pipeline)
        normalized = run_fact_service.normalize_records(records, jobs_all)
        conflict_index = {
            (entry["job_id"], date.fromisoformat(entry["biz_date"])): entry["reason"]
            for entry in normalized["conflicts"]
        }
        bundles.append(
            {
                "pipeline": pipeline,
                "jobs": jobs,
                "edges": edges,
                "facts": normalized["facts"],
                "conflicts": conflict_index,
                "rejected_count": len(normalized["rejected"]),
                "conflict_count": len(normalized["conflicts"]),
            }
        )
    return bundles


def _item_stats(items: list[dict]) -> dict:
    stats: dict[str, int] = {}
    for item in items:
        key = f"{item['item_kind']}:{item['verdict']}"
        stats[key] = stats.get(key, 0) + 1
    return stats


def run_check(db: Session, actor_email: str) -> dict:
    """发起体检：同指纹复用，不新增批次；指纹变化则作废旧草稿、开新草稿。"""
    fingerprint = compute_fingerprint(db)

    draft = db.scalar(
        select(DepCheckBatch).where(
            DepCheckBatch.status == CheckBatchStatus.DRAFT.value,
            DepCheckBatch.input_fingerprint == fingerprint,
        )
    )
    if draft is not None:
        return {"batch": draft, "reused": True, "message": "同一批检查已存在草稿，直接复用，不新增版本"}

    current = db.scalar(
        select(DepCheckBatch)
        .where(DepCheckBatch.status == CheckBatchStatus.PUBLISHED.value)
        .order_by(DepCheckBatch.version.desc())
        .limit(1)
    )
    if current is not None and current.input_fingerprint == fingerprint:
        return {"batch": current, "reused": True, "message": "上游记录与依赖配置未变化，当前发布结论仍然有效"}

    # 指纹变了：未发布的旧草稿全部失效
    stale_drafts = db.scalars(
        select(DepCheckBatch).where(DepCheckBatch.status == CheckBatchStatus.DRAFT.value)
    ).all()
    for old in stale_drafts:
        old.status = CheckBatchStatus.STALE.value
        old.note = "上游记录或依赖配置已变化，草稿结论失效"

    bundles = _load_check_inputs(db)
    today = local_now().date()
    all_items: list[dict] = []
    candidate_stats = {"pairs_evaluated": 0, "pairs_skipped_declared": 0}
    rejected_total = conflict_total = 0
    for bundle in bundles:
        window_dates = _window_dates(bundle["facts"], bundle["conflicts"], today)
        edge_items = evaluate_edges(
            bundle["pipeline"].code, bundle["jobs"], bundle["edges"],
            bundle["facts"], bundle["conflicts"], window_dates,
        )
        candidate_items, stats = evaluate_candidates(
            bundle["pipeline"].code, bundle["jobs"], bundle["edges"],
            bundle["facts"], bundle["conflicts"], window_dates,
        )
        all_items.extend(edge_items)
        all_items.extend(candidate_items)
        candidate_stats["pairs_evaluated"] += stats["pairs_evaluated"]
        candidate_stats["pairs_skipped_declared"] += stats["pairs_skipped_declared"]
        rejected_total += bundle["rejected_count"]
        conflict_total += bundle["conflict_count"]

    batch = DepCheckBatch(
        batch_no=f"DC-{today.strftime('%Y%m%d')}-{fingerprint[:8]}",
        scope="all",
        status=CheckBatchStatus.DRAFT.value,
        version=0,
        input_fingerprint=fingerprint,
        item_count=len(all_items),
        stats={
            **_item_stats(all_items),
            **candidate_stats,
            "rejected_records": rejected_total,
            "conflict_days": conflict_total,
            "window_days": CHECK_WINDOW_DAYS,
        },
        note="",
        created_by=actor_email,
    )
    db.add(batch)
    db.flush()
    for item in all_items:
        db.add(
            DepCheckItem(
                batch_id=batch.id,
                item_kind=item["item_kind"],
                pipeline_code=item["pipeline_code"],
                upstream_code=item["upstream_code"],
                downstream_code=item["downstream_code"],
                dep_type=item.get("dep_type"),
                lag_minutes=int(item.get("lag_minutes") or 0),
                verdict=item["verdict"],
                sample_days=item["sample_days"],
                support_days=item["support_days"],
                reason=item["reason"],
                evidence=item["evidence"],
                counterexamples=item["counterexamples"],
            )
        )
    db.flush()
    return {"batch": batch, "reused": False, "message": "已生成体检草稿，审核后可整批发布"}


# ---------------------------------------------------------------- 发布 / 查询


def publish_batch(db: Session, batch_id: int, actor_email: str) -> DepCheckBatch:
    """整批发布：事务内作废旧版、启用新版；任何失败回滚，旧版保留。"""
    batch = db.get(DepCheckBatch, batch_id)
    if batch is None:
        raise AppError("NOT_FOUND", 404, detail=f"batch={batch_id}")
    if batch.status != CheckBatchStatus.DRAFT.value:
        raise AppError(
            "BATCH_NOT_DRAFT", 409,
            detail=f"批次状态为 {CHECK_BATCH_STATUS_LABELS.get(batch.status, batch.status)}，只有草稿能发布",
        )
    fingerprint = compute_fingerprint(db)
    if batch.input_fingerprint != fingerprint:
        raise AppError(
            "BATCH_STALE", 409,
            detail="上游记录或依赖配置已变化，该草稿结论失效，请重新发起检查",
        )
    try:
        current = db.scalar(
            select(DepCheckBatch)
            .where(DepCheckBatch.status == CheckBatchStatus.PUBLISHED.value)
            .order_by(DepCheckBatch.version.desc())
            .limit(1)
        )
        next_version = (
            db.scalar(select(func.max(DepCheckBatch.version)).where(DepCheckBatch.version > 0)) or 0
        ) + 1
        if current is not None:
            current.status = CheckBatchStatus.SUPERSEDED.value
            current.note = f"被 v{next_version} 取代"
        batch.status = CheckBatchStatus.PUBLISHED.value
        batch.version = next_version
        batch.published_by = actor_email
        batch.published_at = utcnow()
        db.flush()
    except AppError:
        raise
    except Exception as exc:  # 发布失败：回滚，旧版保留
        db.rollback()
        raise AppError("PUBLISH_FAILED", 500, detail=str(exc)) from exc
    return batch


def _job_names(db: Session) -> dict[str, str]:
    return {job.code: job.name for job in db.scalars(select(Job)).all()}


def serialize_item(item: DepCheckItem, job_names: dict[str, str]) -> dict:
    return {
        "id": item.id,
        "item_kind": item.item_kind,
        "pipeline_code": item.pipeline_code,
        "upstream_code": item.upstream_code,
        "upstream_name": job_names.get(item.upstream_code, ""),
        "downstream_code": item.downstream_code,
        "downstream_name": job_names.get(item.downstream_code, ""),
        "dep_type": item.dep_type,
        "dep_type_label": DEP_TYPE_LABELS.get(item.dep_type or "", item.dep_type or "—"),
        "lag_minutes": item.lag_minutes,
        "verdict": item.verdict,
        "verdict_label": VERDICT_LABELS.get(item.verdict, item.verdict),
        "sample_days": item.sample_days,
        "support_days": item.support_days,
        "reason": item.reason,
        "evidence": item.evidence or [],
        "counterexamples": item.counterexamples or [],
    }


def serialize_batch(batch: DepCheckBatch, current_fingerprint: str | None = None) -> dict:
    stale = (
        batch.status == CheckBatchStatus.PUBLISHED.value
        and current_fingerprint is not None
        and batch.input_fingerprint != current_fingerprint
    )
    return {
        "id": batch.id,
        "batch_no": batch.batch_no,
        "scope": batch.scope,
        "status": batch.status,
        "status_label": CHECK_BATCH_STATUS_LABELS.get(batch.status, batch.status),
        "version": batch.version,
        "input_fingerprint": batch.input_fingerprint,
        "is_stale": stale,
        "item_count": batch.item_count,
        "stats": batch.stats or {},
        "note": batch.note or "",
        "created_by": batch.created_by,
        "created_at": batch.created_at.isoformat(timespec="seconds") if batch.created_at else None,
        "published_by": batch.published_by,
        "published_at": batch.published_at.isoformat(timespec="seconds") if batch.published_at else None,
    }


def list_batches(db: Session) -> dict:
    fingerprint = compute_fingerprint(db)
    rows = db.scalars(
        select(DepCheckBatch).order_by(DepCheckBatch.id.desc()).limit(50)
    ).all()
    return {
        "items": [serialize_batch(row, fingerprint) for row in rows],
        "current_fingerprint": fingerprint,
    }


def get_batch_detail(db: Session, batch_id: int) -> dict:
    batch = db.get(DepCheckBatch, batch_id)
    if batch is None:
        raise AppError("NOT_FOUND", 404, detail=f"batch={batch_id}")
    fingerprint = compute_fingerprint(db)
    job_names = _job_names(db)
    items = db.scalars(
        select(DepCheckItem)
        .where(DepCheckItem.batch_id == batch.id)
        .order_by(DepCheckItem.pipeline_code, DepCheckItem.item_kind, DepCheckItem.id)
    ).all()
    return {
        "batch": serialize_batch(batch, fingerprint),
        "items": [serialize_item(item, job_names) for item in items],
        "verdict_labels": VERDICT_LABELS,
        "reject_reason_labels": REJECT_REASON_LABELS,
    }


def current_published(db: Session) -> dict:
    """读回已发布结论：最新发布批次 + 是否已失效（上游记录变化）。"""
    fingerprint = compute_fingerprint(db)
    batch = db.scalar(
        select(DepCheckBatch)
        .where(DepCheckBatch.status == CheckBatchStatus.PUBLISHED.value)
        .order_by(DepCheckBatch.version.desc())
        .limit(1)
    )
    if batch is None:
        return {"has_current": False, "batch": None, "items": [], "current_fingerprint": fingerprint}
    job_names = _job_names(db)
    items = db.scalars(
        select(DepCheckItem)
        .where(DepCheckItem.batch_id == batch.id)
        .order_by(DepCheckItem.pipeline_code, DepCheckItem.item_kind, DepCheckItem.id)
    ).all()
    return {
        "has_current": True,
        "batch": serialize_batch(batch, fingerprint),
        "items": [serialize_item(item, job_names) for item in items],
        "current_fingerprint": fingerprint,
    }
