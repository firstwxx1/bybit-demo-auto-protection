#!/usr/bin/env node
'use strict';

const fs = require('fs');
const workflow = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const fixture = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

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
  stdout: `${fixture.text}\n${JSON.stringify({decision_id: 'offline-fixture'})}`,
  stderr: '',
};
for (const name of chain) data = runNode(name, data);

if (data.instrument !== 'ETH-USDT-SWAP') throw new Error('instrument mismatch');
if (data.moving_averages.ma5 !== '1881.44') throw new Error('MA5 mismatch');
if (data.fib.fib_1272 !== '1831.9139') throw new Error('Fib 1.272 mismatch');
if (data.news_summary !== 'reused') throw new Error('news fallback mismatch');
if (!data.stop_validation.independent) throw new Error('stop independence lost');
if (!data.paper_only_validated) throw new Error('paper-only validation missing');
if (data.metadata[0].decision_id !== 'offline-fixture') throw new Error('stdout metadata mismatch');

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

console.log('PASS: historical stdout traversed 11 validation nodes');
console.log('PASS: missing paper-only statement failed closed');
