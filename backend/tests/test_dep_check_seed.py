"""seed 剧本 → 体检结论的端到端验证（SQLite）。

锁定演示数据的体检结论分布：
  长期违背  stage→store_daily（L3，七成天数抢跑）
  证据不足  label_join→publish_hive（L4，遵守率 70%）
  待核      store_profile 相关边（样本不足）+ channel_map 相关边（时间冲突）
  补边候选  dws_trade_minute→dqc_trade_rules（L1，含反例）
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.constants.enums import CheckItemKind, CheckVerdict
from src.models import Base
from src.models.dep_check import DepCheckItem
from src.models.job import Job
from src.models.job_run_record import JobRunRecord
from src.models.pipeline import Pipeline
from src.services import dep_check_service
from scripts.seed_demo import (
    ensure_dependencies,
    ensure_job_run_records,
    ensure_jobs,
    ensure_pipelines,
)


@pytest.fixture()
def seeded_db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with factory() as session:
        pipelines = ensure_pipelines(session)
        jobs = ensure_jobs(session, pipelines)
        ensure_dependencies(session, pipelines, jobs)
        ensure_job_run_records(session, pipelines, jobs)
        session.commit()
        yield session


def _items_by_edge(db: Session) -> dict[tuple[str, str], DepCheckItem]:
    items = db.scalars(select(DepCheckItem)).all()
    return {(item.upstream_code, item.downstream_code): item for item in items}


def test_seed_records_are_deterministic_and_messy(seeded_db):
    records = seeded_db.scalars(select(JobRunRecord)).all()
    # 720 基线 − 27 天新作业缺失 + 6 重试 + 6 旧版本 + 1 同版冲突 = 706
    assert len(records) == 706
    attempts = {(record.job_id, record.biz_date, record.attempt, record.record_version) for record in records}
    assert len(attempts) < len(records)  # 同版冲突的两条记录共享 (job, date, attempt, version)
    # 重跑 seed 不新增、不覆盖（幂等）
    pipelines = {row.code: row for row in seeded_db.scalars(select(Pipeline)).all()}
    jobs = {row.code: row for row in seeded_db.scalars(select(Job)).all()}
    again = ensure_job_run_records(seeded_db, pipelines, jobs)
    assert again == 0
    assert seeded_db.scalar(select(func.count()).select_from(JobRunRecord)) == 706


def test_check_verdict_distribution(seeded_db):
    result = dep_check_service.run_check(seeded_db, "tester@dagpilot.io")
    seeded_db.commit()
    batch = result["batch"]
    stats = batch.stats

    edges = {
        (item.upstream_code, item.downstream_code): item
        for item in seeded_db.scalars(
            select(DepCheckItem).where(DepCheckItem.item_kind == CheckItemKind.EDGE.value)
        ).all()
    }
    # 长期违背：报表线 stage→store_daily
    assert edges[("ads_report_stage", "ads_store_daily")].verdict == CheckVerdict.VIOLATED.value
    # 证据不足：特征线 label_join→publish_hive
    assert edges[("feature_label_join", "feature_publish_hive")].verdict == CheckVerdict.INSUFFICIENT.value
    # 待核（样本不足）：新作业 store_profile 的两条出边
    assert edges[("feature_store_profile", "feature_label_join")].verdict == CheckVerdict.PENDING.value
    assert edges[("feature_store_profile", "feature_publish_hive")].verdict == CheckVerdict.PENDING.value
    assert edges[("feature_store_profile", "feature_label_join")].sample_days < 5
    # 待核（时间冲突）：channel_map 相关的三条边
    assert edges[("dim_date_build", "dim_channel_map")].verdict == CheckVerdict.PENDING.value
    assert edges[("dim_store_full", "dim_channel_map")].verdict == CheckVerdict.PENDING.value
    assert edges[("dim_channel_map", "dim_wide_publish")].verdict == CheckVerdict.PENDING.value
    # 有效：关键路径上的 data 边
    assert edges[("ads_report_stage", "ads_trade_daily")].verdict == CheckVerdict.EFFECTIVE.value
    assert edges[("ads_report_join", "ads_report_qc")].verdict == CheckVerdict.EFFECTIVE.value

    assert stats.get("edge:violated") == 1
    assert stats.get("edge:insufficient") == 1
    assert stats.get("edge:pending") == 5
    assert stats.get("edge:effective") == 31


def test_check_candidate_with_counterexamples(seeded_db):
    dep_check_service.run_check(seeded_db, "tester@dagpilot.io")
    seeded_db.commit()
    candidates = seeded_db.scalars(
        select(DepCheckItem).where(DepCheckItem.item_kind == CheckItemKind.CANDIDATE.value)
    ).all()
    suggested = [item for item in candidates if item.verdict == CheckVerdict.SUGGESTED.value]
    assert len(suggested) == 1
    candidate = suggested[0]
    assert candidate.upstream_code == "dws_trade_minute"
    assert candidate.downstream_code == "dqc_trade_rules"
    assert candidate.sample_days == 30
    assert candidate.support_days >= 24  # 30 天里绝大多数前后相接
    assert len(candidate.counterexamples) >= 4  # 每 6 天 1 次抢跑反例
    assert all(item["reason"] == "order_reversed" for item in candidate.counterexamples)
    # 样本不足的候选对保留待核，不下结论
    pending = [item for item in candidates if item.verdict == CheckVerdict.PENDING.value]
    assert pending, "新作业相关的作业对应保留为待核"
    assert all(item.sample_days < 5 or "冲突" in item.reason for item in pending)


def test_check_batch_idempotent_on_seeded_data(seeded_db):
    first = dep_check_service.run_check(seeded_db, "tester@dagpilot.io")
    seeded_db.commit()
    second = dep_check_service.run_check(seeded_db, "tester@dagpilot.io")
    assert second["reused"] is True
    assert second["batch"].id == first["batch"].id

    dep_check_service.publish_batch(seeded_db, first["batch"].id, "admin@dagpilot.io")
    seeded_db.commit()
    # 已发布后记录未变化：再次发起检查直接指向当前发布，不新增批次
    third = dep_check_service.run_check(seeded_db, "tester@dagpilot.io")
    assert third["reused"] is True
    assert third["batch"].id == first["batch"].id
