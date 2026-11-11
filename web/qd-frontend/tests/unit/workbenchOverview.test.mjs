import test from 'node:test'
import assert from 'node:assert/strict'
import {
  eodNotDueCopy,
  expandWorkbenchRows,
  formatCutoff,
  formatUnknownCutoff,
  latestReport,
  reportRows,
  statusMeta,
  summarizeWorkbench,
  unknownOr
} from '../../src/utils/workbench.js'

const overview = {
  contract_version: 'workbench.overview.v1',
  generated_at: '2026-09-20T09:00:00+08:00',
  partial: true,
  all_unavailable: false,
  unavailable_items: ['execution_digest'],
  not_integrated_items: [],
  items: [
    { id: 'calendar_coverage', label: '交易日历覆盖', status: 'ok', data_cutoff: '2026-12-31' },
    { id: 'digest_inputs', label: '研究摘要行情截止', status: 'ok', data_cutoff: '2026-09-18' },
    { id: 'eod_status', label: '收盘任务', status: 'recovered', data_cutoff: '2026-09-18' },
    { id: 'execution_digest', label: '执行摘要', status: 'unavailable', detail: '权限不足' }
  ]
}

test('summarizes partial availability and keeps open questions', () => {
  const summary = summarizeWorkbench(overview)
  assert.equal(summary.overall, 'partial')
  assert.equal(summary.overallLabel, '部分不可用')
  assert.ok(summary.openQuestions.join(' ').includes('执行摘要'))
  assert.deepEqual(summary.cutoffs.map(row => row.id), ['digest_inputs', 'eod_status'])
})

test('reports all-unavailable explicitly', () => {
  const summary = summarizeWorkbench({ ...overview, all_unavailable: true, partial: true })
  assert.equal(summary.overall, 'unavailable')
  assert.ok(summary.openQuestions[0].includes('不能下任何结论'))
})

test('always marks degraded statuses as open questions', () => {
  const summary = summarizeWorkbench({
    ...overview,
    items: [
      { id: 'eod_status', label: '收盘任务', status: 'evidence_missing', detail: '超过 15:50 仍无当日记录' },
      { id: 'release_version', label: '发布版本', status: 'not_integrated' }
    ]
  })
  assert.ok(summary.openQuestions.join(' ').includes('证据缺失'))
  assert.ok(summary.openQuestions.join(' ').includes('待接入'))
})

test('uses the acceptance-deadline copy for not_due', () => {
  assert.ok(eodNotDueCopy({ status: 'not_due' }).includes('尚未到最终验收时点'))
  assert.equal(eodNotDueCopy({ status: 'ok' }), '')
})

test('formats cutoffs and statuses', () => {
  assert.equal(formatCutoff('2026-09-18T15:50:00+08:00'), '2026-09-18 15:50:00')
  assert.equal(formatCutoff(null), '—')
  assert.ok(statusMeta('recovered').label.includes('已恢复'))
  assert.equal(statusMeta('unknown-status').label, '未知状态')
})

test('picks the newest readable report and keeps per-format metadata', () => {
  const payload = {
    reports: [
      { id: 'a', label: '日报', format: 'json', status: 'ok', generated_at: '2026-09-18T15:50:00+08:00', schema_version: 2 },
      { id: 'b', label: '摘要', format: 'markdown', status: 'ok', file_mtime: '2026-09-20T06:00:00+08:00' },
      { id: 'c', label: '坏文件', format: 'json', status: 'unavailable', detail: 'JSON 报告不可读' }
    ]
  }
  assert.equal(latestReport(payload).id, 'b')
  assert.deepEqual(reportRows(payload).map(row => row.format), ['json', 'markdown', 'json'])
  assert.equal(reportRows(payload)[2].statusMeta.label, '不可用')
})

test('unknownOr and formatUnknownCutoff never invent values', () => {
  assert.equal(unknownOr(null), '未知')
  assert.equal(unknownOr(''), '未知')
  assert.equal(unknownOr('表现承压'), '表现承压')
  assert.equal(formatUnknownCutoff(null), '未知')
  assert.equal(formatUnknownCutoff('2026-09-18'), '2026-09-18')
})

test('expands profit radar files without swapping conclusions or using aggregate cutoff', () => {
  const rows = expandWorkbenchRows([
    {
      id: 'profit_radar',
      label: 'Profit Radar 报告',
      status: 'ok',
      source: 'whitelist',
      data_cutoff: '2026-09-18T15:35:36+08:00',
      detail: '模拟跟踪 ≠ 账户收益；报告内盈亏为模拟/逐笔统计，不是账户收益',
      value: {
        files: [
          {
            file: '026a1083d9e0/latest.md',
            status: 'ok',
            conclusion: '表现承压，盈利优势未建立',
            generated_at: '2026-09-18T15:35:36+08:00',
            market_cutoff: null,
            market_cutoff_date: null
          },
          {
            file: 'ec6c86fd856c/latest.md',
            status: 'ok',
            conclusion: null,
            generated_at: '2026-09-18T15:10:37+08:00',
            market_cutoff: '2026-09-18｜收盘口径，非实时',
            market_cutoff_date: '2026-09-18'
          }
        ]
      }
    }
  ])
  assert.equal(rows.length, 3)
  assert.equal(rows[0].id, 'profit_radar')
  assert.ok(rows[0].detail.includes('模拟跟踪'))
  assert.equal(rows[0].cutoff, '见分文件')
  assert.notEqual(rows[0].cutoff, '2026-09-18T15:35:36+08:00')

  const a = rows.find(r => r.id.includes('026a'))
  const b = rows.find(r => r.id.includes('ec6c'))
  assert.ok(a && b)
  assert.equal(a.cutoff, '未知')
  assert.equal(b.cutoff, '2026-09-18')
  assert.ok(a.detail.includes('表现承压，盈利优势未建立'))
  assert.ok(a.detail.includes('行情截止日：未知'))
  assert.ok(b.detail.includes('结论：未知'))
  assert.ok(b.detail.includes('行情截止日：2026-09-18'))
  // conclusions must not be swapped
  assert.ok(!b.detail.includes('表现承压'))
  assert.ok(a.detail.startsWith('结论：表现承压'))
  assert.ok(!a.detail.includes('结论：未知'))
})

test('reportRows surfaces per-file profit summary fields', () => {
  const rows = reportRows({
    reports: [
      {
        id: 'profit_radar_0',
        label: 'Profit Radar 报告（026a1083d9e0/latest.md）',
        format: 'markdown',
        status: 'ok',
        generated_at: '2026-09-18T15:35:36+08:00',
        summary: {
          conclusion: '表现承压，盈利优势未建立',
          generated_at: '2026-09-18T15:35:36+08:00',
          market_cutoff: null,
          market_cutoff_date: null,
          disclaimer: '模拟跟踪 ≠ 账户收益'
        }
      },
      {
        id: 'profit_radar_1',
        label: 'Profit Radar 报告（ec6c86fd856c/latest.md）',
        format: 'markdown',
        status: 'ok',
        generated_at: '2026-09-18T15:10:37+08:00',
        summary: {
          conclusion: null,
          generated_at: '2026-09-18T15:10:37+08:00',
          market_cutoff_date: '2026-09-18',
          disclaimer: '模拟跟踪 ≠ 账户收益'
        }
      }
    ]
  })
  assert.ok(rows[0].detail.includes('结论：表现承压'))
  assert.ok(rows[0].detail.includes('行情截止日：未知'))
  assert.ok(rows[1].detail.includes('结论：未知'))
  assert.ok(rows[1].detail.includes('行情截止日：2026-09-18'))
  assert.ok(!rows[1].detail.includes('表现承压'))
})
