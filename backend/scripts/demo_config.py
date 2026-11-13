"""seed 演示配置：链路 / 作业 / 依赖 / SLA 的唯一定义处。

数值不是随手写的——它们是被主流程验收锁死的：
  报表出数线 P50 关键路径 = 192 分钟（3h12m），P90 = 348 分钟（5h48m）
  01:00 起跑 + 348 → 06:48 出数，SLA 截止 06:30 → 超 18 分钟
  latest_start = 06:30 − 5h48m − 30min = 00:12 → 实际 01:00 起跑，晚 48 分钟
  四条链路同一晚按 P50 起跑，扫描线峰值并发 = 7（01:34 时刻）
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineSpec:
    code: str
    name: str
    line_no: str
    line_color: str
    start_clock: str
    crew: str
    note: str


@dataclass(frozen=True)
class JobSpec:
    code: str
    name: str
    pipeline: str
    layer: str
    owner: str
    target_table: str
    p50_minutes: int
    p90_minutes: int


@dataclass(frozen=True)
class EdgeSpec:
    pipeline: str
    upstream: str
    downstream: str
    dep_type: str
    lag_minutes: int


@dataclass(frozen=True)
class SlaSpec:
    pipeline: str
    name: str
    deadline_clock: str
    buffer_minutes: int
    owner_team: str
    consumer: str
    note: str


PIPELINES: list[PipelineSpec] = [
    PipelineSpec("ingest", "交易接入线", "L1", "oklch(0.55 0.175 28)", "01:00", "接入组 · 唐棠",
                 "POS 与 App 埋点落地，天亮前必须完成明细层"),
    PipelineSpec("dim", "维表构建线", "L2", "oklch(0.52 0.095 205)", "01:00", "维表组 · 罗野",
                 "日期/商品/门店/会员四张维表，报表与特征都吃它"),
    PipelineSpec("report", "报表出数线", "L3", "oklch(0.45 0.105 152)", "01:00", "报表组 · 郑迟",
                 "早会 07:00 前要出数，本链路是全组 SLA 最紧的一条"),
    PipelineSpec("ml", "特征回流线", "L4", "oklch(0.62 0.145 66)", "02:30", "算法组 · 韩沐",
                 "特征表推回训练环境，无下游 SLA，白天补跑也行"),
]

JOB_SPECS: list[JobSpec] = [
    # 交易接入线 L1（6 作业）
    JobSpec("ods_pos_order_sync", "POS 订单同步", "ingest", "ODS", "唐棠", "ods.pos_order", 25, 43),
    JobSpec("ods_app_event_sync", "App 埋点同步", "ingest", "ODS", "唐棠", "ods.app_event", 18, 40),
    JobSpec("dwd_order_detail", "订单明细构建", "ingest", "DWD", "唐棠", "dwd.order_detail", 41, 83),
    JobSpec("dwd_pay_flow", "支付流水构建", "ingest", "DWD", "罗野", "dwd.pay_flow", 26, 52),
    JobSpec("dws_trade_minute", "分钟级交易汇总", "ingest", "DWS", "罗野", "dws.trade_minute", 33, 68),
    JobSpec("dqc_trade_rules", "交易质量规则", "ingest", "DWS", "何静", "dws.trade_dqc", 12, 24),
    # 维表构建线 L2（6 作业）
    JobSpec("dim_date_build", "日期维生成", "dim", "DWD", "罗野", "dim.dim_date", 8, 13),
    JobSpec("dim_product_full", "商品维全量", "dim", "DWD", "罗野", "dim.dim_product", 35, 59),
    JobSpec("dim_store_full", "门店维全量", "dim", "DWD", "沈岸", "dim.dim_store", 24, 41),
    JobSpec("dim_member_scd", "会员拉链维", "dim", "DWD", "沈岸", "dim.dim_member", 47, 97),
    JobSpec("dim_channel_map", "渠道映射维", "dim", "DWD", "何静", "dim.dim_channel", 19, 32),
    JobSpec("dim_wide_publish", "维表宽表发布", "dim", "DWS", "沈岸", "dws.dim_wide", 28, 56),
    # 报表出数线 L3（7 作业）—— 关键路径 34+52+45+33+28 = 192
    JobSpec("ads_report_stage", "报表中间层", "report", "ADS", "郑迟", "ads.report_stage", 34, 62),
    JobSpec("ads_trade_daily", "交易日报", "report", "ADS", "郑迟", "ads.trade_daily", 52, 96),
    JobSpec("ads_member_daily", "会员日报", "report", "ADS", "沈岸", "ads.member_daily", 38, 70),
    JobSpec("ads_store_daily", "门店日报", "report", "ADS", "韩沐", "ads.store_daily", 33, 61),
    JobSpec("ads_report_join", "日报拼装", "report", "ADS", "郑迟", "ads.report_join", 45, 84),
    JobSpec("ads_report_qc", "日报口径校验", "report", "ADS", "何静", "ads.report_qc", 33, 61),
    JobSpec("ads_report_export", "日报出数与分发", "report", "ADS", "郑迟", "ads.report_export", 28, 45),
    # 特征回流线 L4（5 作业）
    JobSpec("feature_trade_window", "交易滑窗特征", "ml", "DWS", "韩沐", "dws.feat_trade", 29, 54),
    JobSpec("feature_member_rfm", "会员 RFM 特征", "ml", "DWS", "韩沐", "dws.feat_rfm", 33, 66),
    JobSpec("feature_store_profile", "门店画像特征", "ml", "DWS", "韩沐", "dws.feat_store", 24, 45),
    JobSpec("feature_label_join", "样本标签拼接", "ml", "FEATURE", "何静", "feat.label_join", 31, 57),
    JobSpec("feature_publish_hive", "特征推回训练环境", "ml", "FEATURE", "韩沐", "feat.publish", 19, 35),
]

DEPENDENCIES: list[EdgeSpec] = [
    # L1 交易接入线 10 条
    EdgeSpec("ingest", "ods_pos_order_sync", "dwd_order_detail", "data", 0),
    EdgeSpec("ingest", "ods_pos_order_sync", "dwd_pay_flow", "data", 0),
    EdgeSpec("ingest", "ods_app_event_sync", "dwd_order_detail", "data", 0),
    EdgeSpec("ingest", "ods_app_event_sync", "dwd_pay_flow", "data", 0),
    EdgeSpec("ingest", "dwd_order_detail", "dws_trade_minute", "data", 0),
    EdgeSpec("ingest", "dwd_pay_flow", "dws_trade_minute", "data", 0),
    EdgeSpec("ingest", "dwd_order_detail", "dqc_trade_rules", "schedule", 0),
    EdgeSpec("ingest", "dwd_pay_flow", "dqc_trade_rules", "schedule", 0),
    EdgeSpec("ingest", "ods_pos_order_sync", "dws_trade_minute", "schedule", 5),
    EdgeSpec("ingest", "ods_app_event_sync", "dqc_trade_rules", "schedule", 5),
    # L2 维表构建线 9 条
    EdgeSpec("dim", "dim_date_build", "dim_product_full", "data", 0),
    EdgeSpec("dim", "dim_date_build", "dim_store_full", "data", 0),
    EdgeSpec("dim", "dim_date_build", "dim_channel_map", "data", 0),
    EdgeSpec("dim", "dim_product_full", "dim_member_scd", "data", 0),
    EdgeSpec("dim", "dim_store_full", "dim_channel_map", "schedule", 0),
    EdgeSpec("dim", "dim_product_full", "dim_wide_publish", "data", 0),
    EdgeSpec("dim", "dim_member_scd", "dim_wide_publish", "data", 0),
    EdgeSpec("dim", "dim_channel_map", "dim_wide_publish", "data", 0),
    EdgeSpec("dim", "dim_store_full", "dim_member_scd", "schedule", 3),
    # L3 报表出数线 12 条
    EdgeSpec("report", "ads_report_stage", "ads_trade_daily", "data", 0),
    EdgeSpec("report", "ads_report_stage", "ads_member_daily", "data", 0),
    EdgeSpec("report", "ads_report_stage", "ads_store_daily", "schedule", 0),
    EdgeSpec("report", "ads_trade_daily", "ads_report_join", "data", 0),
    EdgeSpec("report", "ads_member_daily", "ads_report_join", "data", 0),
    EdgeSpec("report", "ads_store_daily", "ads_report_join", "data", 0),
    EdgeSpec("report", "ads_report_join", "ads_report_qc", "data", 0),
    EdgeSpec("report", "ads_report_qc", "ads_report_export", "data", 0),
    EdgeSpec("report", "ads_trade_daily", "ads_report_qc", "schedule", 0),
    EdgeSpec("report", "ads_member_daily", "ads_report_qc", "schedule", 0),
    EdgeSpec("report", "ads_store_daily", "ads_report_qc", "schedule", 0),
    EdgeSpec("report", "ads_report_stage", "ads_report_join", "schedule", 8),
    # L4 特征回流线 7 条
    EdgeSpec("ml", "feature_trade_window", "feature_label_join", "data", 0),
    EdgeSpec("ml", "feature_member_rfm", "feature_label_join", "data", 0),
    EdgeSpec("ml", "feature_store_profile", "feature_label_join", "data", 0),
    EdgeSpec("ml", "feature_trade_window", "feature_publish_hive", "schedule", 0),
    EdgeSpec("ml", "feature_member_rfm", "feature_publish_hive", "schedule", 0),
    EdgeSpec("ml", "feature_label_join", "feature_publish_hive", "data", 0),
    EdgeSpec("ml", "feature_store_profile", "feature_publish_hive", "schedule", 0),
]

SLA_POLICIES: list[SlaSpec] = [
    SlaSpec("report", "早会报表 SLA", "06:30", 30, "报表组", "经营分析早会 07:00",
            "P90 出数 06:48，超 18 分钟：关键路径上交易日报与日报拼装是稳定瓶颈"),
    SlaSpec("ingest", "交易链路 SLA", "05:00", 20, "接入组", "数仓明细层下游任务",
            "P90 3h14m，01:00 起跑 04:14 完成，卡在窗口内"),
    SlaSpec("dim", "维表链路 SLA", "05:30", 25, "维表组", "报表组与算法组",
            "P90 3h45m，01:00 起跑 04:45 完成，缓冲 45 分钟"),
]

DEMO_USERS = [
    ("admin@dagpilot.io", "沈岸 · 数仓负责人", "admin"),
    ("operator@dagpilot.io", "郑迟 · 值班工程师", "operator"),
    ("viewer@dagpilot.io", "林岚 · 业务方（只读）", "viewer"),
]

DEMO_PASSWORD = "Demo@2026"

# 历史运行：锚定日期往前 30 天 × 4 条链路 = 120 条
HISTORY_ANCHOR = "2026-11-11"
HISTORY_DAYS = 30
# 每条链路的历史迟出/失败天数（用固定步长挑日期，保证幂等可复现）
HISTORY_LATE_DAYS = {
    "report": [2, 5, 9, 13, 20, 26],   # 报表链路最紧张，30 天里 6 次迟出
    "ingest": [7, 18],
    "dim": [4, 11, 23],
    "ml": [],
}
HISTORY_FAILED_DAYS = {
    "report": [17],
    "ingest": [24],
    "dim": [],
    "ml": [8],
}

# 历史总时长抖动（分钟），按 (链路, 第几天) 确定性取值，不用随机数
HISTORY_JITTER = {
    "report": [-6, 4, -2, 7, 0, 5, -4, 3, 1, 9],
    "ingest": [3, -5, 2, 0, 6, -3, 4, 1],
    "dim": [-3, 5, 0, 2, -6, 4, 1],
    "ml": [2, -4, 5, 0, 3],
}


# ---------------------------------------------------------------- 依赖体检剧本
# 原始调度记录（job_run_records）按剧本生成：30 天历史里混进重试 / 迟到上报 /
# 旧版本重发 / 时间冲突，并保证体检产出 有效 / 长期违背 / 证据不足 / 待核 /
# 补边候选 全部情形。全部按 day_index（锚定日往前第几天，0=最近）取模命中，
# 确定性可复现，两次 seed 结果逐字一致。


@dataclass(frozen=True)
class RecordPlay:
    pipeline: str
    job: str
    kind: str                 # 见 seed_demo._apply_play 的说明
    mod: int = 1              # day_index % mod
    hits: tuple[int, ...] = (0,)  # 命中的余数集合
    params: dict = None       # 剧本参数（按 kind 解释）


RECORD_PLAYS: list[RecordPlay] = [
    # L3 报表线：ads_store_daily 七成天数 01:25 抢跑（stage 01:34 才结束）
    # → stage→store_daily 长期违背
    RecordPlay("report", "ads_store_daily", "early_start", 10, (0, 1, 2, 3, 4, 6, 8),
               {"start_minute": 25}),
    # L4 特征线：feature_publish_hive 三成天数提前 5 分钟抢跑
    # → label_join→publish_hive 遵守 70%，落在「证据不足」区间
    RecordPlay("ml", "feature_publish_hive", "early_start", 10, (1, 4, 7),
               {"start_minute": 59}),
    # L4 新作业 feature_store_profile 只有最近 3 天记录 → 相关边样本不足待核
    RecordPlay("ml", "feature_store_profile", "missing_after", 1, (0,),
               {"max_day_index": 2}),
    # L2 dim_channel_map 8 天时间倒挂 + 1 天同版本冲突上报
    # → 相关 3 条边冲突占比 30%，待核
    RecordPlay("dim", "dim_channel_map", "inverted", 4, (1,), {"minutes": 12}),
    RecordPlay("dim", "dim_channel_map", "conflicting_final", 30, (3,), {"shift_minutes": 40}),
    # L2 dim_wide_publish 每 5 天重试一次：attempt1 失败中断，attempt2 成功
    RecordPlay("dim", "dim_wide_publish", "retry", 5, (1,),
               {"first_minutes": 10, "retry_gap_minutes": 15}),
    # L2 dim_member_scd 每 5 天重发一次修正版：v1 作废，v2 有效
    RecordPlay("dim", "dim_member_scd", "reversion", 5, (2,),
               {"v1_end_shift_minutes": -10, "v2_ingest_lag_minutes": 20}),
    # L4 feature_member_rfm 每 6 天迟到上报 8 小时（记录仍有效，但要点名）
    RecordPlay("ml", "feature_member_rfm", "late_ingest", 6, (2,), {"hours": 8}),
    # L1 dqc_trade_rules 稳定等 dws_trade_minute 结束后 3~5 分钟起跑（无声明路径）
    # → 补边候选；每 6 天有 1 天 02:10 自己抢跑（反例）
    RecordPlay("ingest", "dqc_trade_rules", "follow_job", 1, (0,),
               {"follow": "dws_trade_minute", "gap_base": 3, "gap_mod": 3,
                "except_mod": 6, "except_hits": (2,), "except_start_minute": 70}),
]
