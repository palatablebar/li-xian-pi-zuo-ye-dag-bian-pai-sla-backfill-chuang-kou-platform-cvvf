import { http } from './client';
import type {
  DepCheckBatchList,
  DepCheckCurrent,
  DepCheckDetail,
  DepCheckRunResult,
  RunFactsPayload,
} from '../types/models';

export const depChecksApi = {
  run: () => http.post<DepCheckRunResult>('/dep-checks'),
  batches: () => http.get<DepCheckBatchList>('/dep-checks'),
  current: () => http.get<DepCheckCurrent>('/dep-checks/current'),
  detail: (batchId: number) => http.get<DepCheckDetail>(`/dep-checks/${batchId}`),
  publish: (batchId: number) =>
    http.post<{ batch: DepCheckCurrent['batch']; message: string }>(`/dep-checks/${batchId}/publish`),
  runFacts: (pipelineCode: string, bizDate?: string) =>
    http.get<RunFactsPayload>(
      `/run-facts?pipeline_code=${encodeURIComponent(pipelineCode)}${bizDate ? `&biz_date=${bizDate}` : ''}`,
    ),
};
