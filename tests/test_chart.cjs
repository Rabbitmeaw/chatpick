const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync('web/charts.js', 'utf8'), context);

function bars(html) {
  return [...html.matchAll(/<rect ([^>]+)>/g)].map(match => Object.fromEntries(
    [...match[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(item => [item[1], item[2]])));
}

test('24小时共用基线，柱高精确反映数量，零值不冒充消息', () => {
  const html = context.activityChart([{hour:9,count:3},{hour:10,count:2},{hour:13,count:2},{hour:16,count:1}]);
  const result = bars(html);
  assert.equal(result.length, 24);
  assert.equal(result.reduce((sum, bar) => sum + Number(bar['data-count']), 0), 8);
  for (const bar of result) {
    assert.ok(Math.abs(Number(bar.y) + Number(bar.height) - 184) < 0.002);
    assert.ok(Math.abs(Number(bar.height) / 160 - Number(bar['data-count']) / 3) < 0.00001);
  }
  assert.equal(Number(result[0].height), 0);
  assert.equal(Number(result[9].height), 160);
  assert.match(html, /最高每小时 3 条/);
  const hourLabels = [...html.matchAll(/<text class="chart-hour"[^>]+y="([^"]+)"/g)];
  assert.equal(hourLabels.length, 9);
  assert.ok(hourLabels.every(label => label[1] === '207'));
});

test('空窗口没有假柱或无效数值，大数量保留同一线性比例', () => {
  const empty = context.activityChart([]);
  assert.ok(bars(empty).every(bar => Number(bar.height) === 0));
  assert.doesNotMatch(empty, /NaN|Infinity/);
  assert.match(empty, /没有可用用户消息/);
  const scaled = bars(context.activityChart([{hour:1,count:100},{hour:2,count:25}]));
  assert.equal(Number(scaled[1].height) / Number(scaled[2].height), 4);
});
