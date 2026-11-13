"""依赖体检测试：整理算法 / 边结论 / 补边候选 / 批次生命周期 / API。

全部跑在 SQLite 内存库上（models 只用通用列类型），不需要 MySQL。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.constants.enums import CheckBatchStatus, CheckVerdict, RejectReason
from src.constants.limits import CHECK_MIN_SAMPLE_DAYS
from src.models import Base
from src.models.dependency import Dependency
from src.models.job import Job
from src.models.job_run_record import JobRunRecord
from src.models.pipeline import Pipeline
from src.services import dep_check_service, run_fact_service
from src.utils.errors import AppError


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with factory() as session:
        yield session


def _pipeline(db: Session, code: str = "p1", start_clock: str = "01:00") -> Pipeline:
    pipeline = Pipeline(
        code=code, name=f"链路{code}", line_no="L9", line_color="oklch(0.5 0.1 200)",
        start_clock=start_clock, crew="测试组", note="", sort_order=0,
    )
    db.add(pipeline)
    db.flush()
    return pipeline


def _job(db: Session, pipeline: Pipeline, code: str) -> Job:
    job = Job(
        code=code, name=f"作业{code}", pipeline_id=pipeline.id, layer="DWD",
        owner="测试", target_table="", p50_seconds=600, p90_seconds=1200,
        status="active", note="",
    )
    db.add(job)
    db.flush()
    return job


def _edge(db: Session, pipeline: Pipeline, up: Job, down: Job, lag: int = 0) -> Dependency:
    edge = Dependency(
        pipeline_id=pipeline.id, upstream_job_id=up.id, downstream_job_id=down.id,
        dep_type="data", lag_minutes=lag,
    )
    db.add(edge)
    db.flush()
    return edge


def _record(
    db: Session,
    pipeline: Pipeline,
    job: Job,
    day: date,
    start: str,
    end: str,
    *,
    attempt: int = 1,
    version: int = 1,
    status: str = "success",
    ingest_lag_minutes: int = 5,
) -> JobRunRecord:
    base = datetime.combine(day, datetime.min.time())
    start_at = base + timedelta(hours=int(start[:2]), minutes=int(start[3:5]))
    end_at = base + timedelta(hours=int(end[:2]), minutes=int(end[3:5]))
    record = JobRunRecord(
        pipeline_id=pipeline.id, job_id=job.id, biz_date=day, attempt=attempt,
        record_version=version, status=status, start_at=start_at, end_at=end_at,
        ingested_at=end_at + timedelta(minutes=ingest_lag_minutes), source="crontab",
    )
    db.add(record)
    db.flush()
    return record


DAYS = [date(2026, 11, 11) - timedelta(days=offset) for offset in range(10)]


# ---------------------------------------------------------------- 整理算法


def test_normalize_retry_supersedes_earlier_attempt(db):
    pipeline = _pipeline(db)
    job = _job(db, pipeline, "a")
    day = DAYS[0]
    _record(db, pipeline, job, day, "01:00", "01:10", status="failed")
    _record(db, pipeline, job, day, "01:20", "01:40", attempt=2)
    jobs, records = run_fact_service.load_pipeline_records(db, pipeline)
    result = run_fact_service.normalize_records(records, jobs)

    fact = result["facts"][(job.id, day)]
    assert fact["attempt"] == 2
    assert fact["record"].status == "success"
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["reject_reason"] == RejectReason.SUPERSEDED_BY_RETRY.value
    assert not result["conflicts"]


def test_normalize_outdated_version_rejected(db):
    pipeline = _pipeline(db)
    job = _job(db, pipeline, "a")
    day = DAYS[0]
    _record(db, pipeline, job, day, "01:00", "01:30", version=1)
    _record(db, pipeline, job, day, "01:00", "01:40", version=2)
    jobs, records = run_fact_service.load_pipeline_records(db, pipeline)
    result = run_fact_service.normalize_records(records, jobs)

    fact = result["facts"][(job.id, day)]
    assert fact["record"].record_version == 2
    assert fact["record"].end_at.strftime("%H:%M") == "01:40"
    assert result["rejected"][0]["reject_reason"] == RejectReason.OUTDATED_VERSION.value


def test_normalize_time_inversion_is_conflict(db):
    pipeline = _pipeline(db)
    job = _job(db, pipeline, "a")
    day = DAYS[0]
    _record(db, pipeline, job, day, "01:30", "01:00")  # 结束早于开始
    jobs, records = run_fact_service.load_pipeline_records(db, pipeline)
    result = run_fact_service.normalize_records(records, jobs)

    assert (job.id, day) not in result["facts"]
    assert result["conflicts"][0]["reason"] == RejectReason.TIME_INVERSION.value


def test_normalize_conflicting_final_versions(db):
    pipeline = _pipeline(db)
    job = _job(db, pipeline, "a")
    day = DAYS[0]
    _record(db, pipeline, job, day, "01:00", "01:30", version=1)
    _record(db, pipeline, job, day, "02:00", "02:30", version=1)  # 同版本不同时间
    jobs, records = run_fact_service.load_pipeline_records(db, pipeline)
    result = run_fact_service.normalize_records(records, jobs)

    assert (job.id, day) not in result["facts"]
    assert result["conflicts"][0]["reason"] == RejectReason.CONFLICTING_FINAL.value


def test_normalize_late_ingest_marked_but_kept(db):
    pipeline = _pipeline(db)
    job = _job(db, pipeline, "a")
    day = DAYS[0]
    _record(db, pipeline, job, day, "01:00", "01:30", ingest_lag_minutes=8 * 60)
    jobs, records = run_fact_service.load_pipeline_records(db, pipeline)
    result = run_fact_service.normalize_records(records, jobs)

    fact = result["facts"][(job.id, day)]
    assert fact["is_late_record"] is True
    assert fact["ingest_lag_minutes"] == 8 * 60


# ---------------------------------------------------------------- 边结论


def _facts_for(db, pipeline, up, down, days, up_end="01:30", down_start="01:40"):
    for day in days:
        _record(db, pipeline, up, day, "01:00", up_end)
        _record(db, pipeline, down, day, down_start, "02:00")
    jobs, records = run_fact_service.load_pipeline_records(db, pipeline)
    normalized = run_fact_service.normalize_records(records, jobs)
    conflicts = {
        (entry["job_id"], date.fromisoformat(entry["biz_date"])): entry["reason"]
        for entry in normalized["conflicts"]
    }
    return normalized["facts"], conflicts


def _edge_items(db, pipeline, up, down, lag=0):
    jobs, records = run_fact_service.load_pipeline_records(db, pipeline)
    normalized = run_fact_service.normalize_records(records, jobs)
    conflicts = {
        (entry["job_id"], date.fromisoformat(entry["biz_date"])): entry["reason"]
        for entry in normalized["conflicts"]
    }
    job_dicts = [{"id": up.id, "code": up.code, "status": "active"},
                 {"id": down.id, "code": down.code, "status": "active"}]
    edges = [{"upstream_id": up.id, "downstream_id": down.id, "upstream_code": up.code,
              "downstream_code": down.code, "dep_type": "data", "lag_minutes": lag}]
    window = dep_check_service._window_dates(normalized["facts"], conflicts, date(2026, 11, 11))
    return dep_check_service.evaluate_edges(
        pipeline.code, job_dicts, edges, normalized["facts"], conflicts, window
    )


def test_edge_effective_when_obey_ratio_high(db):
    pipeline = _pipeline(db)
    up, down = _job(db, pipeline, "up"), _job(db, pipeline, "down")
    _edge(db, pipeline, up, down)
    _facts_for(db, pipeline, up, down, DAYS)  # 10 天全部 01:30 → 01:40 遵守
    item = _edge_items(db, pipeline, up, down)[0]
    assert item["verdict"] == CheckVerdict.EFFECTIVE.value
    assert item["sample_days"] == len(DAYS)
    assert item["support_days"] == len(DAYS)


def test_edge_violated_when_downstream_starts_early_most_days(db):
    pipeline = _pipeline(db)
    up, down = _job(db, pipeline, "up"), _job(db, pipeline, "down")
    _edge(db, pipeline, up, down)
    # 10 天里 7 天下游 01:20 抢跑（上游 01:30 才结束），3 天遵守
    for index, day in enumerate(DAYS):
        _record(db, pipeline, up, day, "01:00", "01:30")
        _record(db, pipeline, down, day, "01:20" if index < 7 else "01:40", "02:00")
    item = _edge_items(db, pipeline, up, down)[0]
    assert item["verdict"] == CheckVerdict.VIOLATED.value
    assert item["support_days"] == 3


def test_edge_insufficient_when_evidence_mixed(db):
    pipeline = _pipeline(db)
    up, down = _job(db, pipeline, "up"), _job(db, pipeline, "down")
    _edge(db, pipeline, up, down)
    # 7 遵守 / 3 违背：遵守率 0.7，落在 (0.5, 0.9) 区间
    for index, day in enumerate(DAYS):
        _record(db, pipeline, up, day, "01:00", "01:30")
        _record(db, pipeline, down, day, "01:20" if index < 3 else "01:40", "02:00")
    item = _edge_items(db, pipeline, up, down)[0]
    assert item["verdict"] == CheckVerdict.INSUFFICIENT.value


def test_edge_pending_when_samples_below_minimum(db):
    pipeline = _pipeline(db)
    up, down = _job(db, pipeline, "up"), _job(db, pipeline, "down")
    _edge(db, pipeline, up, down)
    _facts_for(db, pipeline, up, down, DAYS[: CHECK_MIN_SAMPLE_DAYS - 1])
    item = _edge_items(db, pipeline, up, down)[0]
    assert item["verdict"] == CheckVerdict.PENDING.value
    assert "样本不足" in item["reason"]


def test_edge_pending_when_time_evidence_conflicts(db):
    pipeline = _pipeline(db)
    up, down = _job(db, pipeline, "up"), _job(db, pipeline, "down")
    _edge(db, pipeline, up, down)
    # 10 天里 3 天上游记录倒挂（30% > 20% 阈值）→ 整条边待核
    for index, day in enumerate(DAYS):
        if index < 3:
            _record(db, pipeline, up, day, "01:30", "01:00")
        else:
            _record(db, pipeline, up, day, "01:00", "01:30")
        _record(db, pipeline, down, day, "01:40", "02:00")
    item = _edge_items(db, pipeline, up, down)[0]
    assert item["verdict"] == CheckVerdict.PENDING.value
    assert "冲突" in item["reason"]


def test_edge_lag_minutes_respected(db):
    pipeline = _pipeline(db)
    up, down = _job(db, pipeline, "up"), _job(db, pipeline, "down")
    _edge(db, pipeline, up, down, lag=15)
    # 下游只等 10 分钟，不满足 lag 15 → 全部违背
    _facts_for(db, pipeline, up, down, DAYS, up_end="01:30", down_start="01:40")
    item = _edge_items(db, pipeline, up, down, lag=15)[0]
    assert item["verdict"] == CheckVerdict.VIOLATED.value


# ---------------------------------------------------------------- 补边候选


def _candidate_items(db, pipeline, jobs, edges):
    _, records = run_fact_service.load_pipeline_records(db, pipeline)
    jobs_by_id = {job.id: job for job in jobs}
    normalized = run_fact_service.normalize_records(records, jobs_by_id)
    conflicts = {
        (entry["job_id"], date.fromisoformat(entry["biz_date"])): entry["reason"]
        for entry in normalized["conflicts"]
    }
    job_dicts = [{"id": job.id, "code": job.code, "status": "active"} for job in jobs]
    edge_dicts = [
        {"upstream_id": edge.upstream_job_id, "downstream_id": edge.downstream_job_id,
         "upstream_code": next(j.code for j in jobs if j.id == edge.upstream_job_id),
         "downstream_code": next(j.code for j in jobs if j.id == edge.downstream_job_id),
         "dep_type": edge.dep_type, "lag_minutes": edge.lag_minutes}
        for edge in edges
    ]
    window = dep_check_service._window_dates(normalized["facts"], conflicts, date(2026, 11, 11))
    return dep_check_service.evaluate_candidates(
        pipeline.code, job_dicts, edge_dicts, normalized["facts"], conflicts, window
    )


def test_candidate_suggested_with_counterexamples(db):
    pipeline = _pipeline(db)
    up, down = _job(db, pipeline, "up"), _job(db, pipeline, "down")
    # 无声明边；10 天里 9 天 down 紧跟 up 结束 4 分钟起跑，1 天颠倒
    for index, day in enumerate(DAYS):
        _record(db, pipeline, up, day, "01:00", "01:30")
        _record(db, pipeline, down, day, "01:20" if index == 0 else "01:34", "02:00")
    items, stats = _candidate_items(db, pipeline, [up, down], [])
    assert len(items) == 1
    item = items[0]
    assert item["verdict"] == CheckVerdict.SUGGESTED.value
    assert item["upstream_code"] == "up" and item["downstream_code"] == "down"
    assert item["support_days"] == 9
    assert len(item["counterexamples"]) == 1
    assert item["counterexamples"][0]["reason"] == "order_reversed"


def test_candidate_skipped_when_declared_path_exists(db):
    pipeline = _pipeline(db)
    up, down = _job(db, pipeline, "up"), _job(db, pipeline, "down")
    edge = _edge(db, pipeline, up, down)  # 已有声明路径
    for day in DAYS:
        _record(db, pipeline, up, day, "01:00", "01:30")
        _record(db, pipeline, down, day, "01:34", "02:00")
    items, stats = _candidate_items(db, pipeline, [up, down], [edge])
    assert items == []
    assert stats["pairs_skipped_declared"] == 1


def test_candidate_pending_when_samples_below_minimum(db):
    pipeline = _pipeline(db)
    up, down = _job(db, pipeline, "up"), _job(db, pipeline, "down")
    for day in DAYS[:3]:  # 只有 3 天记录
        _record(db, pipeline, up, day, "01:00", "01:30")
        _record(db, pipeline, down, day, "01:34", "02:00")
    items, _ = _candidate_items(db, pipeline, [up, down], [])
    assert len(items) == 1
    assert items[0]["verdict"] == CheckVerdict.PENDING.value


def test_candidate_not_listed_when_adjacency_unstable(db):
    pipeline = _pipeline(db)
    up, down = _job(db, pipeline, "up"), _job(db, pipeline, "down")
    for index, day in enumerate(DAYS):  # 一半天数间隔 40 分钟，不稳定
        _record(db, pipeline, up, day, "01:00", "01:30")
        _record(db, pipeline, down, day, "01:34" if index % 2 == 0 else "02:10", "03:00")
    items, _ = _candidate_items(db, pipeline, [up, down], [])
    assert items == []


# ---------------------------------------------------------------- 批次生命周期


def _seed_graph(db):
    """两作业一条边的最小图 + 10 天记录。"""
    pipeline = _pipeline(db)
    up, down = _job(db, pipeline, "up"), _job(db, pipeline, "down")
    _edge(db, pipeline, up, down)
    for day in DAYS:
        _record(db, pipeline, up, day, "01:00", "01:30")
        _record(db, pipeline, down, day, "01:40", "02:00")
    return pipeline


def test_run_check_is_idempotent_for_same_fingerprint(db):
    _seed_graph(db)
    first = dep_check_service.run_check(db, "tester@dagpilot.io")
    db.commit()
    assert first["reused"] is False
    second = dep_check_service.run_check(db, "tester@dagpilot.io")
    assert second["reused"] is True
    assert second["batch"].id == first["batch"].id  # 同一批重复执行不新增版本


def test_run_check_new_batch_after_records_change(db):
    pipeline = _seed_graph(db)
    first = dep_check_service.run_check(db, "tester@dagpilot.io")
    db.commit()
    # 上游记录变化：新增一个业务日期的记录
    new_day = date(2026, 11, 12)
    up = db.query(Job).filter_by(code="up").one()
    down = db.query(Job).filter_by(code="down").one()
    _record(db, pipeline, up, new_day, "01:00", "01:30")
    _record(db, pipeline, down, new_day, "01:40", "02:00")
    second = dep_check_service.run_check(db, "tester@dagpilot.io")
    assert second["reused"] is False
    assert second["batch"].id != first["batch"].id
    assert first["batch"].status == CheckBatchStatus.STALE.value  # 旧草稿失效


def test_publish_swaps_current_and_keeps_old_on_failure(db):
    _seed_graph(db)
    first = dep_check_service.run_check(db, "tester@dagpilot.io")["batch"]
    db.commit()
    dep_check_service.publish_batch(db, first.id, "admin@dagpilot.io")
    db.commit()
    assert first.status == CheckBatchStatus.PUBLISHED.value
    assert first.version == 1

    # 重复发布同一批 → 409，旧版保留
    with pytest.raises(AppError) as excinfo:
        dep_check_service.publish_batch(db, first.id, "admin@dagpilot.io")
    assert excinfo.value.code == "BATCH_NOT_DRAFT"
    current = dep_check_service.current_published(db)
    assert current["batch"]["id"] == first.id
    assert current["batch"]["version"] == 1


def test_publish_rejected_when_fingerprint_stale(db):
    pipeline = _seed_graph(db)
    batch = dep_check_service.run_check(db, "tester@dagpilot.io")["batch"]
    db.commit()
    # 草稿生成后上游记录变化 → 旧结论失效，禁止发布
    up = db.query(Job).filter_by(code="up").one()
    _record(db, pipeline, up, date(2026, 11, 12), "01:00", "01:30")
    with pytest.raises(AppError) as excinfo:
        dep_check_service.publish_batch(db, batch.id, "admin@dagpilot.io")
    assert excinfo.value.code == "BATCH_STALE"
    assert dep_check_service.current_published(db)["has_current"] is False


def test_current_published_marked_stale_after_upstream_change(db):
    pipeline = _seed_graph(db)
    batch = dep_check_service.run_check(db, "tester@dagpilot.io")["batch"]
    db.commit()
    dep_check_service.publish_batch(db, batch.id, "admin@dagpilot.io")
    db.commit()
    assert dep_check_service.current_published(db)["batch"]["is_stale"] is False
    # 发布后上游记录变化 → 已发布结论失效（保留可查，但标记 stale）
    up = db.query(Job).filter_by(code="up").one()
    _record(db, pipeline, up, date(2026, 11, 12), "01:00", "01:30")
    current = dep_check_service.current_published(db)
    assert current["batch"]["is_stale"] is True


def test_publish_failure_rolls_back_and_keeps_old_version(db, monkeypatch):
    pipeline = _seed_graph(db)
    first = dep_check_service.run_check(db, "tester@dagpilot.io")["batch"]
    db.commit()
    dep_check_service.publish_batch(db, first.id, "admin@dagpilot.io")
    db.commit()
    assert first.version == 1

    # 上游记录变化 → 新草稿
    up = db.query(Job).filter_by(code="up").one()
    _record(db, pipeline, up, date(2026, 11, 12), "01:00", "01:30")
    second = dep_check_service.run_check(db, "tester@dagpilot.io")["batch"]
    db.commit()

    # 发布中途数据库异常 → 回滚，旧版保留
    def boom(*args, **kwargs):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(db, "flush", boom)
    with pytest.raises(AppError) as excinfo:
        dep_check_service.publish_batch(db, second.id, "admin@dagpilot.io")
    assert excinfo.value.code == "PUBLISH_FAILED"
    monkeypatch.undo()
    db.rollback()

    current = dep_check_service.current_published(db)
    assert current["batch"]["id"] == first.id
    assert current["batch"]["version"] == 1
    db.refresh(second)
    assert second.status == CheckBatchStatus.DRAFT.value  # 新草稿未被部分发布
