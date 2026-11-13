"""幂等 seed：3 账号 / 4 链路 / 24 作业 / 38 依赖 / 120 条历史运行 / 3 条 SLA /
30 天 × 24 作业的原始调度记录（含重试、迟到、旧版本剧本）。

自然键 upsert：
  users.email
  pipelines.code
  jobs.code
  dependencies(pipeline_id, upstream_job_id, downstream_job_id)
  sla_policies.pipeline_id
  pipeline_runs.seed_key = "seed:history:<pipeline>:<date>"
  job_run_records.seed_key = "seed:runrec:<job>:<date>:a<attempt>:v<version>"

重复执行不新增、不覆盖用户改过的数据（只在缺失时插入）。
历史运行总时长 = 链路 P50 与 P90 的 40% 分位差 + 按 (链路, 第几天) 查表的抖动，
不使用随机数，因此两次 seed 的数值完全一致。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select

from src.config.database import session_scope
from src.constants.enums import DependencyType, JobStatus, RunMode, RunStatus
from src.models.audit import AuditLog
from src.models.dependency import Dependency
from src.models.job import Job
from src.models.job_run_record import JobRunRecord
from src.models.pipeline import Pipeline
from src.models.pipeline_run import PipelineRun
from src.models.sla_policy import SlaPolicy
from src.models.user import User
from src.services import dag_service
from src.utils.security import hash_password
from src.utils.timeutil import clock_to_minutes, minutes_to_clock
from scripts.demo_config import (
    DEMO_PASSWORD,
    DEMO_USERS,
    DEPENDENCIES,
    HISTORY_ANCHOR,
    HISTORY_DAYS,
    HISTORY_FAILED_DAYS,
    HISTORY_JITTER,
    HISTORY_LATE_DAYS,
    JOB_SPECS,
    PIPELINES,
    RECORD_PLAYS,
    SLA_POLICIES,
)

SEED_AUDIT = [
    ("seed:audit:1", "admin@dagpilot.io", "admin", "auth.login", "user", "1", {"role": "admin"}),
    ("seed:audit:2", "admin@dagpilot.io", "admin", "dep.import", "dependency", "report",
     {"mode": "merge", "created": 0, "updated": 12, "removed": 0}),
]


def ensure_users(db) -> dict[str, User]:
    users: dict[str, User] = {}
    for email, display_name, role in DEMO_USERS:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                password_hash=hash_password(DEMO_PASSWORD),
                display_name=display_name,
                role=role,
            )
            db.add(user)
            db.flush()
        users[email] = user
    return users


def ensure_pipelines(db) -> dict[str, Pipeline]:
    result: dict[str, Pipeline] = {}
    for index, spec in enumerate(PIPELINES):
        pipeline = db.scalar(select(Pipeline).where(Pipeline.code == spec.code))
        if pipeline is None:
            pipeline = Pipeline(
                code=spec.code,
                name=spec.name,
                line_no=spec.line_no,
                line_color=spec.line_color,
                start_clock=spec.start_clock,
                crew=spec.crew,
                note=spec.note,
                sort_order=index,
            )
            db.add(pipeline)
            db.flush()
        result[spec.code] = pipeline
    return result


def ensure_jobs(db, pipelines: dict[str, Pipeline]) -> dict[str, Job]:
    result: dict[str, Job] = {}
    for spec in JOB_SPECS:
        job = db.scalar(select(Job).where(Job.code == spec.code))
        if job is None:
            job = Job(
                code=spec.code,
                name=spec.name,
                pipeline_id=pipelines[spec.pipeline].id,
                layer=spec.layer,
                owner=spec.owner,
                target_table=spec.target_table,
                p50_seconds=spec.p50_minutes * 60,
                p90_seconds=spec.p90_minutes * 60,
                status=JobStatus.ACTIVE.value,
                note=f"历史 {HISTORY_DAYS} 天 P50 {spec.p50_minutes}m / P90 {spec.p90_minutes}m",
            )
            db.add(job)
            db.flush()
        result[spec.code] = job
    return result


def ensure_dependencies(db, pipelines: dict[str, Pipeline], jobs: dict[str, Job]) -> int:
    created = 0
    for spec in DEPENDENCIES:
        pipeline = pipelines[spec.pipeline]
        upstream = jobs[spec.upstream]
        downstream = jobs[spec.downstream]
        edge = db.scalar(
            select(Dependency).where(
                Dependency.pipeline_id == pipeline.id,
                Dependency.upstream_job_id == upstream.id,
                Dependency.downstream_job_id == downstream.id,
            )
        )
        if edge is None:
            db.add(
                Dependency(
                    pipeline_id=pipeline.id,
                    upstream_job_id=upstream.id,
                    downstream_job_id=downstream.id,
                    dep_type=spec.dep_type or DependencyType.DATA.value,
                    lag_minutes=spec.lag_minutes,
                )
            )
            created += 1
    db.flush()
    return created


def ensure_sla(db, pipelines: dict[str, Pipeline]) -> int:
    created = 0
    for spec in SLA_POLICIES:
        pipeline = pipelines[spec.pipeline]
        policy = db.scalar(select(SlaPolicy).where(SlaPolicy.pipeline_id == pipeline.id))
        if policy is None:
            db.add(
                SlaPolicy(
                    pipeline_id=pipeline.id,
                    name=spec.name,
                    deadline_clock=spec.deadline_clock,
                    buffer_minutes=spec.buffer_minutes,
                    owner_team=spec.owner_team,
                    consumer=spec.consumer,
                    enabled=True,
                    note=spec.note,
                )
            )
            created += 1
    db.flush()
    return created


def _history_total_minutes(pipeline_code: str, day_index: int, p50: int, p90: int) -> int:
    jitter_list = HISTORY_JITTER.get(pipeline_code, [0])
    jitter = jitter_list[day_index % len(jitter_list)]
    base = p50 + round((p90 - p50) * 0.4)
    return max(p50, base + jitter)


def ensure_history_runs(db, pipelines: dict[str, Pipeline], jobs_by_code: dict[str, Job]) -> int:
    anchor = date.fromisoformat(HISTORY_ANCHOR)
    created = 0
    for spec in PIPELINES:
        pipeline = pipelines[spec.code]
        job_rows = [
            {
                "id": jobs_by_code[item.code].id,
                "code": item.code,
                "name": item.name,
                "pipeline_code": spec.code,
                "layer": item.layer,
                "owner": item.owner,
                "status": JobStatus.ACTIVE.value,
                "p50_seconds": item.p50_minutes * 60,
                "p90_seconds": item.p90_minutes * 60,
            }
            for item in JOB_SPECS
            if item.pipeline == spec.code
        ]
        code_to_id = {row["code"]: row["id"] for row in job_rows}
        edge_rows = [
            {
                "id": index + 1,
                "upstream_id": code_to_id[edge.upstream],
                "downstream_id": code_to_id[edge.downstream],
                "upstream_code": edge.upstream,
                "downstream_code": edge.downstream,
                "dep_type": edge.dep_type,
                "lag_minutes": edge.lag_minutes,
            }
            for index, edge in enumerate(e for e in DEPENDENCIES if e.pipeline == spec.code)
        ]
        p50_schedule = dag_service.compute_schedule(job_rows, edge_rows, RunMode.P50.value)
        p90_schedule = dag_service.compute_schedule(job_rows, edge_rows, RunMode.P90.value)
        start_minute = clock_to_minutes(spec.start_clock)

        for day_index in range(HISTORY_DAYS):
            run_date = anchor - timedelta(days=day_index)
            seed_key = f"seed:history:{spec.code}:{run_date.isoformat()}"
            if db.scalar(select(PipelineRun).where(PipelineRun.seed_key == seed_key)):
                continue
            total = _history_total_minutes(spec.code, day_index, p50_schedule["total_minutes"], p90_schedule["total_minutes"])
            if day_index in HISTORY_FAILED_DAYS.get(spec.code, []):
                status = RunStatus.FAILED.value
                total = total // 2  # 失败批次只跑到一半就中断
            elif day_index in HISTORY_LATE_DAYS.get(spec.code, []):
                status = RunStatus.LATE.value
                total = max(total, p90_schedule["total_minutes"] + 4)
            else:
                status = RunStatus.SUCCESS.value
            timings = [
                {
                    "code": row["code"],
                    "name": row["name"],
                    "layer": row["layer"],
                    "owner": row["owner"],
                    "duration_minutes": row["duration_minutes"],
                    "es_minutes": row["es_minutes"],
                    "ef_minutes": row["ef_minutes"],
                    "ls_minutes": row["ls_minutes"],
                    "lf_minutes": row["lf_minutes"],
                    "total_float_minutes": row["total_float_minutes"],
                    "on_critical": row["on_critical"],
                    "start_clock": minutes_to_clock(start_minute + row["es_minutes"]),
                    "end_clock": minutes_to_clock(start_minute + row["ef_minutes"]),
                }
                for row in p50_schedule["jobs"]
            ]
            db.add(
                PipelineRun(
                    pipeline_id=pipeline.id,
                    mode=RunMode.P90.value,
                    run_kind="history",
                    status=status,
                    run_date=run_date,
                    total_minutes=total,
                    p50_minutes=p50_schedule["total_minutes"],
                    p90_minutes=p90_schedule["total_minutes"],
                    peak_concurrency=p50_schedule["peak_concurrency"],
                    critical_path=p50_schedule["critical_path"],
                    job_timings=timings,
                    sla_verdict={},
                    triggered_by="crontab",
                    seed_key=seed_key,
                    created_at=_run_created_at(run_date, spec.start_clock, total),
                )
            )
            created += 1
    db.flush()
    return created


def _run_created_at(run_date: date, start_clock: str, total_minutes: int) -> datetime:
    hour, minute = (int(part) for part in start_clock.split(":"))
    return datetime.combine(run_date, time(hour, minute)) + timedelta(minutes=int(total_minutes))


# ---------------------------------------------------------------- 原始调度记录（依赖体检用）
#
# 剧本 kind 说明（参数见 demo_config.RECORD_PLAYS）：
#   early_start        提前到「起跑 + start_minute」开跑，时长不变（制造抢跑）
#   retry              attempt1 跑 first_minutes 失败，attempt2 完整成功
#   reversion          同一 attempt 先报 v1（结束时间偏移），再报 v2（正确时间）
#   late_ingest        记录正常，但入库时间晚 hours 小时（迟到记录）
#   inverted           结束时间早于开始时间（时间倒挂，证据冲突）
#   conflicting_final  同 attempt 同 version 上报两条时间不同的记录（证据冲突）
#   missing_after      只有最近 max_day_index 天有记录（新作业，样本不足）
#   follow_job         稳定跟在 follow 作业结束后 gap_base + day%gap_mod 分钟起跑；
#                      每 except_mod 天（except_hits）例外，提前到 except_start_minute


def _day_record_tuples(
    spec,
    job_code: str,
    day_index: int,
    base_start: int,
    base_end: int,
    jitter: int,
    follow_end: int | None,
) -> list[tuple[int, int, str, int, int, int]]:
    """返回 [(attempt, version, status, start_min, end_min, ingested_min)]，分钟相对业务日期 00:00。"""
    duration = base_end - base_start
    start_minute = clock_to_minutes(spec.start_clock)
    normal = [(1, 1, "success", base_start, base_end, base_end + 5)]
    for play in RECORD_PLAYS:
        if play.pipeline != spec.code or play.job != job_code:
            continue
        if day_index % play.mod not in play.hits:
            continue
        params = play.params or {}
        if play.kind == "missing_after":
            if day_index > int(params["max_day_index"]):
                return []
        elif play.kind == "early_start":
            start = start_minute + int(params["start_minute"]) + jitter
            return [(1, 1, "success", start, start + duration, start + duration + 5)]
        elif play.kind == "retry":
            first_end = base_start + int(params["first_minutes"])
            retry_start = base_start + int(params["retry_gap_minutes"])
            retry_end = retry_start + duration
            return [
                (1, 1, "failed", base_start, first_end, first_end + 5),
                (2, 1, "success", retry_start, retry_end, retry_end + 5),
            ]
        elif play.kind == "reversion":
            v1_end = base_end + int(params["v1_end_shift_minutes"])
            return [
                (1, 1, "success", base_start, v1_end, v1_end + 5),
                (1, 2, "success", base_start, base_end, base_end + int(params["v2_ingest_lag_minutes"])),
            ]
        elif play.kind == "late_ingest":
            return [(1, 1, "success", base_start, base_end, base_end + int(params["hours"]) * 60)]
        elif play.kind == "inverted":
            return [(1, 1, "success", base_start, base_start - int(params["minutes"]), base_start + 5)]
        elif play.kind == "conflicting_final":
            shift = int(params["shift_minutes"])
            return [
                (1, 1, "success", base_start, base_end, base_end + 5),
                (1, 1, "success", base_start + shift, base_end + shift, base_end + shift + 5),
            ]
        elif play.kind == "follow_job":
            if day_index % int(params["except_mod"]) in tuple(params["except_hits"]):
                start = start_minute + int(params["except_start_minute"]) + jitter
            else:
                anchor_end = follow_end if follow_end is not None else base_end
                start = anchor_end + int(params["gap_base"]) + (day_index % int(params["gap_mod"]))
            return [(1, 1, "success", start, start + duration, start + duration + 5)]
    return normal


def ensure_job_run_records(db, pipelines: dict[str, Pipeline], jobs_by_code: dict[str, Job]) -> int:
    """按剧本生成 30 天 × 24 作业的原始调度记录（重试/迟到/旧版本混在一起）。"""
    anchor = date.fromisoformat(HISTORY_ANCHOR)
    created = 0
    for spec in PIPELINES:
        pipeline = pipelines[spec.code]
        job_rows = [
            {
                "id": jobs_by_code[item.code].id,
                "code": item.code,
                "name": item.name,
                "pipeline_code": spec.code,
                "layer": item.layer,
                "owner": item.owner,
                "status": JobStatus.ACTIVE.value,
                "p50_seconds": item.p50_minutes * 60,
                "p90_seconds": item.p90_minutes * 60,
            }
            for item in JOB_SPECS
            if item.pipeline == spec.code
        ]
        code_to_id = {row["code"]: row["id"] for row in job_rows}
        edge_rows = [
            {
                "id": index + 1,
                "upstream_id": code_to_id[edge.upstream],
                "downstream_id": code_to_id[edge.downstream],
                "upstream_code": edge.upstream,
                "downstream_code": edge.downstream,
                "dep_type": edge.dep_type,
                "lag_minutes": edge.lag_minutes,
            }
            for index, edge in enumerate(e for e in DEPENDENCIES if e.pipeline == spec.code)
        ]
        p50_schedule = dag_service.compute_schedule(job_rows, edge_rows, RunMode.P50.value)
        es_ef = {row["code"]: (row["es_minutes"], row["ef_minutes"]) for row in p50_schedule["jobs"]}
        start_minute = clock_to_minutes(spec.start_clock)

        for day_index in range(HISTORY_DAYS):
            run_date = anchor - timedelta(days=day_index)
            jitter = ((day_index * 7) % 5) - 2  # 整链路同向平移，不改变作业间相对关系
            base = {
                code: (start_minute + es + jitter, start_minute + ef + jitter)
                for code, (es, ef) in es_ef.items()
            }
            for item in [row for row in JOB_SPECS if row.pipeline == spec.code]:
                base_start, base_end = base[item.code]
                follow_end = None
                for play in RECORD_PLAYS:
                    if play.pipeline == spec.code and play.job == item.code and play.kind == "follow_job":
                        follow_code = (play.params or {}).get("follow")
                        if follow_code in base:
                            follow_end = base[follow_code][1]
                tuples = _day_record_tuples(
                    spec, item.code, day_index, base_start, base_end, jitter, follow_end
                )
                day_start = datetime.combine(run_date, time(0, 0))
                seen_keys: dict[tuple[int, int], int] = {}
                for attempt, version, status, s_min, e_min, i_min in tuples:
                    # 同 (attempt, version) 的重复上报（conflicting_final 剧本）加序号后缀
                    dup_index = seen_keys.get((attempt, version), 0)
                    seen_keys[(attempt, version)] = dup_index + 1
                    suffix = f":r{dup_index + 1}" if dup_index else ""
                    seed_key = f"seed:runrec:{item.code}:{run_date.isoformat()}:a{attempt}:v{version}{suffix}"
                    if db.scalar(select(JobRunRecord).where(JobRunRecord.seed_key == seed_key)):
                        continue
                    db.add(
                        JobRunRecord(
                            pipeline_id=pipeline.id,
                            job_id=jobs_by_code[item.code].id,
                            biz_date=run_date,
                            attempt=attempt,
                            record_version=version,
                            status=status,
                            start_at=day_start + timedelta(minutes=s_min),
                            end_at=day_start + timedelta(minutes=e_min),
                            ingested_at=day_start + timedelta(minutes=i_min),
                            source="crontab",
                            seed_key=seed_key,
                        )
                    )
                    created += 1
    db.flush()
    return created


def ensure_seed_audit(db) -> None:
    for seed_key, email, role, action, entity, entity_id, detail in SEED_AUDIT:
        if db.scalar(select(AuditLog).where(AuditLog.seed_key == seed_key)):
            continue
        db.add(
            AuditLog(
                actor_email=email,
                actor_role=role,
                action=action,
                entity=entity,
                entity_id=entity_id,
                detail=detail,
                layer="seed",
                ip="127.0.0.1",
                seed_key=seed_key,
            )
        )
    db.flush()


def seed_all() -> str:
    with session_scope() as db:
        ensure_users(db)
        pipelines = ensure_pipelines(db)
        jobs = ensure_jobs(db, pipelines)
        ensure_dependencies(db, pipelines, jobs)
        ensure_sla(db, pipelines)
        ensure_history_runs(db, pipelines, jobs)
        ensure_job_run_records(db, pipelines, jobs)
        ensure_seed_audit(db)

        job_count = db.scalar(select(func.count()).select_from(Job)) or 0
        dep_count = db.scalar(select(func.count()).select_from(Dependency)) or 0
        pipeline_count = db.scalar(select(func.count()).select_from(Pipeline)) or 0
        run_count = (
            db.scalar(
                select(func.count()).select_from(PipelineRun).where(PipelineRun.run_kind == "history")
            )
            or 0
        )
        record_count = db.scalar(select(func.count()).select_from(JobRunRecord)) or 0
        sla_count = db.scalar(select(func.count()).select_from(SlaPolicy)) or 0
        user_count = db.scalar(select(func.count()).select_from(User)) or 0

    summary = (
        f"SEED_SUMMARY brand=dagpilot jobs={job_count} deps={dep_count} pipelines={pipeline_count} "
        f"runs={run_count} records={record_count} slas={sla_count} users={user_count}"
    )
    print(summary, flush=True)
    return summary


if __name__ == "__main__":
    seed_all()
