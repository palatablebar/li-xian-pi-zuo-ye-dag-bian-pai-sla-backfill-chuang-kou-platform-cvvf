"""共享枚举（后端唯一定义处）。前端副本见 frontend/src/constants/enums.ts。

README「共享枚举出现位置」一节列出全部出现点，改动需两侧同步。
"""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """三角色：数仓负责人 / 值班工程师 / 业务方只读。"""

    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class JobLayer(StrEnum):
    """离线数仓分层（地铁线路的「站段」）。"""

    ODS = "ODS"
    DWD = "DWD"
    DWS = "DWS"
    ADS = "ADS"
    FEATURE = "FEATURE"


LAYER_ORDER: list[str] = [
    JobLayer.ODS.value,
    JobLayer.DWD.value,
    JobLayer.DWS.value,
    JobLayer.ADS.value,
    JobLayer.FEATURE.value,
]


class JobStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DEPRECATED = "deprecated"


class DependencyType(StrEnum):
    """数据依赖（等上游写表）/ 调度依赖（仅等上游结束）。"""

    DATA = "data"
    SCHEDULE = "schedule"


class RunMode(StrEnum):
    """分位口径：历史时长 P50 / P90。"""

    P50 = "p50"
    P90 = "p90"
    BOTH = "both"


class RunStatus(StrEnum):
    """历史运行 / 模拟运行状态。"""

    SUCCESS = "success"
    LATE = "late"
    FAILED = "failed"
    SIMULATED = "simulated"


class RiskLevel(StrEnum):
    """SLA 判定档位，5 页面共用（告警列表按此排序）。"""

    OVER = "over"        # P90 已超 SLA
    TIGHT = "tight"      # 缓冲余量 < 15 分钟
    OK = "ok"
    NO_SLA = "no_sla"


class RunRecordStatus(StrEnum):
    """调度器上报的原始作业记录状态。"""

    SUCCESS = "success"
    FAILED = "failed"
    KILLED = "killed"


class RejectReason(StrEnum):
    """运行明细整理时，原始记录被拒绝作为有效证据的原因。"""

    SUPERSEDED_BY_RETRY = "superseded_by_retry"   # 同业务日期有更新的尝试，本次尝试作废
    OUTDATED_VERSION = "outdated_version"          # 同一尝试的旧版本上报，被新版本取代
    TIME_INVERSION = "time_inversion"              # 结束早于开始，时间证据自相矛盾
    CONFLICTING_FINAL = "conflicting_final"        # 同尝试同版本多条记录时间不一致


class CheckVerdict(StrEnum):
    """体检结论：现有依赖三态 + 待核；补边候选单列。"""

    EFFECTIVE = "effective"        # 有效：下游稳定在上游完成后起跑
    VIOLATED = "violated"          # 长期违背：过半样本里下游抢跑
    INSUFFICIENT = "insufficient"  # 证据不足：遵守与违背混杂，无法定性
    PENDING = "pending"            # 待核：样本不足或时间证据冲突，不下结论
    SUGGESTED = "suggested"        # 建议补边：无声明路径却稳定前后相接


class CheckItemKind(StrEnum):
    EDGE = "edge"            # 现有依赖边的体检结论
    CANDIDATE = "candidate"  # 补边候选（无声明路径的作业对）


class CheckBatchStatus(StrEnum):
    DRAFT = "draft"            # 草稿：已算好，待审核发布
    PUBLISHED = "published"    # 已发布：当前生效结论
    SUPERSEDED = "superseded"  # 曾被发布，已被更新的批次取代
    STALE = "stale"            # 草稿未发布，上游记录已变化，结论失效


RISK_ORDER: dict[str, int] = {
    RiskLevel.OVER.value: 0,
    RiskLevel.TIGHT.value: 1,
    RiskLevel.OK.value: 2,
    RiskLevel.NO_SLA.value: 3,
}

JOB_STATUS_LABELS: dict[str, str] = {
    JobStatus.ACTIVE.value: "启用",
    JobStatus.PAUSED.value: "暂停",
    JobStatus.DEPRECATED.value: "下线",
}

LAYER_LABELS: dict[str, str] = {
    JobLayer.ODS.value: "接入层 ODS",
    JobLayer.DWD.value: "明细层 DWD",
    JobLayer.DWS.value: "汇总层 DWS",
    JobLayer.ADS.value: "应用层 ADS",
    JobLayer.FEATURE.value: "特征层",
}

DEP_TYPE_LABELS: dict[str, str] = {
    DependencyType.DATA.value: "数据依赖",
    DependencyType.SCHEDULE.value: "调度依赖",
}

RUN_STATUS_LABELS: dict[str, str] = {
    RunStatus.SUCCESS.value: "成功",
    RunStatus.LATE.value: "迟出",
    RunStatus.FAILED.value: "失败",
    RunStatus.SIMULATED.value: "模拟",
}

RISK_LABELS: dict[str, str] = {
    RiskLevel.OVER.value: "超 SLA",
    RiskLevel.TIGHT.value: "缓冲紧张",
    RiskLevel.OK.value: "达标",
    RiskLevel.NO_SLA.value: "无 SLA",
}

VERDICT_LABELS: dict[str, str] = {
    CheckVerdict.EFFECTIVE.value: "有效",
    CheckVerdict.VIOLATED.value: "长期违背",
    CheckVerdict.INSUFFICIENT.value: "证据不足",
    CheckVerdict.PENDING.value: "待核",
    CheckVerdict.SUGGESTED.value: "建议补边",
}

REJECT_REASON_LABELS: dict[str, str] = {
    RejectReason.SUPERSEDED_BY_RETRY.value: "被重试取代",
    RejectReason.OUTDATED_VERSION.value: "旧版本记录",
    RejectReason.TIME_INVERSION.value: "时间倒挂",
    RejectReason.CONFLICTING_FINAL.value: "最终记录冲突",
}

CHECK_BATCH_STATUS_LABELS: dict[str, str] = {
    CheckBatchStatus.DRAFT.value: "草稿",
    CheckBatchStatus.PUBLISHED.value: "已发布",
    CheckBatchStatus.SUPERSEDED.value: "已被取代",
    CheckBatchStatus.STALE.value: "已失效",
}
