'use strict';

let dailyScope = '';
let dailyTask = null;
let dailyDirty = false;
let dailyPolling = false;
let dailyBefore = '';
let dailyHistorySignature = '';
let dailyBackground = null;

function currentDailyScope() {
  return selectedDataScope();
}

function selectedDailyMembers() {
  return [...$('daily-members').querySelectorAll('input:checked')].map(input => input.value);
}

function renderDailyMembers(reset=false) {
  if (!isLive()) return;
  const selected = new Set(reset ? dailyTask?.members || [] : selectedDailyMembers());
  const names = {...dailyTask?.member_names, ...selectedDataMembers()};
  $('daily-members').innerHTML = Object.entries(names).map(([id,name]) => `<label class="daily-member" title="${escapeHTML(name)} · ${escapeHTML(id)}" data-member-name="${escapeHTML(name.toLocaleLowerCase())}"><input type="checkbox" value="${escapeHTML(id)}"${selected.has(id) ? ' checked' : ''}><span><span class="member-choice-name">${escapeHTML(name)}</span><small title="${escapeHTML(id)}">${escapeHTML(id.slice(-6))}</small></span></label>`).join('') || '<p class="hint">正在同步频道。成员发过言后会出现在这里，可先只开启频道日报。</p>';
  filterDailyMembers();
}

function filterDailyMembers() {
  const query = $('daily-member-search').value.trim().toLocaleLowerCase();
  for (const row of $('daily-members').querySelectorAll('.daily-member')) row.hidden = !row.dataset.memberName.includes(query);
  $('daily-member-count').textContent = `已选 ${selectedDailyMembers().length} 人`;
}

function resetDailyScope() {
  dailyScope = '';
  dailyTask = null;
  dailyDirty = false;
  dailyBefore = '';
  dailyHistorySignature = '';
  $('daily-runs').textContent = '';
  $('daily-message').hidden = true;
  $('daily-member-search').value = '';
  $('daily-members').textContent = '';
  $('daily-time').value = '00:10';
  $('daily-enabled').checked = false;
  $('daily-instructions').value='';
  $('daily-preview').disabled = true;
  $('daily-generate-date').disabled = true;
  $('daily-badge').textContent = '正在读取设置';
  $('daily-summary').textContent = '读取当前频道的固定名单与每日任务…';
  $('daily-panel').hidden = !isLive() || !currentDailyScope().channel;
  if (!$('daily-panel').hidden) refreshDailyTask(true).catch(error => message('daily-message',error.message,true));
}

function dailyRunHTML(run) {
  const labels = {pending:run.retry_at ? '等待记录补齐' : '等待生成',running:'正在生成',needs_attention:'需要处理',complete:'已生成'};
  return `<article class="daily-run"><div class="daily-run-heading"><strong>${escapeHTML(run.date)}</strong><small>${labels[run.status] || '待检查'}</small></div><div class="daily-report-links">${run.steps.map(step => step.status === 'complete' ? `<button type="button" data-daily-report="${escapeHTML(step.report_id)}">${escapeHTML(step.label)}${step.coverage === 'partial' ? ' · 覆盖不全' : ''}</button>` : `<span class="hint">${escapeHTML(step.label)} · ${step.status === 'running' ? '生成中' : step.status === 'uncertain' ? '结果待确认' : step.status === 'failed' ? '未完成' : '等待'}</span>`).join('')}${run.status === 'needs_attention' ? `<button type="button" data-daily-retry="${escapeHTML(run.date)}">继续未完成项</button>` : ''}</div>${run.status === 'needs_attention' ? `<p class="daily-run-note">${escapeHTML(run.note)} 继续可能重新调用未完成项的模型。</p>` : ''}</article>`;
}

async function refreshDailyTask(reset=false, before='') {
  if (!isLive() || scopeLoading || !currentDailyScope().channel || currentDailyScope().source==='account' && !currentDailyScope().account_id) return;
  const scope = scopeSequence, key = JSON.stringify(currentDailyScope());
  const data = await api('/api/daily-task?' + new URLSearchParams({...currentDailyScope(),before}));
  if (scope !== scopeSequence) return;
  const changed = dailyScope !== key;
  dailyScope = key;
  dailyTask = data.task;
  $('daily-panel').hidden = false;
  const enabled = Boolean(dailyTask?.enabled);
  const zoneLabel={'Asia/Shanghai':'北京时间','America/Los_Angeles':'洛杉矶时间',UTC:'UTC'}[dailyTask?.timezone] || '北京时间';
  $('daily-badge').textContent = enabled ? `每天 ${dailyTask.run_time} · ${zoneLabel}` : dailyTask ? '已暂停' : '未开启';
  $('daily-summary').textContent = dailyTask ? `${enabled ? `每天 ${dailyTask.run_time}` : '已保存固定名单'} · 频道日报 + ${dailyTask.members.length} 人摘要${enabled ? ` · 下一统计日 ${dailyTask.next_day}` : ''}` : '设置一次，之后自动生成；结果和原文保存在本机。';
  $('daily-preview').disabled = !dailyTask;
  $('daily-generate-date').disabled = !dailyTask;
  if (reset || changed || !dailyDirty) {
    $('daily-time').value = dailyTask?.run_time || '00:10';
    $('daily-enabled').checked = !!dailyTask?.enabled;
    $('daily-instructions').value=dailyTask?.instructions || '';
    $('daily-timezone').value=dailyTask?.timezone || currentDailyScope().timezone || 'Asia/Shanghai';
    $('daily-timezone').disabled=!usingAccountSource();
    $('daily-time-help').textContent='按所选时区总结前一天 00:00—24:00 的完整记录。';
    renderDailyMembers(true);
    $('save-daily').textContent = $('daily-enabled').checked ? '保存并开启' : '保存设置';
  }
  if (changed || reset) {
    $('daily-panel').open = false;
    $('daily-date').value=quickTimeRange('yesterday',$('daily-timezone').value)[0].slice(0,10);
    $('daily-date').max = $('daily-date').value;
  }
  const signature = JSON.stringify(data.runs);
  if (signature !== dailyHistorySignature || before) {
    if (before) $('daily-runs').insertAdjacentHTML('beforeend',data.runs.map(dailyRunHTML).join(''));
    else $('daily-runs').innerHTML = data.runs.map(dailyRunHTML).join('');
    dailyHistorySignature = signature;
    dailyBefore = data.next_before;
    $('daily-older').hidden = !dailyBefore;
  }
}

async function refreshBackground() {
  dailyBackground = await api('/api/background');
  $('background-state').textContent = dailyBackground.managed ? '后台运行中 · 登录后自动启动，可关闭网页和终端。' : dailyBackground.supported ? '当前服务已启动；双击“启动拾聊”即可改为登录后台运行。' : '当前设备的常驻服务请由维护者配置。';
  $('enable-background').hidden = dailyBackground.managed || !dailyBackground.supported;
  $('enable-background').disabled = dailyBackground.generating;
}

async function queueDaily(date='', retry=false) {
  if (scopeLoading) return;
  const scope=scopeSequence;
  const run=await api('/api/daily-task/run',{...currentDailyScope(),...(date ? {date} : {}),...(retry ? {retry:true} : {})});
  if (scope !== scopeSequence) return;
  message('daily-message',run.status === 'complete' ? '这一天已生成，点击下方报告直接查看，不会重复调用模型。' : '已加入后台生成任务，可离开页面；完成后从下方打开报告。');
  await refreshDailyTask();
}

function configureDailyUI() {
  $('daily-form').addEventListener('submit',event => {
    event.preventDefault();
    if (scopeLoading || !$('daily-form').reportValidity()) return;
    const scope=scopeSequence;
    task(event.submitter,'daily-message',async () => {
      await api('/api/daily-task',{...currentDailyScope(),members:selectedDailyMembers(),run_time:$('daily-time').value,timezone:$('daily-timezone').value,instructions:$('daily-instructions').value,enabled:$('daily-enabled').checked});
      if (scope !== scopeSequence) return;
      dailyDirty=false;
      await refreshDailyTask(true);
      message('daily-message',dailyTask.enabled ? `已开启：每天 ${dailyTask.run_time} 整理前一天的频道话题和 ${dailyTask.members.length} 位成员发言。` : '每日自动生成已暂停，固定成员名单保留。');
    });
  });
  $('daily-form').addEventListener('change',() => {
    dailyDirty=true; filterDailyMembers();
    $('save-daily').textContent=$('daily-enabled').checked ? '保存并开启' : '保存设置';
  });
  $('daily-time').addEventListener('input',() => { dailyDirty=true; });
  $('daily-instructions').addEventListener('input',() => { dailyDirty=true; });
  $('daily-member-search').addEventListener('input',filterDailyMembers);
  $('daily-preview').addEventListener('click',event => task(event.currentTarget,'daily-message',() => queueDaily()));
  $('daily-generate-date').addEventListener('click',event => task(event.currentTarget,'daily-message',() => queueDaily($('daily-date').value)));
  $('daily-older').addEventListener('click',event => task(event.currentTarget,'daily-message',() => refreshDailyTask(false,dailyBefore)));
  $('daily-runs').addEventListener('click',event => {
    const view=event.target.closest('[data-daily-report]'), retry=event.target.closest('[data-daily-retry]');
    if (!view && !retry || scopeLoading) return;
    const scope=scopeSequence;
    task(view || retry,'daily-message',async () => {
      if (retry) return queueDaily(retry.dataset.dailyRetry,true);
      const report=await api('/api/daily-report?'+new URLSearchParams({...currentDailyScope(),id:view.dataset.dailyReport}));
      if (scope !== scopeSequence) return;
      if (usingAccountSource()) {
        showImportReport(report);$('import-result').scrollIntoView({block:'start'});
        message('daily-message','正在查看已保存的报告与当时原文，没有重新调用模型。');return;
      }
      parentReport=null; lastReport=report;
      restoreReportForm(report);
      setReadingMode(report,$('report-mode').value);
      renderCachedReport();
      $('report-output').scrollIntoView({block:'start'});
      message('daily-message','正在查看已保存的报告与当时原文，没有重新调用模型。');
    });
  });
  $('enable-background').addEventListener('click',event => task(event.currentTarget,'daily-message',async () => {
    const value=await api('/api/background/start',{});
    message('daily-message',value.note);
  }));
  refreshBackground().catch(() => {});
  setInterval(async () => {
    if (!isLive() || dailyPolling || scopeLoading || !currentDailyScope().channel) return;
    dailyPolling=true;
    try { await refreshDailyTask(); await refreshBackground(); }
    catch { /* The launch-agent handoff briefly restarts the local listener. */ }
    finally { dailyPolling=false; }
  },4000);
}
