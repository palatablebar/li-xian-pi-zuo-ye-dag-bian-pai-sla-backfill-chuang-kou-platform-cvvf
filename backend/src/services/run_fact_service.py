"""运行明细整理：把混在一起的原始调度记录归并成「每作业每业务日期一条有效记录」。

规则（确定性，同样的输入永远得到同样的输出）：
  1. 按 (job_id, biz_date) 分组，组内按 attempt 分组。
  2. 同一 attempt 内：record_version 最大（并列取 ingested_at 最晚）的为有效版本，
     其余拒绝为「旧版本记录」；若最高版本出现多条且时间不一致，
     说明调度器自己都说不清这次跑成什么样 → 整组标记「最终记录冲突」。
  3. 跨 attempt：最大 attempt 的代表为有效记录，其余 attempt 拒绝为「被重试取代」。
  4. 有效记录 start/end 缺失或 end < start → 时间倒挂，整组标记冲突，不出有效记录。
  5. 有效记录 ingested_at 晚于 end_at 超过 CHECK_LATE_RECORD_HOURS → 标记「迟到记录」
     （仍是有效证据，但要在页面上点名）。

输出三部分：facts（有效明细）/ rejected（被拒绝记录 + 原因）/ conflicts（时间证据冲突）。
样本不足或时间证据冲突时，上层（dep_check_service）保留为待核，这里不下任何结论。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import RejectReason
from src.constants.limits import CHECK_LATE_RECORD_HOURS
from src.models.job import Job
from src.models.job_run_record import JobRunRecord
from src.models.pipeline import Pipeline


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _record_view(record: JobRunRecord, jobs: dict[int, Job]) -> dict:
    job = jobs.get(record.job_id)
    return {
        "record_id": record.id,
        "job_id": record.job_id,
        "job_code": job.code if job else "",
        "job_name": job.name if job else "",
        "biz_date": record.biz_date.isoformat(),
        "attempt": record.attempt,
        "record_version": record.record_version,
        "status": record.status,
        "start_at": _iso(record.start_at),
        "end_at": _iso(record.end_at),
        "ingested_at": _iso(record.ingested_at),
        "source": record.source,
    }


def normalize_records(records: list[JobRunRecord], jobs: dict[int, Job]) -> dict:
    """纯函数：同一链路（或任意子集）的原始记录 → 有效明细 + 拒绝列表 + 冲突列表。"""
    groups: dict[tuple[int, date], list[JobRunRecord]] = {}
    for record in records:
        groups.setdefault((record.job_id, record.biz_date), []).append(record)

    facts: dict[tuple[int, date], dict] = {}
    rejected: list[dict] = []
    conflicts: dict[tuple[int, date], dict] = {}

    for (job_id, biz_date), group in sorted(groups.items(), key=lambda item: (item[0][1], item[0][0])):
        # 第 2 步：attempt 内部先决出版本代表
        by_attempt: dict[int, list[JobRunRecord]] = {}
        for record in group:
            by_attempt.setdefault(record.attempt, []).append(record)

        attempt_final: dict[int, JobRunRecord] = {}
        conflict_reason: str | None = None
        conflict_detail: dict | None = None

        for attempt, rows in sorted(by_attempt.items()):
            best_version = max(row.record_version for row in rows)
            top_rows = [row for row in rows if row.record_version == best_version]
            for row in rows:
                if row.record_version < best_version:
                    rejected.append(
                        {
                            "record": row,
                            "reason": RejectReason.OUTDATED_VERSION.value,
                            "detail": f"attempt {attempt} 已有 v{best_version} 上报，v{row.record_version} 作废",
                        }
                    )
            timelines = {(row.start_at, row.end_at) for row in top_rows}
            if len(top_rows) > 1 and len(timelines) > 1:
                # 同 attempt 同版本却报出不同时间：证据自相矛盾，无法取信任何一条
                conflict_reason = RejectReason.CONFLICTING_FINAL.value
                conflict_detail = {
                    "attempt": attempt,
                    "record_version": best_version,
                    "variants": [
                        {"start_at": _iso(row.start_at), "end_at": _iso(row.end_at), "record_id": row.id}
                        for row in sorted(top_rows, key=lambda item: item.id)
                    ],
                }
                break
            attempt_final[attempt] = max(
                top_rows, key=lambda row: (row.ingested_at, row.id)
            )

        if conflict_reason is not None:
            conflicts[(job_id, biz_date)] = {
                "reason": conflict_reason,
                "detail": conflict_detail,
                "records": list(group),
            }
            continue

        # 第 3 步：最大 attempt 为有效记录
        final_attempt = max(attempt_final)
        final_record = attempt_final[final_attempt]
        for attempt, record in attempt_final.items():
            if attempt != final_attempt:
                rejected.append(
                    {
                        "record": record,
                        "reason": RejectReason.SUPERSEDED_BY_RETRY.value,
                        "detail": f"第 {final_attempt} 次尝试已覆盖第 {attempt} 次（{record.status}）",
                    }
                )

        # 第 4 步：时间倒挂 / 缺时间 → 冲突，不出有效记录
        if final_record.start_at is None or final_record.end_at is None:
            conflicts[(job_id, biz_date)] = {
                "reason": RejectReason.TIME_INVERSION.value,
                "detail": {"attempt": final_attempt, "missing": "start_at/end_at 缺失"},
                "records": list(group),
            }
            continue
        if final_record.end_at < final_record.start_at:
            conflicts[(job_id, biz_date)] = {
                "reason": RejectReason.TIME_INVERSION.value,
                "detail": {
                    "attempt": final_attempt,
                    "start_at": _iso(final_record.start_at),
                    "end_at": _iso(final_record.end_at),
                },
                "records": list(group),
            }
            continue

        # 第 5 步：迟到记录标记
        lag_seconds = (final_record.ingested_at - final_record.end_at).total_seconds()
        is_late_record = lag_seconds > CHECK_LATE_RECORD_HOURS * 3600
        facts[(job_id, biz_date)] = {
            "record": final_record,
            "attempt": final_attempt,
            "is_late_record": is_late_record,
            "ingest_lag_minutes": max(0, round(lag_seconds / 60)),
        }

    job_ids = {job_id for job_id, _ in facts} | {job_id for job_id, _ in conflicts}
    job_map = {job_id: jobs.get(job_id) for job_id in job_ids}
    return {
        "facts": facts,
        "rejected": [
            {
                **_record_view(item["record"], jobs),
                "reject_reason": item["reason"],
                "reject_detail": item["detail"],
            }
            for item in sorted(rejected, key=lambda item: item["record"].id)
        ],
        "conflicts": [
            {
                "job_id": job_id,
                "job_code": job_map[job_id].code if job_map.get(job_id) else "",
                "job_name": job_map[job_id].name if job_map.get(job_id) else "",
                "biz_date": biz_date.isoformat(),
                "reason": payload["reason"],
                "detail": payload["detail"],
            }
            for (job_id, biz_date), payload in sorted(conflicts.items(), key=lambda item: (item[0][1], item[0][0]))
        ],
    }


def load_pipeline_records(db: Session, pipeline: Pipeline) -> tuple[dict[int, Job], list[JobRunRecord]]:
    jobs = {
        job.id: job
        for job in db.scalars(select(Job).where(Job.pipeline_id == pipeline.id)).all()
    }
    records = db.scalars(
        select(JobRunRecord)
        .where(JobRunRecord.pipeline_id == pipeline.id)
        .order_by(JobRunRecord.biz_date, JobRunRecord.job_id, JobRunRecord.attempt, JobRunRecord.record_version)
    ).all()
    return jobs, list(records)


def pipeline_run_facts(db: Session, pipeline: Pipeline) -> dict:
    """接口层：按链路整理运行明细（可选业务日期由调用方过滤）。"""
    jobs, records = load_pipeline_records(db, pipeline)
    normalized = normalize_records(records, jobs)
    facts = []
    for (job_id, biz_date), fact in sorted(normalized["facts"].items(), key=lambda item: (item[0][1], item[0][0])):
        record = fact["record"]
        job = jobs.get(record.job_id)
        facts.append(
            {
                "job_id": record.job_id,
                "job_code": job.code if job else "",
                "job_name": job.name if job else "",
                "biz_date": biz_date.isoformat(),
                "attempt": fact["attempt"],
                "status": record.status,
                "start_at": _iso(record.start_at),
                "end_at": _iso(record.end_at),
                "ingested_at": _iso(record.ingested_at),
                "is_late_record": fact["is_late_record"],
                "ingest_lag_minutes": fact["ingest_lag_minutes"],
            }
        )
    return {
        "pipeline_code": pipeline.code,
        "pipeline_name": pipeline.name,
        "facts": facts,
        "rejected": normalized["rejected"],
        "conflicts": normalized["conflicts"],
        "summary": {
            "raw_records": len(records),
            "fact_days": len(facts),
            "rejected": len(normalized["rejected"]),
            "conflict_days": len(normalized["conflicts"]),
        },
    }
