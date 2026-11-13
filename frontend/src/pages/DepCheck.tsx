import { Fragment, useMemo, useState } from 'react';
import EmptyState from '../components/common/EmptyState';
import { depChecksApi } from '../api/depChecks';
import { jobsApi } from '../api/jobs';
import { useAuth } from '../router/guards';
import { can } from '../constants/roles';
import { CHECK_BATCH_STATUS_LABELS, VERDICT_CLASS, VERDICT_LABELS, REJECT_REASON_LABELS } from '../constants/enums';
import { useAsync } from '../hooks/useAsync';
import { uiStore } from '../stores/uiStore';
import { errorMessage } from '../utils/errorMessage';
import type { CheckVerdictType, DepCheckItem, Pipeline } from '../types/models';

const VERDICT_ORDER: CheckVerdictType[] = ['violated', 'insufficient', 'pending', 'effective', 'suggested'];

function VerdictPill({ verdict }: { verdict: CheckVerdictType }) {
  return <span className={VERDICT_CLASS[verdict]}>{VERDICT_LABELS[verdict]}</span>;
}

function EvidenceTable({ item }: { item: DepCheckItem }) {
  const isEdge = item.item_kind === 'edge';
  return (
    <div style={{ marginTop: 8 }}>
      <table className="table">
        <thead>
          <tr>
            <th>业务日期</th>
            <th className="num">上游完成</th>
            <th className="num">下游起跑</th>
            <th className="num">间隔(分钟)</th>
            <th>判定</th>
          </tr>
        </thead>
        <tbody>
          {item.evidence.map((pair) => {
            const ok = isEdge ? pair.obey : pair.adjacent;
            return (
              <tr key={pair.biz_date}>
                <td className="mono tiny">{pair.biz_date}</td>
                <td className="num mono tiny">{pair.up_end}</td>
                <td className="num mono tiny">{pair.down_start}</td>
                <td className="num mono tiny">{pair.gap_minutes}</td>
                <td>
                  <span className={`pill ${ok ? 'ok' : 'over'}`}>
                    {isEdge ? (ok ? '遵守' : '抢跑') : ok ? '相接' : pair.gap_minutes < 0 ? '颠倒' : '间隔过大'}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {item.counterexamples.length > 0 && (
        <div className="tiny" style={{ marginTop: 6 }}>
          <b>反例 {item.counterexamples.length} 天：</b>
          {item.counterexamples.map((cex) => (
            <div key={cex.biz_date} className="muted">
              {cex.biz_date} · {cex.detail}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function DepCheck() {
  const { user } = useAuth();
  const canRun = can(user?.role, 'check:run');
  const canPublish = can(user?.role, 'check:publish');

  const currentQuery = useAsync(() => depChecksApi.current(), []);
  const batchesQuery = useAsync(() => depChecksApi.batches(), []);
  const pipelinesQuery = useAsync(() => jobsApi.pipelines(), []);
  const pipelines: Pipeline[] = pipelinesQuery.data?.items || [];

  const [selectedBatchId, setSelectedBatchId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [factsPipeline, setFactsPipeline] = useState('');
  const [factsDate, setFactsDate] = useState('');

  const detailQuery = useAsync(
    () => (selectedBatchId ? depChecksApi.detail(selectedBatchId) : Promise.resolve(null)),
    [selectedBatchId],
  );

  const activeFactsPipeline = factsPipeline || pipelines[0]?.code || '';
  const factsQuery = useAsync(
    () =>
      activeFactsPipeline
        ? depChecksApi.runFacts(activeFactsPipeline, factsDate || undefined)
        : Promise.resolve(null),
    [activeFactsPipeline, factsDate],
  );

  // 展示数据：选中某个批次看详情，否则看当前已发布结论
  const viewing = useMemo(() => {
    if (selectedBatchId && detailQuery.data) {
      return { batch: detailQuery.data.batch, items: detailQuery.data.items };
    }
    if (currentQuery.data?.has_current && currentQuery.data.batch) {
      return { batch: currentQuery.data.batch, items: currentQuery.data.items };
    }
    return null;
  }, [selectedBatchId, detailQuery.data, currentQuery.data]);

  const stats = viewing?.batch?.stats || {};
  const statOf = (kind: string, verdict: string) => stats[`${kind}:${verdict}`] || 0;
  const edgeItems = (viewing?.items || []).filter((item) => item.item_kind === 'edge');
  const candidateItems = (viewing?.items || []).filter((item) => item.item_kind === 'candidate');
  const sortedEdges = useMemo(
    () =>
      [...edgeItems].sort(
        (a, b) =>
          VERDICT_ORDER.indexOf(a.verdict) - VERDICT_ORDER.indexOf(b.verdict) ||
          a.pipeline_code.localeCompare(b.pipeline_code) ||
          a.upstream_code.localeCompare(b.upstream_code),
      ),
    [edgeItems],
  );
  const pipelineName = (code: string) => pipelines.find((item) => item.code === code)?.name || code;

  async function runCheck() {
    setBusy(true);
    try {
      const result = await depChecksApi.run();
      uiStore.toast(result.message);
      setSelectedBatchId(result.batch.id);
      currentQuery.reload();
      batchesQuery.reload();
      detailQuery.reload();
    } catch (err) {
      uiStore.toast(errorMessage((err as { payload?: unknown }).payload, '发起体检失败'), true);
    } finally {
      setBusy(false);
    }
  }

  async function publish(batchId: number) {
    setBusy(true);
    try {
      const result = await depChecksApi.publish(batchId);
      uiStore.toast(result.message);
      setSelectedBatchId(null);
      currentQuery.reload();
      batchesQuery.reload();
    } catch (err) {
      uiStore.toast(errorMessage((err as { payload?: unknown }).payload, '发布失败'), true);
      batchesQuery.reload();
    } finally {
      setBusy(false);
    }
  }

  function toggleExpand(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const batches = batchesQuery.data?.items || [];
  const draftBatch = batches.find((item) => item.status === 'draft');

  return (
    <>
      <div className="stat-strip" style={{ marginBottom: 16 }} data-testid="check-summary">
        <div className="stat">
          <div className="k">当前发布</div>
          <div className="v mono" style={{ fontSize: '0.95rem' }}>
            {currentQuery.data?.has_current ? `v${currentQuery.data.batch?.version}` : '未发布'}
          </div>
        </div>
        <div className={`stat${currentQuery.data?.batch?.is_stale ? ' alert' : ''}`}>
          <div className="k">结论状态</div>
          <div className="v" style={{ fontSize: '0.95rem' }}>
            {!currentQuery.data?.has_current ? '—' : currentQuery.data.batch?.is_stale ? '已失效' : '有效'}
          </div>
        </div>
        <div className="stat">
          <div className="k">有效</div>
          <div className="v mono">{viewing ? statOf('edge', 'effective') : '—'}</div>
        </div>
        <div className={`stat${statOf('edge', 'violated') > 0 ? ' alert' : ''}`}>
          <div className="k">长期违背</div>
          <div className="v mono">{viewing ? statOf('edge', 'violated') : '—'}</div>
        </div>
        <div className="stat">
          <div className="k">证据不足</div>
          <div className="v mono">{viewing ? statOf('edge', 'insufficient') : '—'}</div>
        </div>
        <div className="stat">
          <div className="k">待核</div>
          <div className="v mono">{viewing ? statOf('edge', 'pending') + statOf('candidate', 'pending') : '—'}</div>
        </div>
        <div className="stat">
          <div className="k">补边候选</div>
          <div className="v mono">{viewing ? statOf('candidate', 'suggested') : '—'}</div>
        </div>
      </div>

      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="row">
          <div>
            <b>依赖体检</b>
            <div className="tiny muted">
              按链路 + 业务日期整理运行明细，对声明依赖逐条下结论；样本不足或时间证据冲突一律保留待核
            </div>
          </div>
          <span className="spacer" />
          {canRun && (
            <button className="btn" onClick={runCheck} disabled={busy} data-testid="run-check">
              {busy ? '执行中…' : '发起体检'}
            </button>
          )}
          {canPublish && draftBatch && (
            <button
              className="btn btn-primary"
              onClick={() => publish(draftBatch.id)}
              disabled={busy}
              data-testid="publish-check"
            >
              整批发布草稿 {draftBatch.batch_no}
            </button>
          )}
        </div>
        {currentQuery.data?.batch?.is_stale && (
          <div className="error-banner" style={{ marginTop: 10 }} data-testid="stale-banner">
            上游记录或依赖配置已变化，当前发布结论（v{currentQuery.data.batch.version}）已失效，请重新发起体检并发布。
          </div>
        )}
        {draftBatch && (
          <div className="tiny muted" style={{ marginTop: 8 }}>
            草稿 {draftBatch.batch_no} 由 {draftBatch.created_by} 生成，{draftBatch.item_count} 条结论待审核；
            审核通过后由数仓负责人整批发布，发布失败自动保留旧版。
          </div>
        )}
      </div>

      <div className="grid-2">
        <div className="stack">
          <div className="panel">
            <div className="panel-head">
              <h2>现有依赖结论</h2>
              <span className="hint">
                {viewing
                  ? `${viewing.batch.batch_no} · ${CHECK_BATCH_STATUS_LABELS[viewing.batch.status]}${
                      viewing.batch.version ? ` v${viewing.batch.version}` : ''
                    }`
                  : '还没有体检结论'}
              </span>
            </div>
            {!viewing ? (
              <EmptyState title="暂无结论" hint="点击「发起体检」生成第一批草稿，审核发布后这里展示正式结论。" />
            ) : (
              <table className="table" data-testid="edge-verdicts">
                <thead>
                  <tr>
                    <th>依赖边</th>
                    <th>链路</th>
                    <th className="num">样本</th>
                    <th>结论</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {sortedEdges.map((item) => (
                    <Fragment key={item.id}>
                      <tr>
                        <td>
                          <span className="mono tiny">
                            {item.upstream_code} → {item.downstream_code}
                          </span>
                          <div className="tiny muted">
                            {item.upstream_name} → {item.downstream_name} · {item.dep_type_label}
                            {item.lag_minutes ? ` +${item.lag_minutes}m` : ''}
                          </div>
                        </td>
                        <td className="tiny">{pipelineName(item.pipeline_code)}</td>
                        <td className="num mono tiny">
                          {item.support_days}/{item.sample_days}
                        </td>
                        <td>
                          <VerdictPill verdict={item.verdict} />
                        </td>
                        <td className="num">
                          <button className="btn btn-sm" onClick={() => toggleExpand(item.id)}>
                            {expanded.has(item.id) ? '收起' : '证据'}
                          </button>
                        </td>
                      </tr>
                      {expanded.has(item.id) && (
                        <tr key={`${item.id}-evidence`}>
                          <td colSpan={5}>
                            <div className="tiny" style={{ marginBottom: 4 }}>
                              <b>结论依据：</b>
                              {item.reason}
                            </div>
                            <EvidenceTable item={item} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="panel">
            <div className="panel-head">
              <h2>补边候选</h2>
              <span className="hint">无声明路径却稳定前后相接的作业对，反例如实列出</span>
            </div>
            {!candidateItems.length ? (
              <EmptyState title="没有补边候选" hint="所有稳定相接的作业对都已有声明路径，或证据不足以提候选。" />
            ) : (
              <div className="stack">
                {candidateItems.map((item) => (
                  <div key={item.id} className="panel" style={{ boxShadow: 'none', padding: '12px 14px', marginTop: 0 }}>
                    <div className="row" style={{ marginBottom: 4 }}>
                      <b className="mono tiny">
                        {item.upstream_code} → {item.downstream_code}
                      </b>
                      <VerdictPill verdict={item.verdict} />
                      <span className="spacer" />
                      <span className="tiny muted">
                        {pipelineName(item.pipeline_code)} · 相接 {item.support_days}/{item.sample_days} 天
                      </span>
                      <button className="btn btn-sm" onClick={() => toggleExpand(item.id)}>
                        {expanded.has(item.id) ? '收起' : '证据'}
                      </button>
                    </div>
                    <div className="tiny muted">{item.reason}</div>
                    {expanded.has(item.id) && <EvidenceTable item={item} />}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="stack">
          <div className="panel">
            <div className="panel-head">
              <h2>批次历史</h2>
              <span className="hint">草稿 → 整批发布 → 被取代</span>
            </div>
            {!batches.length ? (
              <EmptyState title="还没有体检批次" hint="发起体检后，草稿与发布记录都会留在这里。" />
            ) : (
              <table className="table" data-testid="batch-history">
                <thead>
                  <tr>
                    <th>批次</th>
                    <th>状态</th>
                    <th className="num">结论数</th>
                    <th>生成</th>
                  </tr>
                </thead>
                <tbody>
                  {batches.map((batch) => (
                    <tr
                      key={batch.id}
                      style={{ cursor: 'pointer' }}
                      className={selectedBatchId === batch.id ? 'hl' : ''}
                      onClick={() => setSelectedBatchId(batch.id === selectedBatchId ? null : batch.id)}
                    >
                      <td className="mono tiny">
                        {batch.batch_no}
                        {batch.version ? <span className="tiny muted"> v{batch.version}</span> : null}
                      </td>
                      <td>
                        <span className={`pill ${batch.status === 'published' ? 'ok' : batch.status === 'draft' ? 'tight' : 'muted'}`}>
                          {batch.status_label}
                          {batch.is_stale ? ' · 已失效' : ''}
                        </span>
                      </td>
                      <td className="num mono tiny">{batch.item_count}</td>
                      <td className="tiny muted">{batch.created_by.split('@')[0]}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="tiny muted" style={{ marginBottom: 0 }}>
              同一批重复发起不新增版本；上游记录变化后旧结论自动标记失效，需重新体检。
            </p>
          </div>

          <div className="panel">
            <div className="panel-head">
              <h2>运行明细</h2>
              <span className="hint">按链路 + 业务日期整理；被拒绝的记录带原因</span>
            </div>
            <div className="row" style={{ marginBottom: 10, gap: 8 }}>
              <select
                className="input"
                value={activeFactsPipeline}
                onChange={(event) => setFactsPipeline(event.target.value)}
                style={{ maxWidth: 200 }}
              >
                {pipelines.map((pipeline) => (
                  <option key={pipeline.code} value={pipeline.code}>
                    {pipeline.line_no} {pipeline.name}
                  </option>
                ))}
              </select>
              <input
                className="input"
                type="date"
                value={factsDate}
                onChange={(event) => setFactsDate(event.target.value)}
                style={{ maxWidth: 170 }}
              />
              {factsDate && (
                <button className="btn btn-sm" onClick={() => setFactsDate('')}>
                  全部日期
                </button>
              )}
            </div>
            {factsQuery.data && (
              <>
                <div className="row tiny muted" style={{ marginBottom: 8, gap: 14 }}>
                  <span>原始记录 {factsQuery.data.summary.raw_records}</span>
                  <span>有效明细 {factsQuery.data.summary.fact_days}</span>
                  <span>被拒绝 {factsQuery.data.summary.rejected}</span>
                  <span>冲突 {factsQuery.data.summary.conflict_days}</span>
                </div>
                {factsQuery.data.conflicts.length > 0 && (
                  <div className="error-banner" style={{ marginBottom: 8 }}>
                    {factsQuery.data.conflicts.map((conflict) => (
                      <div key={`${conflict.job_code}-${conflict.biz_date}`} className="tiny">
                        {conflict.biz_date} · {conflict.job_code}：
                        {REJECT_REASON_LABELS[conflict.reason] || conflict.reason}（时间证据冲突，相关结论保留待核）
                      </div>
                    ))}
                  </div>
                )}
                <table className="table" data-testid="run-facts">
                  <thead>
                    <tr>
                      <th>业务日期</th>
                      <th>作业</th>
                      <th className="num">尝试</th>
                      <th className="num">开始</th>
                      <th className="num">结束</th>
                      <th>标记</th>
                    </tr>
                  </thead>
                  <tbody>
                    {factsQuery.data.facts.slice(0, 60).map((fact) => (
                      <tr key={`${fact.job_code}-${fact.biz_date}`}>
                        <td className="mono tiny">{fact.biz_date}</td>
                        <td className="tiny">
                          {fact.job_name}
                          <div className="tiny muted mono">{fact.job_code}</div>
                        </td>
                        <td className="num mono tiny">#{fact.attempt}</td>
                        <td className="num mono tiny">{fact.start_at?.slice(11, 16)}</td>
                        <td className="num mono tiny">{fact.end_at?.slice(11, 16)}</td>
                        <td>
                          {fact.is_late_record ? (
                            <span className="pill tight">迟到 {fact.ingest_lag_minutes}m</span>
                          ) : (
                            <span className="pill ok">有效</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {factsQuery.data.rejected.length > 0 && (
                  <>
                    <div className="tiny" style={{ margin: '10px 0 4px' }}>
                      <b>被拒绝的原始记录（{factsQuery.data.rejected.length}）</b>
                    </div>
                    <table className="table" data-testid="run-facts-rejected">
                      <thead>
                        <tr>
                          <th>业务日期</th>
                          <th>作业</th>
                          <th className="num">尝试/版本</th>
                          <th>拒绝原因</th>
                        </tr>
                      </thead>
                      <tbody>
                        {factsQuery.data.rejected.slice(0, 40).map((row) => (
                          <tr key={row.record_id}>
                            <td className="mono tiny">{row.biz_date}</td>
                            <td className="tiny">{row.job_code}</td>
                            <td className="num mono tiny">
                              #{row.attempt}/v{row.record_version}
                            </td>
                            <td>
                              <span className="pill muted">
                                {REJECT_REASON_LABELS[row.reject_reason] || row.reject_reason}
                              </span>
                              <div className="tiny muted">{row.reject_detail}</div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
