/** 共享枚举（前端副本）。后端唯一定义处：backend/src/constants/enums.py */
export type RiskLevel = 'over' | 'tight' | 'ok' | 'no_sla';

export const RISK_LABELS: Record<RiskLevel, string> = {
  over: '超 SLA',
  tight: '缓冲紧张',
  ok: '达标',
  no_sla: '无 SLA',
};

export const RISK_CLASS: Record<RiskLevel, string> = {
  over: 'pill over',
  tight: 'pill tight',
  ok: 'pill ok',
  no_sla: 'pill muted',
};

export type JobLayer = 'ODS' | 'DWD' | 'DWS' | 'ADS' | 'FEATURE';

export const LAYER_LABELS: Record<string, string> = {
  ODS: '接入层 ODS',
  DWD: '明细层 DWD',
  DWS: '汇总层 DWS',
  ADS: '应用层 ADS',
  FEATURE: '特征层',
};

export const LAYER_ORDER: JobLayer[] = ['ODS', 'DWD', 'DWS', 'ADS', 'FEATURE'];

export type RunStatus = 'success' | 'late' | 'failed' | 'simulated';

export const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  success: '成功',
  late: '迟出',
  failed: '失败',
  simulated: '模拟',
};

export const DEP_TYPE_LABELS: Record<string, string> = {
  data: '数据依赖',
  schedule: '调度依赖',
};

export type CheckVerdict = 'effective' | 'violated' | 'insufficient' | 'pending' | 'suggested';

export const VERDICT_LABELS: Record<CheckVerdict, string> = {
  effective: '有效',
  violated: '长期违背',
  insufficient: '证据不足',
  pending: '待核',
  suggested: '建议补边',
};

export const VERDICT_CLASS: Record<CheckVerdict, string> = {
  effective: 'pill ok',
  violated: 'pill over',
  insufficient: 'pill tight',
  pending: 'pill muted',
  suggested: 'pill ok',
};

export const CHECK_BATCH_STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  published: '已发布',
  superseded: '已被取代',
  stale: '已失效',
};

export const REJECT_REASON_LABELS: Record<string, string> = {
  superseded_by_retry: '被重试取代',
  outdated_version: '旧版本记录',
  time_inversion: '时间倒挂',
  conflicting_final: '最终记录冲突',
};

export type RunMode = 'p50' | 'p90' | 'both';

export const RUN_MODE_LABELS: Record<RunMode, string> = {
  p50: 'P50 常规',
  p90: 'P90 风险',
  both: '双口径',
};
