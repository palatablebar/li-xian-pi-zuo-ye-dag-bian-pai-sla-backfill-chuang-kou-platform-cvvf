"""依赖体检接口的控制器：发起检查 / 批次列表 / 详情 / 发布 / 读回已发布 / 运行明细。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, dep_check_service, job_service, run_fact_service


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def run_check(db: Session, actor: dict, request: Request) -> dict:
    result = dep_check_service.run_check(db, actor["email"])
    batch = result["batch"]
    audit_service.record(
        db,
        actor_email=actor["email"],
        actor_role=actor["role"],
        action="check.run",
        entity="dep_check_batch",
        entity_id=batch.batch_no,
        detail={"reused": result["reused"], "items": batch.item_count, "status": batch.status},
        ip=_ip(request),
    )
    db.commit()
    return {
        "batch": dep_check_service.serialize_batch(batch, dep_check_service.compute_fingerprint(db)),
        "reused": result["reused"],
        "message": result["message"],
    }


def list_batches(db: Session) -> dict:
    return dep_check_service.list_batches(db)


def get_batch(db: Session, batch_id: int) -> dict:
    return dep_check_service.get_batch_detail(db, batch_id)


def publish_batch(db: Session, batch_id: int, actor: dict, request: Request) -> dict:
    batch = dep_check_service.publish_batch(db, batch_id, actor["email"])
    audit_service.record(
        db,
        actor_email=actor["email"],
        actor_role=actor["role"],
        action="check.publish",
        entity="dep_check_batch",
        entity_id=batch.batch_no,
        detail={"version": batch.version, "items": batch.item_count},
        ip=_ip(request),
    )
    db.commit()
    return {
        "batch": dep_check_service.serialize_batch(batch, dep_check_service.compute_fingerprint(db)),
        "message": f"已整批发布为 v{batch.version}，旧版结论已归档",
    }


def current(db: Session) -> dict:
    return dep_check_service.current_published(db)


def run_facts(db: Session, pipeline_code: str, biz_date: str | None) -> dict:
    pipeline = job_service.get_pipeline(db, pipeline_code)
    payload = run_fact_service.pipeline_run_facts(db, pipeline)
    if biz_date:
        payload["facts"] = [row for row in payload["facts"] if row["biz_date"] == biz_date]
        payload["rejected"] = [row for row in payload["rejected"] if row["biz_date"] == biz_date]
        payload["conflicts"] = [row for row in payload["conflicts"] if row["biz_date"] == biz_date]
    payload["biz_date"] = biz_date
    return payload
