'use strict';

function activityChart(hours) {
  const counts = Array.from({length:24}, (_, hour) => Math.max(0, Number(hours.find(item => item.hour === hour)?.count) || 0));
  const peak = Math.max(...counts);
  const step = Math.max(1, Math.ceil(peak / 4));
  const ceiling = Math.max(1, Math.ceil(peak / step)) * step;
  const left = 42, top = 24, baseline = 184, height = baseline - top;
  const slot = 37.5, barWidth = 23;
  const number = value => Number(value.toFixed(3));
  const grid = [];
  for (let value = 0; value <= ceiling; value += step) {
    const y = number(baseline - value / ceiling * height);
    grid.push(`<g class="chart-grid"><line x1="${left}" y1="${y}" x2="942" y2="${y}"/><text x="30" y="${y + 4}" text-anchor="end">${value}</text></g>`);
  }
  const bars = counts.map((count, hour) => {
    const x = number(left + hour * slot + (slot - barWidth) / 2);
    const barHeight = number(count / ceiling * height);
    const y = number(baseline - barHeight);
    return `<g><title>${hour}:00–${hour}:59，${count} 条消息</title><rect data-hour="${hour}" data-count="${count}" x="${x}" y="${y}" width="${barWidth}" height="${barHeight}" rx="3" class="chart-bar${count && count === peak ? ' chart-peak' : ''}"${count ? ` tabindex="0" role="img" aria-label="${hour} 时，${count} 条消息"` : ''}/>${count ? `<text class="chart-count" x="${x + barWidth / 2}" y="${y - 7}" text-anchor="middle">${count}</text>` : ''}<line class="chart-tick" x1="${x + barWidth / 2}" y1="${baseline}" x2="${x + barWidth / 2}" y2="${baseline + 4}"/>${hour % 3 === 0 || hour === 23 ? `<text class="chart-hour" x="${x + barWidth / 2}" y="207" text-anchor="middle">${String(hour).padStart(2,'0')}:00</text>` : ''}</g>`;
  }).join('');
  const total = counts.reduce((sum, count) => sum + count, 0);
  const summary = peak ? `合计 ${total} 条消息 · 最高每小时 ${peak} 条` : '当前日期没有可用用户消息；请结合覆盖状态判断。';
  return `<figure class="activity-chart"><div class="chart-scroll"><svg viewBox="0 0 960 226" role="img" aria-label="北京时间每小时用户消息数量，合计 ${total} 条"><text class="chart-unit" x="${left}" y="13">消息数（条）</text>${grid.join('')}<line class="chart-baseline" x1="${left}" y1="${baseline}" x2="942" y2="${baseline}"/>${bars}</svg></div><figcaption>${summary}</figcaption></figure>`;
}
