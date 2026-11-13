"""依赖体检 API 测试：鉴权 / 发起 / 发布 / 读回 / 运行明细（SQLite + TestClient）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.config.app import create_app
from src.config.database import get_db
from src.models import Base
from src.models.dependency import Dependency
from src.models.job import Job
from src.models.job_run_record import JobRunRecord
from src.models.pipeline import Pipeline
from src.utils.security import create_access_token

ADMIN = {"Authorization": f"Bearer {create_access_token('admin@dagpilot.io', 'admin')[0]}"}
OPERATOR = {"Authorization": f"Bearer {create_access_token('operator@dagpilot.io', 'operator')[0]}"}
VIEWER = {"Authorization": f"Bearer {create_access_token('viewer@dagpilot.io', 'viewer')[0]}"}


@pytest.fixture()
def client_and_session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    session = factory()

    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, raise_server_exceptions=False)

    pipeline = Pipeline(
        code="p1", name="测试链路", line_no="L9", line_color="oklch(0.5 0.1 200)",
        start_clock="01:00", crew="测试组", note="", sort_order=0,
    )
    session.add(pipeline)
    session.flush()
    up = Job(code="up", name="上游", pipeline_id=pipeline.id, layer="DWD", owner="t",
             target_table="", p50_seconds=600, p90_seconds=900, status="active", note="")
    down = Job(code="down", name="下游", pipeline_id=pipeline.id, layer="DWD", owner="t",
               target_table="", p50_seconds=600, p90_seconds=900, status="active", note="")
    session.add_all([up, down])
    session.flush()
    session.add(Dependency(pipeline_id=pipeline.id, upstream_job_id=up.id,
                           downstream_job_id=down.id, dep_type="data", lag_minutes=0))
    for offset in range(10):
        day = date(2026, 11, 11) - timedelta(days=offset)
        base = datetime.combine(day, datetime.min.time())
        session.add(JobRunRecord(
            pipeline_id=pipeline.id, job_id=up.id, biz_date=day, attempt=1, record_version=1,
            status="success", start_at=base.replace(hour=1), end_at=base.replace(hour=1, minute=30),
            ingested_at=base.replace(hour=1, minute=35), source="crontab",
        ))
        session.add(JobRunRecord(
            pipeline_id=pipeline.id, job_id=down.id, biz_date=day, attempt=1, record_version=1,
            status="success", start_at=base.replace(hour=1, minute=40), end_at=base.replace(hour=2),
            ingested_at=base.replace(hour=2, minute=5), source="crontab",
        ))
    session.commit()
    yield client, session
    session.close()


def test_auth_required(client_and_session):
    client, _ = client_and_session
    assert client.get("/api/dep-checks").status_code == 401
    assert client.post("/api/dep-checks").status_code == 401


def test_viewer_can_read_but_cannot_run_or_publish(client_and_session):
    client, _ = client_and_session
    assert client.get("/api/dep-checks", headers=VIEWER).status_code == 200
    assert client.get("/api/dep-checks/current", headers=VIEWER).status_code == 200
    assert client.post("/api/dep-checks", headers=VIEWER).status_code == 403
    assert client.post("/api/dep-checks/1/publish", headers=VIEWER).status_code == 403


def test_operator_runs_check_and_admin_publishes(client_and_session):
    client, _ = client_and_session
    created = client.post("/api/dep-checks", headers=OPERATOR)
    assert created.status_code == 201
    batch = created.json()["batch"]
    assert batch["status"] == "draft"
    assert created.json()["reused"] is False

    # 同一批重复发起：复用草稿，不新增版本
    again = client.post("/api/dep-checks", headers=OPERATOR)
    assert again.json()["reused"] is True
    assert again.json()["batch"]["id"] == batch["id"]

    detail = client.get(f"/api/dep-checks/{batch['id']}", headers=OPERATOR)
    assert detail.status_code == 200
    items = detail.json()["items"]
    edge = next(item for item in items if item["item_kind"] == "edge")
    assert edge["verdict"] == "effective"
    assert edge["sample_days"] == 10
    assert len(edge["evidence"]) == 10

    # operator 无权发布（服务端 403，不是前端藏按钮）
    assert client.post(f"/api/dep-checks/{batch['id']}/publish", headers=OPERATOR).status_code == 403

    published = client.post(f"/api/dep-checks/{batch['id']}/publish", headers=ADMIN)
    assert published.status_code == 200
    assert published.json()["batch"]["version"] == 1

    current = client.get("/api/dep-checks/current", headers=VIEWER)
    payload = current.json()
    assert payload["has_current"] is True
    assert payload["batch"]["version"] == 1
    assert payload["batch"]["is_stale"] is False
    assert len(payload["items"]) == len(items)

    # 已发布的批次不能再次发布
    assert client.post(f"/api/dep-checks/{batch['id']}/publish", headers=ADMIN).status_code == 409


def test_run_facts_endpoint_groups_by_pipeline_and_date(client_and_session):
    client, _ = client_and_session
    payload = client.get("/api/run-facts?pipeline_code=p1", headers=VIEWER).json()
    assert payload["summary"]["fact_days"] == 20  # 2 作业 × 10 天
    assert payload["summary"]["rejected"] == 0
    one_day = client.get("/api/run-facts?pipeline_code=p1&biz_date=2026-11-11", headers=VIEWER).json()
    assert len(one_day["facts"]) == 2
    assert {row["job_code"] for row in one_day["facts"]} == {"up", "down"}
    assert client.get("/api/run-facts?pipeline_code=missing", headers=VIEWER).status_code == 404
