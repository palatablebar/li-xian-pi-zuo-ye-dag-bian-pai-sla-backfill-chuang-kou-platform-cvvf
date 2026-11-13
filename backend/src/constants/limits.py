"""算法阈值与跑批约束常量（口径集中，禁止散落在 service 里）。"""
from __future__ import annotations

RATE_WINDOW_SECONDS = 60

# SLA 缓冲（分钟）：latest_start = SLA 截止 - P90 链路时长 - 该缓冲
DEFAULT_SLA_BUFFER_MINUTES = 30
# 判定「缓冲紧张」的余量阈值（分钟）
TIGHT_MARGIN_MINUTES = 15
# 可回填天数上限（补多会挤爆集群，产品口径封顶 7 天）
MAX_BACKFILL_DAYS_CAP = 7
# 当天最晚可开工时刻（分钟，从 00:00 起算）——02:00 之后起跑不再占用夜间窗口
LATEST_BACKFILL_START_MINUTE = 2 * 60
# 单次模拟允许的最大作业数（防误配超大 DAG 拖垮接口）
MAX_JOBS_PER_SIMULATION = 400
# 依赖导入单次最大条数
MAX_IMPORT_EDGES = 200
# 历史运行保留天数（seed 演示）
HISTORY_DAYS = 30

# ---- 依赖体检口径 ----
# 体检算法版本：口径调整时 +1，旧批次指纹随之失效重算
CHECK_ALGO_VERSION = 1
# 回看窗口（个业务日期）
CHECK_WINDOW_DAYS = 30
# 单边最少可配对样本天数，低于此值一律待核
CHECK_MIN_SAMPLE_DAYS = 5
# 遵守率 ≥ 该阈值判定「有效」
CHECK_OBEY_RATIO_EFFECTIVE = 0.9
# 违背率 ≥ 该阈值判定「长期违背」；两阈值之间为「证据不足」
CHECK_VIOLATE_RATIO_LONG_TERM = 0.5
# 时间证据冲突日期占比超过该阈值，整条边待核
CHECK_CONFLICT_RATIO_PENDING = 0.2
# 「前后相接」的最大间隔（分钟）：下游起跑 − 上游结束 ∈ [0, 10]
CHECK_ADJACENT_GAP_MINUTES = 10
# 相接占比 ≥ 该阈值才列为补边候选
CHECK_ADJACENT_RATIO_SUGGEST = 0.8
# 记录入库时间晚于运行结束超过该小时数，标记为「迟到记录」
CHECK_LATE_RECORD_HOURS = 6
