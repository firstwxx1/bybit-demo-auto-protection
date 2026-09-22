#!/usr/bin/env node
'use strict';

const fs = require('fs');
const workflow = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const fixture = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

if (workflow.active !== false) throw new Error('workflow must be inactive by default');
if (workflow.nodes.length !== 18) throw new Error(`expected 18 nodes, got ${workflow.nodes.length}`);
if (Object.keys(workflow.connections).length !== 17) throw new Error('expected 17 linear/branch connections');
const telegram = workflow.nodes.find((candidate) => candidate.name === 'Telegram发送风险报告（保持禁用）');
if (!telegram || telegram.disabled !== true) throw new Error('Telegram node must remain disabled');
if (workflow.nodes.some((candidate) => candidate.credentials)) throw new Error('workflow must not carry credential bindings');
const bridgeUrls = workflow.nodes.filter((candidate) => candidate.type === 'n8n-nodes-base.httpRequest').map((candidate) => candidate.parameters.url);
if (bridgeUrls.some((url) => !['http://127.0.0.1:38635/report', 'http://127.0.0.1:38636/protect'].includes(url))) throw new Error('workflow contains a non-loopback HTTP endpoint');

function codeNode(name) {
  const node = workflow.nodes.find((candidate) => candidate.name === name);
  if (!node || node.type !== 'n8n-nodes-base.code') {
    throw new Error(`missing code node: ${name}`);
  }
  return node;
}

function runNode(name, data) {
  const code = codeNode(name).parameters.jsCode;
  const result = new Function('$json', code)(data);
  if (!Array.isArray(result) || result.length !== 1 || !result[0].json) {
    throw new Error(`invalid output contract: ${name}`);
  }
  return result[0].json;
}

const chain = [
  '持仓缓存与过期标记',
  '核对实际持仓币种',
  '核对MA5 MA10 MA20 MA60',
  '核对局部Fib结构',
  '核对Grok新闻摘要',
  '汇总市场证据',
  '核对GPT风险分析',
  '独立止损安全校验',
  '止盈与Fib有效性校验',
  '生成中文风险报告',
  '报告字段与paper-only校验',
];

let data = {
  exitCode: 0,
  stdout: `${fixture.text}\n${JSON.stringify(fixture.metadata)}`,
  stderr: '',
};
for (const name of chain) data = runNode(name, data);

if (data.instrument !== 'ETHUSDT') throw new Error('instrument mismatch');
if (data.moving_averages.ma5 !== '1881.44') throw new Error('MA5 mismatch');
const spacedReport = fixture.text.replace(/MA(5|10|20|60)：/g, 'MA$1 ： ');
const spacedMA = runNode('核对MA5 MA10 MA20 MA60', { ...data, report: spacedReport });
if (spacedMA.moving_averages.ma5 !== '1881.44') throw new Error('MA whitespace parsing mismatch');
if (data.fib.fib_1272 !== '1831.9139') throw new Error('Fib 1.272 mismatch');
if (data.news_summary !== 'reused') throw new Error('news summary mismatch');
if (!data.stop_validation.independent) throw new Error('stop independence lost');
if (!data.paper_only_validated) throw new Error('paper-only validation missing');
if (data.metadata[0].decision_id !== 'offline-fixture') throw new Error('stdout metadata mismatch');

const envelopeData = runNode('提取动态TP SL保护信封', data);
if (envelopeData.protection_request.position_source !== 'realtime') throw new Error('realtime source missing');
if (!envelopeData.protection_request.candidates.ETHUSDT) throw new Error('Bybit candidate missing');

let blocked = false;
try {
  runNode('持仓缓存与过期标记', {
    exitCode: 0,
    stdout: fixture.text.replace('不执行交易', '允许执行交易'),
    stderr: '',
  });
} catch (error) {
  blocked = String(error.message).startsWith('BLOCKED:');
}
if (!blocked) throw new Error('unsafe report was not blocked');

let staleBlocked = false;
try {
  runNode('提取动态TP SL保护信封', {
    ...data,
    metadata: [{
      protection_request: {
        paper_only: true,
        trading_mode: 'demo',
        report_version: 'risk-report-v1',
        source: 'live_reporter',
        position_source: 'cache',
        candidates: {ETHUSDT: {stop_loss: 1968.7, take_profit: null}},
      },
    }],
  });
} catch (error) {
  staleBlocked = String(error.message).startsWith('BLOCKED:');
}
if (!staleBlocked) throw new Error('cached position was not blocked from protection');

console.log('PASS: Bybit Demo fixture traversed 11 report-validation nodes');
console.log('PASS: realtime protection envelope passed the bridge gate');
console.log('PASS: unsafe and cached execution inputs failed closed');
