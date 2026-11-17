'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
let bootstrap;
let activePage = 'reports';
let reportSequence = 0;
let activitySequence = 0;
let scopeSequence = 0;
let scopeLoading = false;
let lastReport = null;
let parentReport = null;
let questionTurns = [];
let questionSequence = 0;
let questionConversation = '';
let questionBusy = false;
let historyBefore = '';
let botList = [];
let botSequence = 0;
let deliverySequence = 0;
let deliveryReportKey = '';
let deliveryPreview = null;
const isPeriodic = kind => kind === 'daily' || kind === 'weekly';

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {cache:'no-store'} : {
    method:'POST', headers:{'Content-Type':'application/json', 'X-GroupPulse':'offline'}, body:JSON.stringify(data)
  });
  const value = await response.json();
  if (!response.ok) { const error=new Error(value.error || '操作未完成，请重试。'); error.status=response.status; throw error; }
  return value;
}

function pendingReportJob() {
  try { return JSON.parse(sessionStorage.getItem('chatpick-report-job') || 'null'); }
  catch { return null; }
}

async function reportJob(source, request, progress) {
  const canonical=JSON.stringify(request,Object.keys(request).sort());
  const pending=pendingReportJob();
  const job=pending?.source === source && JSON.stringify(pending.request,Object.keys(pending.request).sort()) === canonical
    ? pending : {id:crypto.randomUUID().replaceAll('-',''),source,request};
  try { sessionStorage.setItem('chatpick-report-job',JSON.stringify(job)); } catch { /* 本次仍以同一任务轮询 */ }
  let state;
  try {
    state=await api('/api/report-jobs',job);
    while (state.status === 'running') {
      progress(`已等待 ${state.elapsed_seconds} 秒 · 后台正在读取或生成，模型单次等待上限 ${state.model_wait_seconds} 秒…`);
      await new Promise(resolve=>setTimeout(resolve,2000));
      state=await api('/api/report-job?'+new URLSearchParams({id:state.id,server:request.server,channel:request.channel}));
    }
  } catch (error) {
    if (error.status >= 400 && error.status < 500) {
      try { if (pendingReportJob()?.id === job.id) sessionStorage.removeItem('chatpick-report-job'); } catch { /* 不影响提示 */ }
      throw error;
    }
    throw new Error('进度连接暂时中断，后台任务可能仍在执行。刷新页面会继续查询同一任务，不会自动重新调用模型。');
  }
  try { if (pendingReportJob()?.id === job.id) sessionStorage.removeItem('chatpick-report-job'); } catch { /* 不影响结果 */ }
  if (state.status !== 'complete') throw new Error(state.error || '本次生成未完成，未自动重发模型请求。');
  return state.result;
}

async function resumeReportJob() {
  const job=pendingReportJob();
  if (!job?.request) return;
  const account=job.source === 'account';
  if (account ? !accountReportsVisible() || job.request.server !== $('exporter-server').value || job.request.channel !== $('exporter-channel').value
              : accountReportsVisible() || job.request.server !== bootstrap.selected_server || job.request.channel !== $('channel').value) return;
  const sequence=account ? ++importSequence : ++reportSequence;
  const current=()=>account ? sequence === importSequence && accountReportsVisible() : sequence === reportSequence;
  const target=account ? 'import-status' : 'global-status';
  const button=$(account ? 'import-generate' : 'generate-report');
  if (button) button.disabled=true;
  if (account) { importBusy=true; importControls(); }
  try {
    const report=await reportJob(job.source,job.request,text=>{ if (current()) message(target,'恢复原生成任务 · '+text); });
    if (!current()) return;
    if (account) { showImportReport(report); await loadImportHistory(sequence); }
    else { parentReport=null; lastReport=report; renderCachedReport(); }
    message(target,'原任务已完成并保存，没有重复生成。');
  } catch (error) { if (current()) message(target,error.message,true); }
  finally {
    if (account) { importBusy=false; importControls(); }
    if (button) button.disabled=false;
  }
}

function pendingQuestionJob() {
  try { return JSON.parse(sessionStorage.getItem('chatpick-question-job') || 'null'); }
  catch { return null; }
}

function questionScopeMatches(request, current=selectedDataScope()) {
  const defaults={source:'bot',account_id:'',server:'',channel:'',timezone:'Asia/Shanghai'};
  return Object.entries(defaults).every(([key,fallback]) => (request[key] || fallback)===(current[key] || fallback));
}

function detachQuestionJob() {
  const job=pendingQuestionJob();
  if (job && questionScopeMatches(job.request)) {
    try { sessionStorage.setItem('chatpick-question-job',JSON.stringify({...job,detached:true})); } catch { /* 旧回答仍保存到历史 */ }
  }
}

async function questionJob(request, progress) {
  const canonical=JSON.stringify(request,Object.keys(request).sort()), pending=pendingQuestionJob();
  const job=pending && !pending.detached && JSON.stringify(pending.request,Object.keys(pending.request).sort())===canonical
    ? pending : {id:crypto.randomUUID().replaceAll('-',''),request};
  try { sessionStorage.setItem('chatpick-question-job',JSON.stringify(job)); } catch { /* 本轮仍以同一任务查询 */ }
  const forget=()=>{
    try { if (pendingQuestionJob()?.id===job.id) sessionStorage.removeItem('chatpick-question-job'); } catch { /* 不影响回答 */ }
  };
  let state;
  try {
    state=await api('/api/question-jobs',{id:job.id,request:job.request});
    const scope=Object.fromEntries(Object.entries(request).filter(([key])=>['source','account_id','server','channel','timezone'].includes(key)));
    while (state.status==='running') {
      progress(`已等待 ${state.elapsed_seconds} 秒 · 正在读取记录并查找依据，模型单次等待上限 ${state.model_wait_seconds} 秒。`);
      await new Promise(resolve=>setTimeout(resolve,2000));
      state=await api('/api/question-job?'+new URLSearchParams({id:job.id,...scope}));
    }
  } catch (error) {
    if (error.status>=400 && error.status<500) { forget(); throw error; }
    throw new Error('进度连接暂时中断，原问题可能仍在后台生成。刷新页面会继续查询同一任务，不会自动重新调用模型。');
  }
  forget();
  if (state.status!=='complete') throw new Error(state.error || '本次问答未完成，未自动重发模型请求。');
  return state.result;
}

function discordCopyTools() {
  return '<div class="report-copy-tools"><label class="check-label"><input type="checkbox" data-copy-sources>附出处链接</label><button type="button" data-copy-discord>复制到 Discord</button><span class="hint" role="status" aria-live="polite" data-copy-status></span><button type="button" data-send-report hidden>发送到频道</button><span class="hint" role="status" aria-live="polite" data-send-status></span></div>';
}

async function copyReportToDiscord(report, button) {
  if (button.disabled) return;
  const tools=button.closest('.report-copy-tools'), status=tools.querySelector('[data-copy-status]');
  const option=tools.querySelector('[data-copy-sources]');
  button.disabled=true;
  if (option) option.disabled=true;
  try {
    const parts=discordReportParts(report,!!option?.checked), index=Number(button.dataset.copyPart || 0)%parts.length;
    if (typeof window !== 'undefined' && typeof window.chatpickCopyText === 'function') await window.chatpickCopyText(parts[index]);
    else await navigator.clipboard.writeText(parts[index]);
    const next=(index+1)%parts.length;
    button.dataset.copyPart=String(next);
    button.textContent=parts.length>1 && next ? `复制第 ${next+1}/${parts.length} 段` : '复制到 Discord';
    status.textContent=parts.length===1 ? '已复制，可直接粘贴到 Discord。' :
      `已复制第 ${index+1}/${parts.length} 段${next ? '，粘贴后继续复制下一段。' : '，全部复制完成。'}`;
  } catch {
    status.textContent='未能复制，请允许此页面访问剪贴板后再点一次。';
  } finally { button.disabled=false; if (option) option.disabled=false; }
}

function resetDiscordCopyOptions(option) {
  const tools=option.closest('.report-copy-tools'), button=tools.querySelector('[data-copy-discord]');
  button.dataset.copyPart='0';
  button.textContent='复制到 Discord';
  tools.querySelector('[data-copy-status]').textContent='';
}

function message(target, text, error=false) {
  const node = $(target);
  node.hidden = false;
  node.className = `message${error ? ' error' : ''}`;
  node.textContent = text;
}

async function task(button, target, work) {
  if (button.disabled) return;
  button.disabled = true;
  try { await work(); }
  catch (error) { message(target, error.message || '无法连接本机服务，请重新双击启动拾聊。', true); }
  finally { button.disabled = false; }
}

function options(id, entries) {
  if ($(id).tagName !== 'SELECT') return;
  $(id).innerHTML = Object.entries(entries).map(([value, label]) => `<option value="${escapeHTML(value)}">${escapeHTML(label)}</option>`).join('');
}

function timeLabel(created, zone='Asia/Shanghai') {
  return new Intl.DateTimeFormat('zh-CN', {timeZone:zone, month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false}).format(new Date(created));
}

function fullTimeLabel(created, zone='Asia/Shanghai') {
  if (!created || !Number.isFinite(new Date(created).getTime())) return '时间未记录';
  return new Intl.DateTimeFormat('zh-CN', {timeZone:zone, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}).format(new Date(created)) + `（${zone === 'Asia/Shanghai' ? '北京时间' : zone === 'America/Los_Angeles' ? '洛杉矶时间' : 'UTC'}）`;
}

function discordSourceLink(message) {
  const url=discordSourceURL(message);
  return url ? `<a href="${url}" target="_blank" rel="noopener noreferrer">在 Discord 查看 ↗</a>` : '';
}

function avatarHTML(message) {
  const initial=escapeHTML(Array.from(message.name || '人')[0]),url=message.avatar_url;
  const image=typeof url==='string' && /^https:\/\/(?:cdn\.discordapp\.com|media\.discordapp\.net)\//.test(url) ?
    `<img src="${escapeHTML(url)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : '';
  return `<span class="member-avatar" aria-hidden="true"><span>${initial}</span>${image}</span>`;
}

function windowSpan(window) {
  if (window.precision !== 'minute') return window.days+' 天';
  const minutes=Math.round((Date.parse(window.end)-Date.parse(window.start))/60000), hours=Math.floor(minutes/60), rest=minutes%60;
  return hours ? `${hours} 小时${rest ? ' '+rest+' 分钟' : ''}` : `${minutes} 分钟`;
}

function metricsHTML(report) {
  const m = report.metrics;
  const rawActivity = !report.offline && report.kind === 'activity';
  const analyzed = rawActivity && report.topic_analysis?.status !== 'not_generated';
  return `<div class="metrics">
    <div class="metric"><small>原始用户消息</small><strong class="number">${m.messages}</strong><small>不含 ${m.bots} 条 Bot 消息${m.system ? `、${m.system} 条系统消息` : ''}和补读背景</small></div>
    <div class="metric"><small>发言人数</small><strong class="number">${m.members}</strong><small>按成员身份去重，不是在线人数</small></div>
    <div class="metric"><small>${rawActivity ? '对照话题' : '讨论话题'}</small><strong class="number">${rawActivity && !analyzed ? '—' : m.topics ?? report.trends.filter(t => t.current > 0).length}</strong><small>${report.offline ? '根据合成样例话题归类' : rawActivity ? analyzed ? '两期联合整理，可查看原文' : '点击分析后显示，统计不调用模型' : '模型识别，仅本次范围'}</small></div>
    <div class="metric"><small>回复消息</small><strong class="number">${m.replies}</strong><small>包含原文暂缺的引用</small></div>
  </div>`;
}

function coverageHTML(report) {
  const c = report.coverage;
  const status = report.source?.kind === 'discord-export' ? (report.source.method === 'account-export' ? '本次读取 · 仅所选频道' : '导入记录 · 非服务器全量') : c.up_to_date && !c.missing ? '已同步至本次读取时刻' : c.status === 'complete' ? (report.offline ? '合成窗口完整' : '所选正文窗口已同步') : c.status === 'partial' ? '存在缺失，阅读时留意' : '窗口覆盖未知';
  return `<section class="panel coverage"><h2>数据覆盖</h2><span class="status">${status}</span><p>${escapeHTML(c.note)}</p><hr class="rule-line"><p><strong>本次范围</strong><br>${escapeHTML(report.channel_name || bootstrap.channels[report.channel])}<br>${escapeHTML(report.window.label)}<br><small>${report.window.timezone === 'America/Los_Angeles' ? '洛杉矶时间' : report.window.timezone === 'UTC' ? 'UTC' : '北京时间'} · ${report.window.precision === 'minute' ? windowSpan(report.window)+' · 含开始、不含结束' : '包含起止当天，按所选范围统计'}</small></p><p class="hint">${report.offline ? '所有计数只反映合成数据。' : report.source ? '只统计这份记录中的消息。' : '只统计本地已同步的频道消息。'}缺少覆盖记录的日期不能据此判断群内没有发言。</p></section>`;
}

function quoteHTML(m, zone) {
  zone = typeof zone === 'string' ? zone : 'Asia/Shanghai';
  return `<div class="quote"><div class="quote-meta"><strong>${escapeHTML(m.name)}</strong><span class="stance ${m.stance === '决定' ? 'decision' : ''}">${escapeHTML(m.stance)}</span><span>${timeLabel(m.created,zone)}</span></div><p>${escapeHTML(m.content)}</p><button type="button" class="evidence-link" data-source="${escapeHTML(m.id)}">查看原文${m.reference_id ? '与引用' : ''} ↗</button></div>`;
}

function sourcesHTML(report, prefix='') {
  const sources = new Map(report.messages.map(m => [m.id, {message:m, context:false}]));
  for (const c of report.contexts) {
    if (c.status === 'available' && !sources.has(c.id)) sources.set(c.id, {message:c.message, context:true});
  }
  const items = [...sources.values()].map(({message:m, context}) => `<article class="source-message" id="${prefix}source-${escapeHTML(m.id)}">${avatarHTML(m)}<strong>${escapeHTML(m.name)}</strong> <span class="stance">${context ? '引用背景 · 不归为目标人物观点' : '范围内原文'}</span><small>消息 ${escapeHTML(m.id)} · 成员 ${escapeHTML(m.author_id)} · ${fullTimeLabel(m.created,report.window.timezone)} · ${escapeHTML(m.thread)}</small>${discordSourceLink(m)}<p>${escapeHTML(m.content)}</p>${m.reference_id ? `<small>引用目标：${escapeHTML(m.reference_id)}</small>` : ''}</article>`).join('');
  const missing = report.contexts.filter(c => c.status !== 'available').map(c => `<article class="source-message missing"><strong>引用 ${escapeHTML(c.id)} 未补齐</strong><p>${escapeHTML(c.reason)}</p></article>`).join('');
  const note=report.generation?.evidence_normalizations?.some(item=>item.reason === 'output-metadata') ? '<p class="hint">摘要正文已保留；未能核对的出处不附链接，署名按可核对原文显示。</p>' : '';
  return `<details id="${prefix}sources"><summary>查看原始消息与引用背景 <span class="hint">${sources.size} 条可读原文</span></summary>${note}${items}${missing}</details>`;
}

function sourceButtons(ids, label='查看出处') {
  return ids.map((id,index) => `<button type="button" class="evidence-link" data-source="${escapeHTML(id)}">${label}${ids.length>1 ? ' '+(index+1) : ''} ↗</button>`).join(' ');
}

function dimensionsHTML(report) {
  const summaries = Object.values(report.dimensions);
  if (!summaries.length) return '';
  return `<section class="report-dimensions"><h2>关注维度</h2><p class="hint">以下摘要与主报告在同一范围生成，全频道正文与统计保持完整。</p><div class="dimension-cards">${summaries.map(summary => `<article class="dimension-card"><span class="eyebrow">${summary.kind === 'member' ? '成员发言摘要' : '多人讨论摘要'}</span><h3>${escapeHTML(summary.subject.name)}</h3>${summary.generation.status === 'failed' ? `<p class="message error"><strong>摘要未生成</strong><br>${escapeHTML(summary.generation.message)}</p>` : `<p class="hint">${summary.selected_count} 条发言 · ${summary.digest.summaryStats.activeUsers} 位成员 · ${summary.digest.summaryStats.topicCount} 个话题</p>${summary.digest.topics.slice(0, 2).map(topic => `<p class="dimension-excerpt">${escapeHTML(topic.summary)} ${sourceButtons(topic.summarySourceMessageIds)}</p>`).join('') || '<p class="hint">本范围内没有该维度的发言。</p>'}<button type="button" class="evidence-link extract-link" data-extract-summary="${summary.kind}">单独查看${summary.kind === 'member' ? '成员' : '讨论'}摘要 →</button>`}</article>`).join('')}</div></section>`;
}

function serverOverviewHTML(digest) {
  const text=digest.hero?.summary || digest.overview;
  return text ? `<section class="digest-hero server-overview"><h2>${digest.overviewStyle === 'topic-index' ? '本期速览' : '今日速览'}</h2><p>${escapeHTML(text)}</p>${sourceButtons(digest.hero?.summarySourceMessageIds || digest.overviewSourceMessageIds)}</section>` : '';
}

function topicBodyHTML(topic, report) {
  if (topic.timeline?.length) return `<div class="topic-timeline">${topic.timeline.map(entry=>`<article class="topic-moment"><small>${escapeHTML(entry.timeRange)}</small><h4>${escapeHTML(entry.title)}</h4>${topicBodyHTML(entry,report)}</article>`).join('')}</div>`;
  const review=topic.review?.confidence === 'low' ? `<p class="topic-review"><strong>置信度较低</strong> · ${escapeHTML((topic.review.reasons || []).join(' '))}</p>` : '';
  return `<p class="topic-summary">${escapeHTML(topic.summary)}</p>${review}<div>${sourceButtons(topic.summarySourceMessageIds,'摘要原文')}</div><details class="topic-evidence"><summary>展开 ${topic.messageCount} 条原文${review ? '核对' : ''}</summary>${topic.messages.map(m=>quoteHTML(m,report.window.timezone)).join('')}</details>`;
}

function summaryHTML(report, backReport=parentReport, sourcePrefix='') {
  const member = report.kind === 'member';
  const digest = report.digest;
  const stats = digest.summaryStats;
  const groups = report.report_profile && serverReportSections(report);
  let content;
  if (member) {
    const topicHTML = topic => `<article class="summary-topic"><div class="section-head"><h3>${escapeHTML(topic.title)}</h3><small>${topic.messageCount} 条 · ${escapeHTML(topic.timeRange)}</small></div>${topicBodyHTML(topic,report)}</article>`;
    content = groups ? serverOverviewHTML(digest)+groups.map(section=>`<section class="server-report-group"><h2>${section.title}</h2>${section.topics.map(topicHTML).join('')}</section>`).join('') :
      `<section><h2>聊了什么</h2>${digest.topics.map(topicHTML).join('') || `<p>${escapeHTML(digest.overview || '')}</p>`}</section>`;
  } else {
    const people = new Map();
    for (const item of report.messages) {
      if (!people.has(item.author_id)) people.set(item.author_id, []);
      people.get(item.author_id).push(item);
    }
    content = `<section><h2>讨论摘要</h2>${digest.topics.map(topic => `<p>${escapeHTML(topic.summary)} ${sourceButtons(topic.summarySourceMessageIds)}</p>`).join('')}</section><section><h2>各方发言</h2><p class="hint">按成员身份分别整理，摘录保留原文；不据此推断共识。</p><div class="viewpoint-cards">${[...people.values()].map(items => `<article class="viewpoint-card"><div class="section-head"><h3>${escapeHTML(items[items.length - 1].name)}</h3><small>${items.length} 条发言</small></div>${quoteHTML(items[items.length - 1])}<details class="topic-evidence"><summary>展开此人的全部 ${items.length} 条发言</summary>${items.map(quoteHTML).join('')}</details></article>`).join('')}</div></section>`;
    const opposition = report.messages.filter(item => item.stance === '反对');
    content += `<section class="summary-section"><h2>不同意见 · 原文明示</h2>${opposition.map(quoteHTML).join('') || '<p class="hint">未识别到明确反对发言，不代表所有人意见一致。</p>'}</section>`;
  }
  const decisions = groups ? [] : digest.topics.flatMap(topic => topic.conclusions);
  const todos = digest.todos.map(item => `<p>${escapeHTML(item.task)} ${sourceButtons(item.sourceMessageIds)}<br><small>负责人：${escapeHTML(item.owner || '未明确')} · 期限：${escapeHTML(item.deadline || '未明确')}</small></p>`).join('');
  const pending = digest.unresolved.map(item => `<p>${escapeHTML(item.asker)}：${escapeHTML(item.question)} ${sourceButtons(item.sourceMessageIds)}</p>`).join('');
  const hidden = (member ? ['topics', 'todos', 'unresolved'] : ['todos', 'unresolved']).reduce((sum, key) => sum + digest.sectionMeta[key].hiddenCount, 0);
  const extra=decisions.map(item=>`<p>${member ? '' : escapeHTML(item.sender)+'：'}${escapeHTML(item.text)} ${sourceButtons(item.sourceMessageIds)}</p>`).join('');
  const supplements=extra || todos || pending ? `<details class="summary-section"><summary>补充观点${todos || pending ? '与明确约定' : ''}</summary>${extra}${todos ? `<h3>明确约定</h3>${todos}` : ''}${pending ? `<h3>尚未答复的问题</h3>${pending}` : ''}</details>` : '';
  return `<div class="report-layout summary-layout"><article class="panel summary-report" data-summary-kind="${report.kind}">${backReport ? `<button type="button" class="evidence-link back-report" data-back-report>← 返回${backReport.kind === 'weekly' ? '周报' : '日报'}</button>` : ''}<div class="summary-intro"><span class="summary-symbol" aria-hidden="true">${member ? '人' : '谈'}</span><div><span class="eyebrow">${member ? '成员发言摘要' : '多人讨论摘要'} · ${report.window.precision === 'minute' ? '指定周期' : report.window.days === 1 ? '单日' : '所选区间'}</span><h2>${escapeHTML(report.subject.name)}</h2><p class="hint">${escapeHTML(report.window.label)} · ${report.window.timezone === 'America/Los_Angeles' ? '洛杉矶时间' : report.window.timezone === 'UTC' ? 'UTC' : '北京时间'} · ${digest.mode === 'compact' ? '精简阅读' : '完整阅读'}</p></div></div><p class="digest-disclaimer">${report.offline ? '模拟模型 · 全程离线 · 摘要基于合成原文。' : report.source ? '真实模型 · 基于这份频道记录。' : '真实模型 · 基于已同步的频道原文。'}${member ? '仅整理此人的发言，他人引用另作背景。' : '按人保留发言与不同意见，不推断共识。'}</p><div class="summary-stats"><span><strong>${stats.messageCount}</strong> 条发言</span><span><strong>${stats.activeUsers}</strong> 位成员</span><span><strong>${stats.topicCount}</strong> 个话题</span></div>${report.selected_count ? content : '<div class="empty"><strong>这个范围内没有相关发言</strong>试试其他日期、成员或话题。</div>'}${supplements}${hidden ? `<div class="hidden-notice">当前模式未展示 ${hidden} 个候选条目。${digest.mode === 'compact' ? '<button type="button" class="evidence-link" data-full-report>查看完整报告 →</button>' : '原始发言可在下方查看。'}</div>` : ''}${sourcesHTML(report,sourcePrefix)}</article><aside class="report-side">${coverageHTML(report)}</aside></div>`;
}

function reportHTML(report, sourcePrefix='', layout=currentLayout()) {
  if (!isPeriodic(report.kind)) return summaryHTML(report,parentReport,sourcePrefix);
  const names = {daily:'日报', weekly:'周报'};
  const digest = report.digest;
  const stats = digest.summaryStats;
  const groups = report.report_profile && serverReportSections(report);
  const topicHTML = (topic, index) => `<section class="digest-topic">
    <div class="topic-card-head"><h3>${groups ? '' : `<span class="topic-number">${index + 1}</span>`}${escapeHTML(topic.title)}</h3><span class="badge">${topic.messageCount} 条 · ${topic.participantIds.length} 人</span></div>
    <p class="topic-meta">${escapeHTML(topic.timeRange)} <span>话题热度：${topic.heat}</span></p>
    ${topicBodyHTML(topic,report)}
    <div class="participants"><span>参与者</span>${topic.participants.map(name => `<span class="person-tag">${escapeHTML(name)}</span>`).join('')}</div>
    ${!groups && topic.conclusions.length ? `<div class="conclusions"><strong>主要结论 · 附原文</strong>${topic.conclusions.map(item => `<p>${escapeHTML(item.sender)}：${escapeHTML(item.text)} ${sourceButtons(item.sourceMessageIds)}</p>`).join('')}</div>` : ''}
  </section>`;
  const topics = digest.topics.map(topicHTML).join('');
  const empty = `<div class="empty"><strong>这个范围内没有可展示的发言</strong>${report.offline ? '试试样例日期 2026-10-21，或切换频道、成员和话题。' : '请检查同步日期、数据覆盖或切换频道。'}<p>覆盖未知时，“没有记录”不等于“没有发言”。</p></div>`;
  const todos = digest.todos.map(item => `<article class="action-item"><span class="action-symbol" aria-hidden="true">□</span><div><p>${escapeHTML(item.task)}</p><small>负责人：${escapeHTML(item.owner || '未明确')} · 期限：${escapeHTML(item.deadline || '未明确')}</small><div>${sourceButtons(item.sourceMessageIds)}</div></div></article>`).join('');
  const pending = digest.unresolved.map(item => `<article class="action-item"><span class="action-symbol" aria-hidden="true">?</span><div><p>${escapeHTML(item.question)}</p><small>${escapeHTML(item.asker)} · ${escapeHTML(item.lastDiscussedAt)} · 后续解决状态未判定</small><div>${sourceButtons(item.sourceMessageIds)}</div></div></article>`).join('');
  const important = digest.importantMessages.map(item => `<article class="important-item"><div class="quote-meta"><strong>${escapeHTML(item.sender)}</strong><span>${escapeHTML(item.time)}</span></div><p>${escapeHTML(item.content)}</p>${sourceButtons(item.sourceMessageIds)}</article>`).join('');
  const hidden = (groups ? ['topics','todos','unresolved'] : ['topics','importantMessages','todos','unresolved']).reduce((sum, key) => sum + (digest.sectionMeta[key]?.hiddenCount || 0), 0);
  const highlights = report.offline ? report.trends.filter(t => t.current) : report.fullDigest.topics.map(topic => ({title:topic.title,current:topic.messageCount,state:'本次报告'}));
  const sections = {
    hero: `<section class="digest-hero"><div class="eyebrow">${report.kind === 'weekly' ? '本期总览' : '本次总览'}</div><h3>${escapeHTML(digest.hero.headline)}</h3><p>${escapeHTML(digest.hero.summary)}</p><div>${sourceButtons(digest.hero.summarySourceMessageIds)}</div><div class="digest-kpis"><div><strong>${stats.messageCount}</strong><small>选中消息</small></div><div><strong>${stats.activeUsers}</strong><small>参与成员</small></div><div><strong>${layout.style === 'quickread' ? windowSpan(report.window) : stats.topicCount}</strong><small>${layout.style === 'quickread' ? '统计跨度' : '讨论话题'}</small></div><div class="fourth-kpi"><strong>${report.metrics.replies}</strong><small>回复互动</small></div></div><div class="hero-status">${escapeHTML(digest.hero.statusLine)}</div>${digest.hero.keyTakeaway ? `<div class="hero-takeaway"><strong>值得一看 · 附原文</strong><p>${escapeHTML(digest.hero.keyTakeaway)} ${sourceButtons(digest.hero.keyTakeawaySourceMessageIds)}</p></div>` : ''}</section>`,
    topics: `<section class="topics-section"><div class="section-head topic-section-heading"><h2>讨论话题</h2><small>共 ${stats.topicCount} 个 · 当前展示 ${digest.topics.length} 个</small></div><div class="topics-grid">${topics || empty}</div></section>`,
    actions: todos || pending ? `<details class="action-section"><summary>明确约定与尚未答复的问题</summary><div class="action-columns">${todos ? `<div><h3>明确约定</h3>${todos}</div>` : ''}${pending ? `<div><h3>尚未答复的问题</h3>${pending}</div>` : ''}</div></details>` : '',
    important: important ? `<section class="important-section"><h2>重要消息 · 明确通知</h2>${important}</section>` : ''
  };
  if (groups) {
    sections.hero = serverOverviewHTML(digest);
    sections.topics = `<section class="topics-section">${groups.map(section=>`<section class="server-report-group"><div class="section-head topic-section-heading"><h2>${section.title}</h2><small>${section.topics.length} 个话题</small></div><div class="topics-grid">${section.topics.map(topic=>topicHTML(topic,digest.topics.indexOf(topic))).join('')}</div></section>`).join('') || empty}</section>`;
    sections.important = '';
  }
  return `${metricsHTML(report)}<div class="report-layout reading-${layout.platform}"><article class="panel digest-report layout-${layout.style}${groups ? ' server-digest' : ''}" data-layout="${escapeHTML(layout.id)}">
    <div class="section-head"><h2>${names[report.kind]} · ${report.offline ? '模拟生成' : '模型生成'}</h2><small>${escapeHTML(report.window.label)}<br>${escapeHTML(layout.name)} · ${digest.mode === 'compact' ? '精简阅读' : '完整阅读'} · ${report.selected_count} 条</small></div>
    <p class="digest-disclaimer">${report.offline ? '模拟模型 · 全程离线 · 摘要基于合成原文，未连接真实模型。' : '真实模型 · ' + escapeHTML(report.generation.note || '请对照原文检查；不向Discord发送消息。')}</p>
    <div class="digest-sections${important && !groups ? ' has-important' : ''}">${(groups ? ['hero','topics','actions'] : layout.order).map(key => sections[key]).join('')}</div>
    ${dimensionsHTML(report)}
    ${hidden ? `<div class="hidden-notice">当前模式未展示 ${hidden} 个候选条目，原始消息未删减。${digest.mode === 'compact' ? '<button type="button" class="evidence-link" data-full-report>查看完整报告 →</button>' : '原始发言可在下方查看。'}</div>` : ''}
    ${sourcesHTML(report,sourcePrefix)}</article><aside class="report-side">${coverageHTML(report)}<section class="panel"><h2>话题一瞥</h2>${highlights.map(t => `<div class="mini-trend"><span>${escapeHTML(t.title)}<small>${escapeHTML(t.state)}</small></span><strong>${t.current} <small>条消息</small></strong></div>`).join('') || '<p class="hint">暂无话题记录。</p>'}<hr class="rule-line"><p class="hint">上方统计卡片反映整个频道窗口；当前摘录按筛选范围展示。精简模式只减少展示条目，不减少取数。</p></section></aside></div>`;
}

function currentLayout() {
  return bootstrap.layouts.find(layout => layout.id === $('report-layout').value) || bootstrap.layouts[0];
}

function renderCachedReport() {
  if (lastReport) $('report-output').innerHTML = discordCopyTools()+reportHTML(lastReport);
  if (lastReport && isLive()) bindRealDelivery(lastReport,$('report-output'));
  $('layout-help').textContent = currentLayout().description + ' 切换使用同一份报告，不重新分析。';
  $('report-delivery').hidden = !lastReport || isLive();
  const key = lastReport ? lastReport.report_id + '/' + lastReport.kind : '';
  if (key !== deliveryReportKey) {
    deliveryReportKey = key;
    clearDelivery();
    if (!isLive()) loadBots().catch(error => message('delivery-status', error.message, true));
  }
}

function setReadingMode(report, mode) {
  if (!report.fullDigest) return;
  if (report.presentations) {
    report.digest = report.presentations[mode];
    for (const dimension of Object.values(report.dimensions || {})) setReadingMode(dimension, mode);
    return;
  }
  const digest = {...report.fullDigest, mode, sectionMeta:{}};
  for (const [key, limit] of Object.entries({topics:3, importantMessages:3, todos:4, unresolved:3})) {
    const items = report.fullDigest[key];
    digest[key] = mode === 'compact' ? items.slice(0, limit) : items;
    digest.sectionMeta[key] = {enabled:items.length > 0, totalCount:items.length,
      displayedCount:digest[key].length, hiddenCount:items.length - digest[key].length};
  }
  report.digest = digest;
  for (const dimension of Object.values(report.dimensions || {})) setReadingMode(dimension, mode);
}

$('report-mode').addEventListener('change', () => {
  const mode = $('report-mode').value;
  if (lastReport) setReadingMode(lastReport, mode);
  if (parentReport) setReadingMode(parentReport, mode);
  renderCachedReport();
});

function initLayouts() {
  const layouts = bootstrap.layouts;
  $('report-layout').innerHTML = ['mobile','desktop'].map(platform => `<optgroup label="${platform === 'mobile' ? '手机阅读' : '电脑阅读'}">${layouts.filter(l => l.platform === platform).map(l => `<option value="${escapeHTML(l.id)}">${escapeHTML(l.name)}</option>`).join('')}</optgroup>`).join('');
  let stored;
  try { stored = localStorage.getItem('grouppulse-layout'); } catch { /* 禁用本地存储时使用默认项。 */ }
  $('report-layout').value = layouts.some(l => l.id === stored) ? stored : bootstrap.default_layout;
}

$('report-layout').addEventListener('change', () => {
  renderCachedReport();
  try { localStorage.setItem('grouppulse-layout', currentLayout().id); } catch { /* 不影响本次阅读。 */ }
});

async function loadReport() {
  if (scopeLoading) throw new Error('请等待服务器和频道切换完成。');
  if (isLive() && ['today','yesterday','last24','last3','last7'].includes($('range-preset').value)) applyRangePreset();
  const sequence = ++reportSequence;
  const query = new URLSearchParams(new FormData($('report-form')));
  query.set('server', bootstrap.selected_server);
  query.set('channel', $('channel').value);
  let report;
  if (isLive()) message('global-status','正在补齐所选日期的记录并生成报告…');
  try { report = isLive() ? await reportJob('bot',Object.fromEntries(query),text=>{ if (sequence === reportSequence) message('global-status',text); }) : await api('/api/report?' + query); }
  catch (error) {
    if (sequence !== reportSequence) return;
    clearReport();
    $('report-output').innerHTML = '<div class="empty"><strong>本次报告未生成</strong>请按上方提示重新生成。</div>';
    throw error;
  }
  if (sequence !== reportSequence) return;
  parentReport = null;
  lastReport = report;
  renderCachedReport();
  if (isLive()) message('global-status', `${report.generation.calls || 0}次模型调用已完成。请对照原文检查内容；没有向Discord发送消息。`);
}

function activityHTML(report) {
  if (!report.offline) {
    const prior=report.comparison, analysis=report.topic_analysis, m=report.metrics;
    const delta=!m.comparable ? '两期尚未完整，暂不判断增减' : prior.metrics.messages===0 ? `上期 0 条 → 本期 ${m.messages} 条` : `${m.messages-prior.metrics.messages>=0 ? '增加' : '减少'} ${Math.abs(m.messages-prior.metrics.messages)} 条（${Math.abs((m.messages/prior.metrics.messages-1)*100).toFixed(1)}%）`;
    const sources=new Map((analysis.messages || []).map(row=>[row.id,row]));
    const topics=(analysis.topics || []).map(topic=>`<tr><td><strong>${escapeHTML(topic.title)}</strong><details class="topic-evidence"><summary>查看 ${topic.sourceMessageIds.length} 条原文</summary>${topic.sourceMessageIds.map(id=>sourceCard(sources.get(id),report.window.start)).join('')}</details></td><td>${topic.current}<small>${topic.members} 人 · ${(topic.share*100).toFixed(1)}%</small></td><td>${topic.previous}<small>${topic.previous_members} 人 · ${(topic.previous_share*100).toFixed(1)}%</small></td><td>${escapeHTML(topic.state)}</td></tr>`).join('');
    const daily=report.daily.map(day=>`<tr><td>${escapeHTML(day.date)}</td><td>${day.period==='current' ? '本期' : '上期'}</td><td>${day.messages}</td><td>${day.members}</td><td>${day.replies}</td></tr>`).join('');
    return `${metricsHTML(report)}<section class="panel activity-comparison"><div class="section-head"><h2>与上一等长周期对照</h2><span class="badge">${report.window.days} 天对 ${prior.window.days} 天</span></div><div class="table-wrap"><table><thead><tr><th>周期</th><th>消息</th><th>发言人数</th><th>回复</th><th>覆盖</th></tr></thead><tbody><tr><td>本期<small>${escapeHTML(report.window.label)}</small></td><td>${m.messages}</td><td>${m.members}</td><td>${m.replies}</td><td>${report.coverage.window_complete ? '完整' : '未结束或有缺失'}</td></tr><tr><td>上期<small>${escapeHTML(prior.window.label)}</small></td><td>${prior.metrics.messages}</td><td>${prior.metrics.members}</td><td>${prior.metrics.replies}</td><td>${prior.coverage.window_complete ? '完整' : '有缺失'}</td></tr></tbody></table></div><p class="hint">${delta}。排除 Bot、系统消息及补读背景，原始消息只计一次。</p><details><summary>逐日明细</summary><p class="hint">仅列有本地用户消息的日期，缺失日期需结合覆盖判断。</p><div class="table-wrap"><table><thead><tr><th>日期</th><th>周期</th><th>消息</th><th>人数</th><th>回复</th></tr></thead><tbody>${daily || '<tr><td colspan="5">所选范围暂无用户消息</td></tr>'}</tbody></table></div></details><details><summary>上期覆盖说明</summary><p class="hint">${escapeHTML(prior.coverage.note)}</p></details></section><section class="panel"><h2>一天中的活跃时段</h2><p class="hint">北京时间 · 汇总本期各小时 · 不调用模型</p>${activityChart(m.hours)}</section><section class="panel"><div class="section-head"><h2>话题跨期对照</h2><button type="button" data-topic-trends="${analysis.status==='not_generated' ? 'new' : 'refresh'}">${analysis.status==='not_generated' ? '分析话题趋势' : '重新分析话题'}</button></div>${analysis.status==='not_generated' ? `<p class="hint">两期共 ${m.messages+prior.metrics.messages} 条用户消息。点击后由模型整理话题，并按需逐条补分类，可能产生费用；普通统计和重复查看缓存不调用模型。</p>` : `<p class="hint">已归类 ${analysis.classified_messages} 条（其中其他 ${analysis.other_messages} 条） · 未完成归类 ${analysis.unclassified_messages} 条 · ${analysis.cached ? '缓存结果，原分析' : '本次生成'} ${analysis.generation.calls} 次模型请求</p><div class="table-wrap"><table><thead><tr><th>话题与原文</th><th>本期</th><th>上期</th><th>变化</th></tr></thead><tbody>${topics || '<tr><td colspan="4">未识别到可对照的话题</td></tr>'}</tbody></table></div><details><summary>统计与关联说明</summary><p class="hint">${escapeHTML(analysis.note)} 消息增加至少5条且增幅达到25%才标升温，减少同理；“本期出现”仅指上期没有，并非历史首次。改名按来源关联，重名不直接合并。</p></details>`}</section>${coverageHTML(report)}`;
  }
  const rows = report.trends.map(t => `<tr><td>${escapeHTML(t.title)}</td><td>${t.current}</td><td>${t.previous}</td><td>${t.members}</td><td>${escapeHTML(t.state)}</td></tr>`).join('');
  return `${metricsHTML(report)}<section class="panel"><h2>一天中的活跃时段</h2><p class="hint">北京时间 · 每条原始消息只计一次 · 将鼠标移到柱上查看数量</p>${activityChart(report.metrics.hours)}</section><section class="panel"><div class="section-head"><h2>话题跨期对照</h2><span class="badge">与前一自然日比较</span></div><p class="hint">使用样例自带的稳定话题标识。消息增加至少 5 条且增幅达 25% 才标升温，减少同理；上期为 0 显示“本期出现”。</p><div class="table-wrap"><table><thead><tr><th>话题</th><th>本期消息</th><th>上期消息</th><th>本期人数</th><th>变化</th></tr></thead><tbody>${rows || '<tr><td colspan="5">暂无话题记录</td></tr>'}</tbody></table></div></section>${coverageHTML(report)}`;
}

async function loadActivity() {
  if (scopeLoading) return;
  const key = dataScopeKey();
  const scope = scopeSequence, sequence=++activitySequence;
  const report = await api('/api/report?' + new URLSearchParams({...activityParams(),kind:'activity'}));
  if (scope !== scopeSequence || sequence!==activitySequence || key !== dataScopeKey()) return;
  $('activity-output').innerHTML = activityHTML(report);
}

function activityParams() {
  return {...selectedDataScope(),date:$('activity-date').value,
    end_date:isLive() && $('activity-range').value!=='day' ? $('activity-end').value : ''};
}

function sourceCard(row,boundary='') {
  if (!row) return '<p class="hint">原文不在本次快照中。</p>';
  return `<article class="source-message">${avatarHTML(row)}<strong>${escapeHTML(row.name)}</strong><small>${boundary ? row.created>=boundary ? '本期 · ' : '上期 · ' : ''}${fullTimeLabel(row.created)} · 成员 ${escapeHTML(row.author_id)} · 消息 ${escapeHTML(row.id)}</small><p>${escapeHTML(row.content)}</p>${discordSourceLink(row)}${row.reference_id ? `<small>回复消息：${escapeHTML(row.reference_id)}</small>` : ''}</article>`;
}

$('activity-output').addEventListener('click',event=>{
  const button=event.target.closest('[data-topic-trends]');
  if (!button || scopeLoading) return;
  const request=activityParams(), scope=scopeSequence, sequence=++activitySequence;
  task(button,'global-status',async()=>{
    message('global-status','正在整理两期话题并补齐分类，请稍候…');
    const report=await api('/api/topic-trends',{...request,refresh:button.dataset.topicTrends==='refresh'});
    if (scope!==scopeSequence || sequence!==activitySequence) return;
    $('activity-output').innerHTML=activityHTML(report);
    $('global-status').hidden=true;
  });
});

function syncActivityRange(preset=false) {
  const ranged=isLive() && $('activity-range').value!=='day';
  $('activity-end-field').hidden=!ranged;$('activity-end').disabled=!ranged;$('activity-end').required=ranged;
  $('activity-date-label').textContent=ranged ? '开始日期' : '日期';
  if (preset && $('activity-range').value==='week') {
    const start=new Date(bootstrap.date+'T00:00:00Z');start.setUTCDate(start.getUTCDate()-6);
    $('activity-date').value=start.toISOString().slice(0,10);$('activity-end').value=bootstrap.date;
  }
  if (!ranged) $('activity-end').value=$('activity-date').value;
  $('activity-end').setCustomValidity(ranged && $('activity-end').value<$('activity-date').value ? '结束日期不能早于开始日期。' : '');
  ++activitySequence;$('activity-output').textContent='';
}
$('activity-range').addEventListener('change',()=>syncActivityRange(true));
for (const id of ['activity-date','activity-end']) $(id).addEventListener('change',()=>{
  if ($('activity-range').value!=='day') $('activity-range').value='custom';syncActivityRange();
});

function renderAlerts(alerts) {
  if (isLive()) {
    $('alerts-output').innerHTML=`<section class="panel"><div class="section-head"><h2>本机提醒记录</h2><span class="badge">最近 ${alerts.length} 条 · 未向群投递</span></div>${alerts.map(event=>`<article class="local-alert"><div class="section-head"><h3>${escapeHTML(event.keyword || selectedDataMembers()[event.member] || '指定成员')} ${event.member && event.keyword ? '· '+escapeHTML(selectedDataMembers()[event.member] || '指定成员') : ''}</h3><span class="badge">${event.status!=='active' ? '来源已变化' : event.legacy ? '旧记录' : event.updates ? '讨论串更新' : '新提醒'}</span></div><small>${event.legacy ? '旧记录未保存提醒时间' : fullTimeLabel(event.updated_at)} · ${event.evidence_count} 条证据</small>${event.messages.length ? `<p>${escapeHTML(event.messages.at(-1).content.slice(0,160))}${event.messages.at(-1).content.length>160 ? '…' : ''}</p><details><summary>查看 ${event.messages.length} 条当前原文</summary>${event.messages.map(row=>sourceCard(row)).join('')}</details>` : '<p class="hint">原文已删除、不可读或不再匹配，不显示旧缓存。</p>'}</article>`).join('') || '<div class="empty"><strong>还没有新提醒</strong>保存关注后，新发言或后续编辑命中时会出现在这里；已有历史不会自动变成新提醒。</div>'}</section>`;
    return;
  }
  $('alerts-output').innerHTML = `<section class="panel"><div class="section-head"><h2>本机提醒记录</h2><span class="badge">${alerts.length} 条 · 未向群投递</span></div>${alerts.map(a => `<article class="quote"><div class="quote-meta"><strong>${escapeHTML(a.message.name)}</strong><span>关注：${escapeHTML(a.keyword)}</span><span>${timeLabel(a.message.created)}</span></div><p>${escapeHTML(a.message.content)}</p><small>原文 ${escapeHTML(a.message.id)} · 频道 ${escapeHTML(bootstrap.channels[a.message.channel])}</small></article>`).join('') || '<div class="empty"><strong>还没有提醒</strong>输入关键词并点击“检查本地提醒”，试试“发布”。</div>'}</section>`;
}

async function loadAlerts() {
  if (scopeLoading) return;
  const key = dataScopeKey();
  const scope = scopeSequence;
  const result = await api('/api/alerts?' + new URLSearchParams(selectedDataScope()));
  if (scope !== scopeSequence || key !== dataScopeKey()) return;
  if (isLive()) renderAlertRules(result.rules);
  renderAlerts(result.alerts);
}

function renderAlertRules(rules) {
  $('alert-rule-list').innerHTML=`<section class="panel"><h2>当前频道的关注</h2>${rules.map(rule=>`<div class="saved-alert-rule"><span><strong>${escapeHTML(rule.keyword || '该成员的全部文字')}</strong>${rule.member ? ' · '+escapeHTML(selectedDataMembers()[rule.member] || '指定成员') : ''}<small>${rule.enabled ? '已启用' : '已暂停'} · 只在本机服务运行时检查</small></span><button type="button" data-alert-rule="${escapeHTML(rule.id)}" data-enabled="${rule.enabled ? 'false' : 'true'}">${rule.enabled ? '暂停' : '启用'}</button></div>`).join('') || '<p class="hint">保存一个关键词或成员，以后同步到的新内容会自动检查。</p>'}</section>`;
}

$('alert-rule-form').addEventListener('submit',event=>{
  event.preventDefault();if (scopeLoading) return;
  const scope=scopeSequence;
  task(event.submitter,'alert-result',async()=>{
    await api('/api/alert-rules',{...selectedDataScope(),keyword:$('rule-keyword').value,member:$('rule-member').value});
    if (scope!==scopeSequence) return;
    message('alert-result','关注已保存。已有记录作为基线，后续新发言和编辑命中时会自动更新本机提醒。');
    await loadAlerts();
  });
});
$('alert-rule-list').addEventListener('click',event=>{
  const button=event.target.closest('[data-alert-rule]');if (!button || scopeLoading) return;
  const scope=scopeSequence;
  task(button,'alert-result',async()=>{
    await api('/api/alert-rules',{...selectedDataScope(),id:button.dataset.alertRule,enabled:button.dataset.enabled==='true'});
    if (scope!==scopeSequence) return;
    message('alert-result',button.dataset.enabled==='true' ? '已启用，只关注此后的新发言和编辑。' : '该关注已暂停。');
    await loadAlerts();
  });
});

function updateQuestionState() {
  const hasTurns = questionTurns.length > 0;
  $('page-questions').classList.toggle('has-questions', hasTurns);
  $('question-welcome').hidden = hasTurns;
  $('question-input').placeholder = hasTurns ? '继续追问这个话题…' : '例如：这个频道最近主要聊了什么？';
  $('ask-question').textContent = hasTurns ? '发送 →' : '提问 →';
}

function clearQuestions(keepDraft=false) {
  ++questionSequence;
  questionBusy = false;
  questionConversation = '';
  questionTurns = [];
  historyBefore = '';
  $('older-questions').hidden = true;
  $('question-history').value = '';
  $('ask-question').disabled = false;
  if (!keepDraft) $('question-input').value = '';
  $('question-status').hidden = true;
  $('question-messages').innerHTML = '';
  $('question-scroll').scrollTop = 0;
  updateQuestionState();
  if (isLive()) updateLiveReadiness();
}

function renderQuestions(previousScroll=null) {
  const scroller = $('question-scroll');
  $('question-messages').innerHTML = questionTurns.map((turn, index) => {
    const question = `<div class="question-user"><small>你${turn.response ? ' · '+fullTimeLabel(turn.response.asked_at) : ''}</small><p>${escapeHTML(turn.question)}</p></div>`;
    if (turn.pending) return `<article class="question-turn">${question}<div class="question-answer question-waiting"><strong>拾聊正在整理回答</strong><p data-question-pending role="status">${escapeHTML(turn.pending)}</p><small>可以刷新页面，原问题会继续处理。</small></div></article>`;
    if (turn.error) return `<article class="question-turn">${question}<div class="message error">${escapeHTML(turn.error)}</div></article>`;
    const answer = turn.response;
    const sources = new Map(answer.messages.map(m => [m.id, {message:m, background:false}]));
    for (const context of answer.contexts) {
      if (context.status === 'available' && !sources.has(context.id)) sources.set(context.id, {message:context.message, background:true});
    }
    const evidence = [...sources.values()].map(({message:m, background}) => `<article class="source-message" id="question-source-${index}-${escapeHTML(m.id)}">${avatarHTML(m)}<strong>${escapeHTML(m.name)}</strong><small>${background ? '引用背景' : '当前频道原话'} · ${fullTimeLabel(m.created)} · 消息 ${escapeHTML(m.id)} · 成员 ${escapeHTML(m.author_id)}${m.source_channel && m.source_channel !== m.channel ? ` · 子线程 ${escapeHTML(m.thread_name || m.source_channel)}` : ''}</small>${discordSourceLink(m)}<p>${escapeHTML(m.content)}</p>${m.reference_id ? `<small>回复消息：${escapeHTML(m.reference_id)}</small>` : ''}</article>`).join('');
    const missing = answer.contexts.filter(c => c.status !== 'available').map(c => `<p class="hint">引用 ${escapeHTML(c.id)}：${escapeHTML(c.reason)}</p>`).join('');
    return `<article class="question-turn">${question}<div class="question-answer"><div class="answer-meta"><strong>拾聊</strong><small>${fullTimeLabel(answer.answered_at)}</small><span class="badge">${answer.offline ? '本机模拟' : '真实模型'} · ${escapeHTML(answer.scope.label)}</span></div><p class="answer-text">${escapeHTML(answer.answer).replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')}</p>${answer.points.length ? `<div class="answer-points">${answer.points.map(point => `<div class="answer-point"><strong>${escapeHTML(sources.get(point.sourceMessageIds[0]).message.name)}</strong>${point.citationId ? `<small>引用 ${escapeHTML(point.citationId)}</small>` : ''}<p>${escapeHTML(point.text)}</p>${point.sourceMessageIds.map(id => `<button type="button" class="evidence-link" data-question-source="${escapeHTML(id)}">查看原文 ↗</button>`).join('')}</div>`).join('')}</div><details class="question-sources"><summary>查看回答依据与引用背景</summary>${evidence}${missing}</details>` : ''}<p class="hint answer-coverage">${escapeHTML(answer.coverage.note)}</p><p class="hint question-budget">${answer.query_stats ? `本轮取证 ${answer.metrics.retrieved_messages} 条 · 所选窗口 ${answer.metrics.scope_messages} 条文字` : `聊天依据 ${answer.metrics.retrieved_messages}/${answer.metrics.matched_messages} 条匹配`} · 携带 ${answer.metrics.memory_turns} 次问答记忆（最多${answer.metrics.memory_limit || 6}次，内容裁短）${answer.metrics.memory_context?.hasSummary ? ` · 较早历史已摘录 ${answer.metrics.memory_context.summarizedTurnCount} 次问答` : ''} · 输入 ${(answer.metrics.input_bytes / 1024).toFixed(1)}/${Math.round(answer.metrics.input_limit_bytes / 1024)} KiB</p></div></article>`;
  }).join('');
  updateQuestionState();
  scroller.scrollTop = previousScroll ? previousScroll.top + scroller.scrollHeight - previousScroll.height : scroller.scrollHeight;
}

async function loadHistory() {
  if (isLive() && !selectedDataScope().channel) return;
  const scope = scopeSequence;
  const result = await api('/api/conversations?' + new URLSearchParams(selectedDataScope()));
  if (scope !== scopeSequence) return;
  options('question-history', {'':result.items.length ? '查看以前的问答' : '还没有问答记录', ...Object.fromEntries(result.items.map(item =>
    [item.id, `${item.title} · ${item.start_date} → ${item.end_date}`]))});
  $('question-history').disabled = !result.items.length;
  $('question-history').value = questionConversation;
}

async function restoreQuestions(id, before='') {
  if (scopeLoading || !id) return;
  const scope = scopeSequence, sequence = before ? questionSequence : ++questionSequence;
  const history = await api('/api/conversation?' + new URLSearchParams({id,...selectedDataScope(), before}));
  if (scope !== scopeSequence || sequence !== questionSequence) return;
  const scroller = $('question-scroll');
  const previousScroll = before ? {top:scroller.scrollTop, height:scroller.scrollHeight} : null;
  questionConversation = id;
  questionTurns = before ? [...history.turns, ...questionTurns] : history.turns;
  historyBefore = history.turns[0]?.seq || '';
  $('older-questions').hidden = !history.has_more;
  if (!before) {
    questionBusy = false;
    $('question-start').value = history.start_date;
    $('question-end').value = history.end_date;
    $('question-end').setCustomValidity('');
    $('question-input').value = '';
    $('ask-question').disabled = history.stale && !isLive();
    $('question-status').hidden = !history.stale;
    if (history.stale) message('question-status', isLive() ? '聊天已有更新，可直接继续提问；将重新检索并清除旧回答背景，历史记录保留。' : '本地聊天已更新。这是当时的存档；请点击“新对话”后重新提问。');
  }
  renderQuestions(previousScroll);
  return true;
}

async function runQuestion(request) {
  if (questionBusy || !questionScopeMatches(request)) return;
  const sequence=++questionSequence, scope=scopeSequence;
  const current=()=>sequence===questionSequence && scope===scopeSequence && questionScopeMatches(request);
  questionBusy=true;
  const pending={question:request.question,pending:'正在准备所选日期的记录…'};
  questionTurns.push(pending);
  $('question-input').value='';
  $('question-status').hidden=true;
  $('ask-question').disabled=true;
  renderQuestions();
  try {
    const response=isLive() ? await questionJob(request,text=>{
      if (!current()) return;
      pending.pending=text;
      const node=$('question-messages').querySelector('[data-question-pending]');
      if (node) node.textContent=text;
    }) : await api('/api/questions',request);
    if (!current()) return;
    if (!questionScopeMatches(response.scope,request)) throw new Error('回答范围与原问题不符，请查看以前的问答。');
    questionConversation=response.conversation_id;
    const index=questionTurns.indexOf(pending);
    const alreadySaved=questionTurns.some(turn=>turn.response?.asked_at===response.asked_at && turn.response?.conversation_id===response.conversation_id);
    if (alreadySaved) questionTurns.splice(index,1); else questionTurns[index]={question:request.question,response};
    renderQuestions();
    try { await loadHistory(); }
    catch { if (current()) message('question-status','回答已保存，历史列表暂时未刷新，可稍后重新打开查看。'); }
  } catch (error) {
    if (!current()) return;
    questionTurns[questionTurns.indexOf(pending)]={question:request.question,error:error.message};
    renderQuestions();
  } finally {
    if (current()) {
      questionBusy=false;
      $('ask-question').disabled=false;
      if (isLive()) updateLiveReadiness();
    }
  }
}

async function resumeQuestionJob() {
  const job=pendingQuestionJob();
  if (!isLive() || questionBusy || !job?.request || job.detached || !questionScopeMatches(job.request)) return;
  const scope=scopeSequence;
  if (job.request.conversation_id && !await restoreQuestions(job.request.conversation_id)) return;
  if (scope!==scopeSequence || !questionScopeMatches(job.request) || pendingQuestionJob()?.detached) return;
  $('question-start').value=job.request.start_date;
  $('question-end').value=job.request.end_date;
  $('question-end').setCustomValidity('');
  runQuestion(job.request);
}

$('question-history').addEventListener('change', event => {
  detachQuestionJob();
  if (!event.target.value) return clearQuestions();
  task(event.target, 'question-status', () => restoreQuestions(event.target.value));
});
$('older-questions').addEventListener('click', event => task(event.currentTarget, 'question-status', () => restoreQuestions(questionConversation, String(historyBefore))));

$('question-form').addEventListener('submit', event => {
  event.preventDefault();
  if (scopeLoading) return message('question-status', '请等待服务器和频道切换完成。', true);
  if (questionBusy) return;
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const question = $('question-input').value.trim();
  if (!question) return message('question-status', '请输入想了解的问题。', true);
  const request = {question,...selectedDataScope(),
    start_date:$('question-start').value, end_date:$('question-end').value, conversation_id:questionConversation};
  runQuestion(request);
});

$('clear-questions').addEventListener('click', () => {
  detachQuestionJob();
  clearQuestions();
  $('question-input').focus({preventScroll:true});
});
$('question-recent').addEventListener('click', () => {
  detachQuestionJob();
  const defaults=usingAccountSource() && accountScopeData?.question_defaults || bootstrap.question_defaults;
  $('question-start').value = defaults.start_date;
  $('question-end').value = defaults.end_date;
  $('question-end').setCustomValidity('');
  clearQuestions(true);
});
for (const id of ['question-start', 'question-end']) $(id).addEventListener('change', () => {
  detachQuestionJob();
  const start = $('question-start'), end = $('question-end');
  end.setCustomValidity(start.value && end.value && end.value < start.value ? '结束日期不能早于开始日期。' : '');
  clearQuestions(true);
});
document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => {
  $('question-input').value = button.dataset.question;
  $('question-input').focus();
}));
$('question-messages').addEventListener('click', event => {
  const button = event.target.closest('[data-question-source]');
  if (!button) return;
  const turn = button.closest('.question-turn');
  turn.querySelector('.question-sources').open = true;
  const index = [...$('question-messages').children].indexOf(turn);
  const target = $('question-source-' + index + '-' + button.dataset.questionSource);
  if (target) { target.tabIndex = -1; target.focus({preventScroll:true}); target.scrollIntoView({behavior:'instant', block:'center'}); }
});

function fillConfig(config) {
  for (const key of ['base_url','model','protocol','timeout','retries','max_tokens','temperature']) {
    $('config-form').elements.namedItem(key).value = config[key] ?? '';
  }
  $('api-key').value = '';
  $('api-key').type = 'password';
  $('toggle-key').textContent = '显示';
  $('toggle-key').setAttribute('aria-pressed','false');
  $('clear-key').checked = false;
  $('key-state').textContent = config.has_key ? '本次运行已保留演示密钥；留空保存不会覆盖。关闭服务后需重新填写。' : '尚未填写演示密钥，例如 demo-test。';
  if (isLive()) {
    for (const key of ['input_kib','output_parameter','json_mode']) $('config-form').elements.namedItem(key).value = String(config[key]);
    $('key-state').textContent = config.has_key ? '已保存密钥，留空不会覆盖；不会向页面回传完整密钥。' : '尚未保存密钥；不需要鉴权的本地服务可留空。';
    $('model-storage-help').textContent = config.storage === 'macos-keychain' ? '密钥保存在本机系统钥匙串，非敏感设置保存在本机。保存不会自动连接。' : '当前仅在本次服务运行内保留密钥，关闭服务后需重填；不会写入普通文件。';
  }
}

function configData() {
  const form = $('config-form');
  if ([...$('advanced').querySelectorAll('input,select')].some(el => !el.validity.valid)) $('advanced').open = true;
  if (!form.reportValidity()) return null;
  return {...Object.fromEntries(new FormData(form)), clear_key:$('clear-key').checked};
}

document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => {
  const settings=button.closest('details');
  if (settings) settings.open=true;
  activePage = button.dataset.page;
  document.body.classList.toggle('question-page', activePage === 'questions');
  document.querySelectorAll('.page').forEach(page => page.hidden = page.id !== `page-${activePage}`);
  document.querySelectorAll('[data-page]').forEach(nav => {
    nav.classList.toggle('active', nav === button);
    if (nav === button) nav.setAttribute('aria-current','page'); else nav.removeAttribute('aria-current');
  });
  $('breadcrumb').textContent = button.textContent.trim();
  window.scrollTo({top:0, left:0, behavior:'instant'});
  $('global-status').hidden = true;
  if (activePage === 'bots') task(button, 'global-status', loadBots);
  if (isLive()) updateLiveReadiness();
  task(button,'global-status',async()=>{
    if (isLive() && usingAccountSource()) { await loadImportPage(); await refreshSelectedAccount(); }
    if (activePage === 'activity') await loadActivity();
    if (activePage === 'alerts') await loadAlerts();
    if (activePage === 'questions') { await loadHistory(); await resumeQuestionJob(); }
    if (activePage === 'reports' && isLive()) await refreshDailyTask();
  });
}));

function syncDateRange() {
  const precise=isLive() && ['minute','today','yesterday','last24','last3','last7'].includes($('range-preset').value);
  const ranged = !precise && ($('kind').value === 'weekly' || ($('kind').value !== 'daily' && $('range-preset').value !== 'day'));
  $('date').closest('label').hidden=precise; $('date').disabled=precise;
  for (const id of ['report-start','report-end']) {
    $(id+'-field').hidden=!precise; $(id).disabled=!precise; $(id).required=precise;
  }
  $('report-end').setCustomValidity(precise && $('report-end').value <= $('report-start').value ? '结束时间必须晚于开始时间。' : '');
  $('date-range').classList.toggle('range-window', ranged);
  $('range-end-field').hidden = !ranged;
  $('range-end').disabled = !ranged;
  $('range-end').required = ranged;
  $('summary-period').value = ranged ? 'weekly' : 'daily';
  $('date-label').textContent = ranged ? '开始日期' : '日期';
  $('range-end').setCustomValidity('');
  if (precise) {
    $('range-help').textContent='北京时间 · YYYY-MM-DD HH:MM · 包含开始时间，不包含结束时间。';
    return;
  }
  if (!ranged) {
    $('range-end').value = $('date').value;
    $('range-help').textContent = '覆盖所选自然日，按北京时间统计。';
    return;
  }
  const start = new Date($('date').value + 'T00:00:00Z');
  const end = new Date($('range-end').value + 'T00:00:00Z');
  if (end < start) {
    $('range-end').setCustomValidity('结束日期不能早于开始日期。');
    $('range-help').textContent = '结束日期不能早于开始日期。';
    return;
  }
  const days = (end - start) / 86400000 + 1;
  $('range-help').textContent = Number.isFinite(days) ? `共 ${days} 天，包含起止当天；北京时间。可直接修改日期。` : '请选择开始和结束日期；北京时间。';
}

function applyRangePreset() {
  const preset = $('range-preset').value;
  if (isLive() && ['today','yesterday','last24','last3','last7'].includes(preset)) {
    [$('report-start').value,$('report-end').value]=quickTimeRange(preset,'Asia/Shanghai');
    syncDateRange(); return;
  }
  if (preset === 'minute' && !$('report-start').value) {
    $('report-start').value=$('date').value+'T00:00';
    const end=new Date(($('range-end').value || $('date').value)+'T00:00:00Z'); end.setUTCDate(end.getUTCDate()+1);
    $('report-end').value=end.toISOString().slice(0,10)+'T00:00';
  }
  if (!['custom','day','minute'].includes(preset)) {
    const end = new Date(bootstrap.date + 'T00:00:00Z');
    const start = new Date(end);
    if (preset === 'last7') start.setUTCDate(start.getUTCDate() - 6);
    else {
      start.setUTCDate(start.getUTCDate() - (start.getUTCDay() + 6) % 7 - (preset === 'lastweek' ? 7 : 0));
      end.setTime(start.getTime());
      end.setUTCDate(end.getUTCDate() + 6);
    }
    $('date').value = start.toISOString().slice(0, 10);
    $('range-end').value = end.toISOString().slice(0, 10);
  }
  syncDateRange();
}

function syncReportForm() {
  const kind = $('kind').value;
  const periodic = isPeriodic(kind);
  $('layout-toolbar').hidden = !periodic;
  $('report-layout').disabled = !periodic;
  $('report-focus').hidden = !periodic;
  $('range-preset-field').hidden = kind === 'daily' && !isLive();
  $('range-minute-option').hidden=!isLive();
  $('range-single-option').hidden = kind === 'weekly';
  $('summary-period').disabled = periodic;
  $('focus-member').disabled = !periodic;
  $('focus-topic').disabled = !periodic;
  for (const [field, enabled] of [['member', kind === 'member'], ['topic', kind === 'discussion']]) {
    $(field + '-field').hidden = !enabled;
    $(field).disabled = !enabled;
    $(field).required = enabled;
  }
  $('generate-report').textContent = {daily:'生成离线日报 →', weekly:'生成离线周报 →', member:'生成成员摘要 →', discussion:'生成讨论摘要 →'}[kind];
  if (kind === 'daily' && !['minute','today','yesterday','last24','last3','last7'].includes($('range-preset').value)) $('range-preset').value = 'day';
  if (kind === 'weekly' && $('range-preset').value === 'day') $('range-preset').value = 'custom';
  syncDateRange();
  if (isLive()) {
    $('generate-report').textContent = {daily:'生成日报 →',weekly:'生成周报 →',member:'生成成员摘要 →',discussion:'生成讨论摘要 →'}[kind];
    updateLiveReadiness();
  }
}

function clearReport() {
  ++reportSequence;
  lastReport = null;
  parentReport = null;
  deliveryReportKey = '';
  clearDelivery();
  $('report-delivery').hidden = true;
  $('report-output').innerHTML = '<div class="empty"><strong>选择范围后点击生成</strong>日报、周报提供版式选择，单独摘要使用固定格式。</div>';
}

for (const id of ['date', 'range-end']) $(id).addEventListener('change', () => {
  if (!$('range-end-field').hidden) $('range-preset').value = 'custom';
  syncDateRange();
  clearReport();
});
$('range-preset').addEventListener('change', () => { applyRangePreset(); clearReport(); });

function restoreReportForm(report) {
  $('kind').value = report.kind;
  $('date').value = report.window.start_date;
  $('range-end').value = report.window.end_date;
  $('range-preset').value = report.period === 'daily' ? 'day' : 'custom';
  $('channel').value = report.channel;
  $('report-mode').value = report.digest.mode;
  $('summary-period').value = report.period;
  if (report.kind === 'member') $('member').value = report.subject.id;
  if (report.kind === 'discussion') $('topic').value = report.subject.id;
  if (isPeriodic(report.kind)) {
    $('focus-member').value = report.dimensions.member?.subject.id || '';
    $('focus-topic').value = report.dimensions.discussion?.subject.id || '';
  }
  syncReportForm();
}

$('kind').addEventListener('change', () => {
  if ($('kind').value !== 'daily' && $('range-preset').value === 'day') $('range-preset').value = 'custom';
  syncReportForm();
  clearReport();
});
$('report-form').addEventListener('submit', event => {
  event.preventDefault();
  task(event.submitter, 'global-status', async () => { await loadReport(); if (!isLive()) $('global-status').hidden = true; });
});
$('activity-form').addEventListener('submit', event => { event.preventDefault(); task(event.submitter, 'global-status', loadActivity); });
document.addEventListener('change',event=>{
  if (event.target.matches('[data-copy-sources]')) resetDiscordCopyOptions(event.target);
});

$('report-output').addEventListener('click', event => {
  const copy=event.target.closest('[data-copy-discord]');
  if (copy && lastReport) { copyReportToDiscord(lastReport,copy); return; }
  const extractButton = event.target.closest('[data-extract-summary]');
  if (extractButton && lastReport && isPeriodic(lastReport.kind)) {
    const dimension = lastReport.dimensions[extractButton.dataset.extractSummary];
    if (!dimension || dimension.generation.status === 'failed' || !dimension.fullDigest) return;
    ++reportSequence;
    parentReport = lastReport;
    lastReport = {report_id:parentReport.report_id, offline:parentReport.offline, server:parentReport.server, channel:parentReport.channel,
      date:parentReport.date, window:parentReport.window, ...dimension, source_dimension:extractButton.dataset.extractSummary, dimensions:{}};
    restoreReportForm(lastReport);
    renderCachedReport();
    $('report-form').scrollIntoView({behavior:'instant', block:'start'});
    return;
  }
  if (event.target.closest('[data-back-report]') && parentReport) {
    ++reportSequence;
    lastReport = parentReport;
    parentReport = null;
    restoreReportForm(lastReport);
    renderCachedReport();
    $('report-form').scrollIntoView({behavior:'instant', block:'start'});
    return;
  }
  const fullButton = event.target.closest('[data-full-report]');
  if (fullButton) {
    $('report-mode').value = 'full';
    if (lastReport) setReadingMode(lastReport, 'full');
    if (parentReport) setReadingMode(parentReport, 'full');
    renderCachedReport();
    return;
  }
  const button = event.target.closest('[data-source]');
  if (!button) return;
  $('sources').open = true;
  const source = $('source-' + button.dataset.source);
  source?.scrollIntoView({behavior:'instant', block:'center'});
  if (source) { source.tabIndex = -1; source.focus({preventScroll:true}); }
});
$('alerts-form').addEventListener('submit', event => {
  event.preventDefault();
  if (scopeLoading) return;
  task(event.submitter, 'alert-result', async () => {
    const server = bootstrap.selected_server;
    const scope = scopeSequence;
    const result = await api('/api/alerts', {keyword:$('keyword').value, channel:$('channel').value, date:$('alert-date').value, server});
    if (scope !== scopeSequence || server !== bootstrap.selected_server) return;
    renderAlerts(result.alerts);
    if (result.preview) {
      message('alert-result',`历史中找到 ${result.matches.length} 条匹配；这次只查看，不产生新提醒。`);
      const sources=document.createElement('div');sources.innerHTML=result.matches.map(row=>sourceCard(row)).join('');
      $('alert-result').append(sources);return;
    }
    message('alert-result', result.created ? `新增 ${result.created} 条本地提醒。没有向群发送任何消息。` : '没有新增提醒。已记录的相同消息不会重复生成。');
  });
});
$('config-form').addEventListener('submit', event => {
  event.preventDefault();
  const data = configData();
  if (!data) return;
  task(event.submitter, 'config-status', async () => {
    const config = await api('/api/config', data);
    bootstrap.config = config;
    fillConfig(config);
    if (isLive()) updateLiveReadiness();
    message('config-status',isLive() ? '配置已保存，未发起连接。可点击“测试模型连接”，测试可能产生少量费用。' : '配置已保存到本机，未发起连接。演示密钥仅在本次运行期间保留。');
  });
});
$('check-config').addEventListener('click', event => {
  const data = configData();
  if (!data) return;
  task(event.currentTarget, 'config-status', async () => {
    const result = await api('/api/config/check', data);
    message('config-status', result.message);
  });
});
$('toggle-key').addEventListener('click', () => {
  const visible = $('api-key').type === 'password';
  $('api-key').type = visible ? 'text' : 'password';
  $('toggle-key').textContent = visible ? '隐藏' : '显示';
  $('toggle-key').setAttribute('aria-pressed', String(visible));
});
$('reset-advanced').addEventListener('click', () => {
  for (const [name, value] of Object.entries({protocol:'openai-compatible', timeout:30, retries:1, max_tokens:2048, temperature:''})) $('config-form').elements.namedItem(name).value = value;
  if (isLive()) for (const [name,value] of Object.entries({timeout:300,max_tokens:8192,input_kib:48,json_mode:'false',output_parameter:'auto'})) $('config-form').elements.namedItem(name).value = value;
  message('config-status','高级设置已恢复默认。点击“保存配置”后生效。');
});

function clearDelivery() {
  ++deliverySequence;
  deliveryPreview = null;
  $('delivery-preview').textContent = '';
  $('delivery-preview').hidden = true;
  $('simulate-delivery').hidden = true;
  $('delivery-status').hidden = true;
}

function fillDeliveryChannels() {
  const bot = botList.find(item => item.id === $('delivery-bot').value);
  const previous = $('delivery-channel').value;
  options('delivery-channel', bot ? Object.fromEntries(bot.channels.map(id => [id, '#' + bootstrap.channels[id]])) : {'':'先配置 Bot'});
  if (bot?.channels.includes(previous)) $('delivery-channel').value = previous;
  else if (bot?.channels.includes(bootstrap.selected_channel)) $('delivery-channel').value = bootstrap.selected_channel;
  $('preview-delivery').disabled = !bot;
}

function renderBots() {
  if (isLive()) return renderLiveBots();
  $('bot-list').innerHTML = botList.length ? botList.map(bot => `<article class="bot-card"><div><strong>${escapeHTML(bot.name)}</strong><p class="hint">${bot.channels.map(c => '#' + escapeHTML(bootstrap.channels[c])).join(' · ')}</p><span class="badge">未连接 · 权限未验证</span></div><button type="button" data-edit-bot="${escapeHTML(bot.id)}">编辑</button></article>`).join('') : '<div class="empty"><strong>还没有配置 Bot</strong>填写右侧表单后，可在报告中预览发送。</div>';
  const previous = $('delivery-bot').value;
  options('delivery-bot', botList.length ? Object.fromEntries(botList.map(bot => [bot.id, bot.name])) : {'':'先配置 Bot'});
  if (botList.some(bot => bot.id === previous)) $('delivery-bot').value = previous;
  fillDeliveryChannels();
}

async function loadBots() {
  const scope = scopeSequence, sequence = ++botSequence;
  const result = await api('/api/bots' + (isLive() ? '' : '?' + new URLSearchParams({server:bootstrap.selected_server})));
  if (scope !== scopeSequence || sequence !== botSequence) return;
  botList = result.items;
  renderBots();
}

function resetBotForm(bot=null) {
  if (isLive()) return resetLiveBotForm(bot);
  $('bot-id').value = bot?.id || '';
  $('bot-name').value = bot?.name || '';
  $('bot-application').value = bot?.application_id || '';
  $('bot-token').value = '';
  $('clear-bot-token').checked = false;
  $('bot-form-title').textContent = bot ? '编辑 Bot' : '添加 Bot';
  $('bot-scope').textContent = '所属服务器：' + bootstrap.servers[bootstrap.selected_server];
  $('bot-token-state').textContent = bot?.has_token ? '本次运行已保留演示 Token，留空不会覆盖；关闭服务后清除。' : '可留空。演示 Token 只在本次运行期间保留，不写入数据库。';
  $('bot-channels').innerHTML = Object.entries(bootstrap.channels).map(([id, name]) => `<label class="check-label"><input type="checkbox" name="bot-channel" value="${escapeHTML(id)}" ${(bot ? bot.channels.includes(id) : id === bootstrap.selected_channel) ? 'checked' : ''}>#${escapeHTML(name)}</label>`).join('');
  $('bot-status').hidden = true;
}

$('new-bot').addEventListener('click', () => { resetBotForm(); $('bot-name').focus(); });
$('cancel-bot-edit').addEventListener('click', () => resetBotForm());
$('bot-list').addEventListener('click', event => {
  const button = event.target.closest('[data-edit-bot]');
  if (button) resetBotForm(botList.find(bot => bot.id === button.dataset.editBot));
});
$('bot-form').addEventListener('submit', event => {
  event.preventDefault();
  if (scopeLoading) return;
  if (!$('bot-application').validity.valid) $('bot-form').querySelector('details').open = true;
  if (!event.currentTarget.reportValidity()) return;
  const scope = scopeSequence;
  const data = isLive() ? {id:$('bot-id').value,name:$('bot-name').value,token:$('bot-token').value,clear_token:$('clear-bot-token').checked} : {id:$('bot-id').value, name:$('bot-name').value, server:bootstrap.selected_server,
    channels:[...$('bot-channels').querySelectorAll('input:checked')].map(input => input.value),
    token:$('bot-token').value, application_id:$('bot-application').value, clear_token:$('clear-bot-token').checked};
  task(event.submitter, 'bot-status', async () => {
    const bot = await api('/api/bots', data);
    if (scope !== scopeSequence) return;
    await loadBots();
    clearDelivery();
    resetBotForm(bot);
    message('bot-status', isLive() ? 'Bot配置已保存，尚未发起连接。请点击列表中的“连接并发现服务器”。' : '离线配置已保存，尚未连接 Discord 或验证权限。请回到报告页预览发送。');
  });
});
$('manage-bots').addEventListener('click', () => document.querySelector('[data-page="bots"]').click());
$('delivery-bot').addEventListener('change', () => { fillDeliveryChannels(); clearDelivery(); });
$('delivery-channel').addEventListener('change', clearDelivery);
$('delivery-form').addEventListener('submit', event => {
  event.preventDefault();
  if (scopeLoading || !lastReport || !event.currentTarget.reportValidity()) return;
  clearDelivery();
  const sequence = deliverySequence, scope = scopeSequence;
  const data = {report_id:lastReport.report_id, kind:lastReport.kind, bot_id:$('delivery-bot').value,
    destination:$('delivery-channel').value, server:bootstrap.selected_server, channel:bootstrap.selected_channel};
  task(event.submitter, 'delivery-status', async () => {
    const result = await api('/api/deliveries/preview', data);
    if (scope !== scopeSequence || sequence !== deliverySequence) return;
    deliveryPreview = result;
    $('delivery-preview').innerHTML = `<div class="section-head"><strong>${escapeHTML(result.bot_name)} → #${escapeHTML(bootstrap.channels[result.destination])}</strong><span class="badge">${result.payloads.length} 条文字消息 · 未实际发送</span></div>${result.payloads.map((payload, i) => `<details ${i === 0 ? 'open' : ''}><summary>第 ${i + 1} 条</summary><pre>${escapeHTML(payload.content)}</pre></details>`).join('')}`;
    $('delivery-preview').hidden = false;
    $('simulate-delivery').hidden = false;
    message('delivery-status', '预览使用已生成的完整报告快照。只做本地模拟，所有提及均禁止解析。');
  });
});
$('simulate-delivery').addEventListener('click', event => {
  if (scopeLoading || !deliveryPreview) return;
  const sequence = deliverySequence, scope = scopeSequence;
  const data = {preview_id:deliveryPreview.id, server:bootstrap.selected_server, channel:bootstrap.selected_channel};
  task(event.currentTarget, 'delivery-status', async () => {
    const result = await api('/api/deliveries/simulate', data);
    if (scope === scopeSequence && sequence === deliverySequence) message('delivery-status', result.message);
  });
});

function fillScope() {
  options('server', bootstrap.servers);
  $('server').value = bootstrap.selected_server;
  options('channel', bootstrap.channels);
  $('channel').value = bootstrap.selected_channel;
  options('member', bootstrap.members); options('topic', bootstrap.topics);
  options('focus-member', {'':'不额外关注', ...bootstrap.members});
  options('rule-member', {'':'所有成员',...bootstrap.members});
  $('rule-keyword').value='';
  options('focus-topic', {'':'不额外关注', ...bootstrap.topics});
  $('focus-member').value = '';
  $('focus-topic').value = '';
  if (isLive()) $('topic').value = '本频道全部讨论';
  $('server-help').textContent = '所有功能只使用当前频道，不合并其他频道。';
  $('scope-label').textContent = `${bootstrap.servers[bootstrap.selected_server]} / #${bootstrap.channels[bootstrap.selected_channel]}`;
  $('question-start').value = bootstrap.question_defaults.start_date;
  $('question-end').value = bootstrap.question_defaults.end_date;
  $('question-end').setCustomValidity('');
  options('question-history', {'':'还没有问答记录'});
  $('question-history').disabled = true;
  botList = [];
  resetBotForm();
  renderBots();
  if (isLive()) updateLiveReadiness();
  $('auto-sync-status').hidden = true;
  if (isLive()) resetDailyScope();
}

async function changeScope(event) {
  const select = event.currentTarget;
  const server = $('server').value;
  const channel = select.id === 'channel' ? select.value : '';
  scopeLoading = true;
  ++scopeSequence;
  clearReport();
  clearQuestions();
  for (const id of ['activity-output','alerts-output','alert-result','alert-rule-list']) $(id).textContent = '';
  task(select, 'global-status', async () => {
    const other = $(select.id === 'server' ? 'channel' : 'server');
    other.disabled = true;
    try {
      bootstrap = await api('/api/bootstrap?' + new URLSearchParams({server, channel}));
      fillScope();
      scopeLoading = false;
      if (activePage === 'activity') await loadActivity();
      if (activePage === 'alerts') await loadAlerts();
      if (activePage === 'questions') { await loadHistory(); await resumeQuestionJob(); }
      if (activePage === 'bots') await loadBots();
      if (isLive() && activePage !== 'bots') await loadBots();
      if (isLive()) await activateLiveSync();
      if (isLive()) await refreshDailyTask(true);
      message('global-status', `已选择“${bootstrap.servers[bootstrap.selected_server]} / #${bootstrap.channels[bootstrap.selected_channel]}”，可以继续使用下方功能。`);
    } finally {
      scopeLoading = false;
      $('server').value = bootstrap.selected_server;
      $('channel').value = bootstrap.selected_channel;
      other.disabled = false;
    }
  });
}
$('server').addEventListener('change', changeScope);
$('channel').addEventListener('change', changeScope);
for (const id of ['report-start','report-end']) $(id).addEventListener('change',()=>{ $('range-preset').value='minute'; syncDateRange(); clearReport(); });

(async () => {
  try {
    bootstrap = await api('/api/bootstrap');
    if (isLive()) configureLiveUI();
    else {
      $('source-notice').innerHTML = '<strong>正在体验离线样例</strong><span>所有聊天均为虚构；使用本机模拟模型，不连接Discord或真实模型，不发送群消息。</span>';
      $('activity-range-field').hidden=true;
    }
    initLayouts();
    fillScope();
    for (const id of ['date','activity-date','activity-end','alert-date']) $(id).value = bootstrap.date;
    syncActivityRange();
    syncReportForm();
    fillConfig(bootstrap.config);
    if (isLive()) { clearReport(); await loadBots(); updateLiveReadiness(); await activateLiveSync(); if (accountReportsVisible()) await loadImportPage(); }
    else await loadReport();
    if (isLive()) await resumeReportJob();
    const pendingQuestion=pendingQuestionJob();
    if (isLive() && pendingQuestion?.request && !pendingQuestion.detached && questionScopeMatches(pendingQuestion.request)) {
      document.querySelector('[data-page="questions"]').click();
    }
  } catch (error) {
    message('global-status', '本机服务暂不可用。请双击“启动拾聊.command”后刷新页面。' + (error.message || ''), true);
  }
})();
