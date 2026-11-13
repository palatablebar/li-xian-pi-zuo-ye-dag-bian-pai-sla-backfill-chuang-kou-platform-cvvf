"""端到端：真 seed_all() + 完整体检流程（登录 → 发起 → 草稿 → 发布 → 读回 → 运行明细）。

把 src.config.database 的 engine/SessionLocal 替换为 SQLite 内存库，
应用代码路径（seed、service、route、中间件）与生产完全一致。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.config.database as database
from src.models import Base
from src.models.dep_check import DepCheckBatch, DepCheckItem
from src.utils.security import create_access_token

ADMIN = {"Authorization": f"Bearer {create_access_token('admin@dagpilot.io', 'admin')[0]}"}
OPERATOR = {"Authorization": f"Bearer {create_access_token('operator@dagpilot.io', 'operator')[0]}"}
VIEWER = {"Authorization": f"Bearer {create_access_token('viewer@dagpilot.io', 'viewer')[0]}"}


@pytest.fixture()
def e2e(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", factory)

    from scripts.seed_demo import seed_all

    first_summary = seed_all()
    second_summary = seed_all()  # 幂等：两次 seed 汇总逐字一致
    assert first_summary == second_summary
    assert "records=706" in first_summary

    from src.config.app import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    return client, factory


def test_full_check_flow_on_seeded_data(e2e):
    client, factory = e2e

    # 业务方能读回（空）结论，但不能发起
    assert client.get("/api/dep-checks/current", headers=VIEWER).json()["has_current"] is False
    assert client.post("/api/dep-checks", headers=VIEWER).status_code == 403

    # 值班员发起体检 → 草稿
    created = client.post("/api/dep-checks", headers=OPERATOR)
    assert created.status_code == 201
    batch = created.json()["batch"]
    assert batch["status"] == "draft"
    stats = batch["stats"]
    assert stats["edge:effective"] == 31
    assert stats["edge:violated"] == 1
    assert stats["edge:insufficient"] == 1
    assert stats["edge:pending"] == 5
    assert stats["candidate:suggested"] == 1
    assert stats["rejected_records"] == 12  # 6 重试 + 6 旧版本（同版冲突的 2 条计入冲突，不算拒绝）
    assert stats["conflict_days"] == 9  # 8 倒挂 + 1 同版冲突

    # 重复发起：复用同一草稿，不新增版本
    again = client.post("/api/dep-checks", headers=OPERATOR)
    assert again.json()["reused"] is True
    assert again.json()["batch"]["id"] == batch["id"]

    # 值班员不能发布，负责人整批发布
    assert client.post(f"/api/dep-checks/{batch['id']}/publish", headers=OPERATOR).status_code == 403
    published = client.post(f"/api/dep-checks/{batch['id']}/publish", headers=ADMIN)
    assert published.status_code == 200
    assert published.json()["batch"]["version"] == 1

    # 读回已发布结论（viewer 可读）
    current = client.get("/api/dep-checks/current", headers=VIEWER).json()
    assert current["has_current"] is True
    assert current["batch"]["is_stale"] is False
    verdicts = {(item["upstream_code"], item["downstream_code"]): item["verdict"] for item in current["items"]}
    assert verdicts[("ads_report_stage", "ads_store_daily")] == "violated"
    assert verdicts[("feature_label_join", "feature_publish_hive")] == "insufficient"
    assert verdicts[("dim_date_build", "dim_channel_map")] == "pending"
    assert verdicts[("dws_trade_minute", "dqc_trade_rules")] == "suggested"
    assert verdicts[("ads_report_join", "ads_report_qc")] == "effective"

    # 证据与反例可读
    candidate = next(item for item in current["items"] if item["item_kind"] == "candidate"
                     and item["verdict"] == "suggested")
    assert candidate["sample_days"] == 30
    assert candidate["support_days"] == 25
    assert len(candidate["counterexamples"]) == 5
    assert all(cex["reason"] == "order_reversed" for cex in candidate["counterexamples"])

    # 运行明细：重试取代 / 旧版本 / 迟到记录都能查到拒绝原因
    facts = client.get("/api/run-facts?pipeline_code=dim&biz_date=2026-11-10", headers=VIEWER).json()
    assert facts["summary"]["rejected"] >= 1
    reasons = {row["reject_reason"] for row in facts["rejected"]}
    assert reasons <= {"superseded_by_retry", "outdated_version", "time_inversion", "conflicting_final"}

    # 上游记录变化 → 旧结论失效（stale），需重新体检
    with factory() as db:
        from src.models.job import Job
        from src.models.job_run_record import JobRunRecord
        from datetime import date, datetime

        job = db.scalar(select(Job).where(Job.code == "ods_pos_order_sync"))
        db.add(JobRunRecord(
            pipeline_id=job.pipeline_id, job_id=job.id, biz_date=date(2026, 11, 12),
            attempt=1, record_version=1, status="success",
            start_at=datetime(2026, 11, 12, 1, 0), end_at=datetime(2026, 11, 12, 1, 25),
            ingested_at=datetime(2026, 11, 12, 1, 30), source="crontab",
        ))
        db.commit()
    stale = client.get("/api/dep-checks/current", headers=VIEWER).json()
    assert stale["batch"]["is_stale"] is True

    # 新记录下的体检生成新草稿；旧发布仍是在读结论
    recheck = client.post("/api/dep-checks", headers=OPERATOR)
    assert recheck.json()["reused"] is False
    new_batch = recheck.json()["batch"]
    assert new_batch["id"] != batch["id"]
    still_current = client.get("/api/dep-checks/current", headers=VIEWER).json()
    assert still_current["batch"]["id"] == batch["id"]

    # 发布新草稿：旧版被取代，版本号递增
    published2 = client.post(f"/api/dep-checks/{new_batch['id']}/publish", headers=ADMIN)
    assert published2.json()["batch"]["version"] == 2
    final = client.get("/api/dep-checks/current", headers=VIEWER).json()
    assert final["batch"]["id"] == new_batch["id"]
    assert final["batch"]["is_stale"] is False

    with factory() as db:
        batches = db.scalars(select(DepCheckBatch)).all()
        by_status = {}
        for row in batches:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        assert by_status["published"] == 1
        assert by_status["superseded"] == 1
        items_count = db.scalar(select(DepCheckItem.id).limit(1)) is not None
        assert items_count
