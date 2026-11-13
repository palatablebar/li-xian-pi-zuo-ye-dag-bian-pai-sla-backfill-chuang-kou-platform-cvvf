"""调度器上报的原始作业运行记录（作业粒度）。

重试、迟到上报、旧版本重发都混在这张表里——体检前必须先按
（链路, 业务日期, 作业）整理出唯一的有效记录，见 run_fact_service。
时间一律存业务时钟 naive（与 pipeline_runs.created_at 同口径）。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class JobRunRecord(Base):
    __tablename__ = "job_run_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    biz_date: Mapped[date] = mapped_column(Date, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)          # 第几次尝试（重试递增）
    record_version: Mapped[int] = mapped_column(Integer, default=1)   # 同一尝试的上报版本
    status: Mapped[str] = mapped_column(String(16), default="success")
    start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime)           # 记录入库时刻（迟到判定）
    source: Mapped[str] = mapped_column(String(40), default="crontab")
    seed_key: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
