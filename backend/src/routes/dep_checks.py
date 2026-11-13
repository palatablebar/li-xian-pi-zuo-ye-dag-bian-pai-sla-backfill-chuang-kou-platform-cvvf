"""依赖体检路由：发起检查 / 批次列表 / 详情 / 整批发布 / 读回已发布 / 运行明细。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import CHECK_PUBLISH_ROLES, CHECK_RUN_ROLES, READ_ROLES
from src.controllers import dep_check_controller
from src.middlewares.rbac import require_roles

router = APIRouter(tags=["dep-check"])


@router.post("/dep-checks", status_code=201)
def run_check(
    request: Request,
    db: Session = Depends(get_db),
    actor: dict = Depends(require_roles(*CHECK_RUN_ROLES)),
) -> dict:
    return dep_check_controller.run_check(db, actor, request)


@router.get("/dep-checks")
def list_batches(
    db: Session = Depends(get_db),
    _: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return dep_check_controller.list_batches(db)


@router.get("/dep-checks/current")
def current_published(
    db: Session = Depends(get_db),
    _: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return dep_check_controller.current(db)


@router.get("/dep-checks/{batch_id}")
def batch_detail(
    batch_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return dep_check_controller.get_batch(db, batch_id)


@router.post("/dep-checks/{batch_id}/publish")
def publish_batch(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: dict = Depends(require_roles(*CHECK_PUBLISH_ROLES)),
) -> dict:
    return dep_check_controller.publish_batch(db, batch_id, actor, request)


@router.get("/run-facts")
def run_facts(
    pipeline_code: str = Query(...),
    biz_date: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return dep_check_controller.run_facts(db, pipeline_code, biz_date)
