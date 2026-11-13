"""依赖体检：批次（草稿 → 整批发布）+ 逐条结论。

批次生命周期：
  draft → published → superseded（被更新的发布取代）
  draft → stale（未发布时上游记录已变，结论失效）

幂等口径：input_fingerprint 由（原始记录 + 依赖边 + 作业状态 + 算法版本）
哈希得出；同指纹重复发起复用旧批次不新增，指纹变化后旧发布结论标记失效。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class DepCheckBatch(Base):
    __tablename__ = "dep_check_batches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    scope: Mapped[str] = mapped_column(String(16), default="all")     # 整批全链路
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    version: Mapped[int] = mapped_column(Integer, default=0)          # 发布序号，草稿为 0
    input_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)           # 各结论计数 + 整理统计
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(160), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DepCheckItem(Base):
    __tablename__ = "dep_check_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("dep_check_batches.id"), index=True)
    item_kind: Mapped[str] = mapped_column(String(16), default="edge", index=True)
    pipeline_code: Mapped[str] = mapped_column(String(40), index=True)
    upstream_code: Mapped[str] = mapped_column(String(64))
    downstream_code: Mapped[str] = mapped_column(String(64))
    dep_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lag_minutes: Mapped[int] = mapped_column(Integer, default=0)
    verdict: Mapped[str] = mapped_column(String(16), index=True)
    sample_days: Mapped[int] = mapped_column(Integer, default=0)      # 可配对业务日期数
    support_days: Mapped[int] = mapped_column(Integer, default=0)     # 遵守 / 前后相接的天数
    reason: Mapped[str] = mapped_column(Text, default="")             # 结论依据 / 待核原因
    evidence: Mapped[list] = mapped_column(JSON, default=list)        # 逐日配对证据快照
    counterexamples: Mapped[list] = mapped_column(JSON, default=list)  # 候选反例
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
