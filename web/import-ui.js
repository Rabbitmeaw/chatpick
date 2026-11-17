'use strict';

let importItem = null;
let importReport = null;
let importSequence = 0;
let importBusy = false;
let importReading = false;
let importHistory = [];
let exporterAccount = {};
let importFromAccount = false;
let importRangeTimer;
let importMemberScope = '';
let importPreferences = {};
let accountScopeData = null;
let accountScopeSequence = 0;
try { importPreferences=JSON.parse(localStorage.getItem('chatpick-report-preferences') || '{}'); } catch { /* 默认设置 */ }

function usingAccountSource() { return isLive() && $('report-source').value === 'account'; }
function accountReportsVisible() { return usingAccountSource() && activePage === 'reports'; }

function selectedDataScope() {
  return usingAccountSource() ? {server:$('exporter-server').value,channel:$('exporter-channel').value,
    source:'account',account_id:exporterAccount.user_id || '',timezone:$('import-timezone').value || 'Asia/Shanghai'} :
    {server:bootstrap.selected_server,channel:bootstrap.selected_channel};
}

function selectedDataMembers() { return usingAccountSource() ? accountScopeData?.members || {} : bootstrap.members; }
function dataScopeKey() { return JSON.stringify(selectedDataScope()); }

async function refreshSelectedAccount() {
  if (!usingAccountSource()) return;
  const request=selectedDataScope(),key=dataScopeKey(),scope=scopeSequence,sequence=++accountScopeSequence;
  if (!request.account_id || !request.server || !request.channel) return;
  const data=await api('/api/account-data?'+new URLSearchParams(request));
  if (sequence !== accountScopeSequence || scope !== scopeSequence || key !== dataScopeKey()) return;
  const first=!accountScopeData;
  accountScopeData=data;
  if (first && data.question_defaults) {
    $('question-start').value=data.question_defaults.start_date;
    $('question-end').value=data.question_defaults.end_date;
  }
  if (first && data.date) for (const id of ['activity-date','activity-end','alert-date']) $(id).value=data.date;
  const selected=$('rule-member').value,members=data.members || {};
  options('rule-member',{'':'所有成员',...members});
  if (selected in members) $('rule-member').value=selected;
  $('account-sync-enabled').checked=!!data.sync?.enabled;
  $('account-sync-note').textContent=data.sync?.enabled ? data.sync.note || '本机服务运行时持续读取当前频道，不自动调用模型。' : '持续读取未开启；生成或提问时会补齐所选日期。';
  renderDailyMembers(false);
}

function invalidateSelectedScope() {
  ++scopeSequence; ++activitySequence; ++questionSequence; ++accountScopeSequence;
  accountScopeData=null;
  importItem=null;importReport=null;importHistory=[];importMemberScope='';
  lastReport=null;parentReport=null;
  $('import-result').textContent='';$('report-output').textContent='';$('import-members').textContent='';
  $('import-analysis').hidden=true;renderImportHistory();
  $('activity-output').textContent=''; $('alerts-output').textContent=''; $('alert-rule-list').textContent='';
  $('daily-panel').hidden=true; resetDailyScope();
  clearQuestions();
  options('question-history', {'':'还没有问答记录'});
  $('question-history').disabled=true;
}

function saveImportPreferences() {
  const server=$('exporter-server').value, channel=$('exporter-channel').value;
  const members=importMemberScope === server+'/'+channel ? [...$('import-members').querySelectorAll('input:checked')].map(n=>n.value) :
    importPreferences.server === server && importPreferences.channel === channel ? importPreferences.members || [] : [];
  importPreferences={server:$('exporter-server').value,channel:$('exporter-channel').value,kind:$('import-kind').value,
    period:$('import-period').value,date:$('import-date').value,start_time:$('import-start').value,end_time:$('import-end').value,
    timezone:$('import-timezone').value,members};
  try { localStorage.setItem('chatpick-report-preferences',JSON.stringify(importPreferences)); } catch { /* 本轮仍可使用 */ }
}

function importTimeRequest() {
  return $('import-period').value !== 'day' ? {start_time:$('import-start').value,end_time:$('import-end').value,timezone:$('import-timezone').value} :
    {date:$('import-date').value,timezone:$('import-timezone').value};
}

function syncImportForm() {
  const custom=$('import-period').value !== 'day', manual=$('import-period').value === 'custom', kind=$('import-kind').value;
  $('import-date-field').hidden=custom; $('import-date').disabled=custom || importBusy || importReading;
  for (const id of ['import-start','import-end']) {
    $(id+'-field').hidden=!manual; $(id).required=manual; $(id).disabled=!manual || importBusy || importReading;
  }
  $('import-end').setCustomValidity(custom && $('import-end').value <= $('import-start').value ? '结束时间必须晚于开始时间。' : '');
  const zone=$('import-timezone').selectedOptions[0].textContent;
  $('import-time-help').textContent=custom ? `${zone} · ${$('import-start').value.replace('T',' ')} → ${$('import-end').value.replace('T',' ')}（结束时间前）` : `${zone} · 所选日期的全天记录。`;
  $('import-options-summary').textContent=$('import-mode').value === 'full' ? '完整阅读' : '精简阅读';
  $('import-member-fields').hidden=kind !== 'member';
  $('import-topic-field').hidden=kind !== 'discussion'; $('import-topic').disabled=kind !== 'discussion';
  $('import-layout-field').hidden=!isPeriodic(kind);
  $('import-generate').textContent={member:'生成成员摘要 →',daily:'生成频道日报 →',weekly:'生成周期报告 →',discussion:'生成讨论摘要 →'}[kind];
  $('import-generation-help').textContent=kind === 'member' ? '多人分别生成；整理闲聊、资讯和观点。' : '整理本频道的闲聊、资讯和观点，保留各人的出处。';
}

function seedImportTimes(day) {
  $('import-start').value=day+'T00:00';
  const next=new Date(day+'T00:00:00Z'); next.setUTCDate(next.getUTCDate()+1);
  $('import-end').value=next.toISOString().slice(0,10)+'T00:00';
}

function localMinute(value,zone) {
  const p=Object.fromEntries(new Intl.DateTimeFormat('en-GB',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(value).map(p=>[p.type,p.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

function quickTimeRange(preset,zone,now=new Date()) {
  const stop=localMinute(now,zone), day=stop.slice(0,10);
  if (preset === 'today') return [day+'T00:00',stop];
  if (preset === 'yesterday') {
    const previous=new Date(day+'T00:00:00Z'); previous.setUTCDate(previous.getUTCDate()-1);
    return [previous.toISOString().slice(0,10)+'T00:00',day+'T00:00'];
  }
  const hours={last24:24,last3:72,last7:168}[preset];
  return [localMinute(new Date(now.getTime()-hours*3600000),zone),stop];
}

function applyImportQuickRange(now=new Date()) {
  const preset=$('import-period').value, zone=$('import-timezone').value;
  if (['day','custom'].includes(preset)) return;
  [$('import-start').value,$('import-end').value]=quickTimeRange(preset,zone,now);
}

function queueImportRangeRead() {
  clearTimeout(importRangeTimer);
  importRangeTimer=setTimeout(()=>{
    if (!accountReportsVisible() || importReading || importBusy || !importFromAccount || !$('exporter-range').checkValidity() ||
        ['import-start','import-end'].includes(document.activeElement?.id)) return;
    task($('exporter-read'),'import-status',()=>prepareImportRecords(importSequence,true));
  },350);
}

function updateImportScope() {
  const showing = usingAccountSource();
  $('bot-reports').hidden=isLive() && $('report-source').value === 'account';
  $('page-imports').hidden=!accountReportsVisible();
  $('bot-workspace').hidden = showing;
  $('import-workspace').hidden = !showing;
  $('account-setup-button').hidden = !showing;
  if (!showing) return;
  const local=accountReportsVisible() && !!importItem && !importFromAccount;
  $('import-account-workspace').hidden=local;
  $('import-file-workspace').hidden=!local;
  $('exporter-use-account').hidden=exporterAccount.status !== 'connected';
  $('exporter-read').hidden=local;
  $('import-server-name').textContent = importItem?.server_name || '选择读取范围';
  $('import-channel-name').textContent = importItem ? '#' + importItem.channel_name : '尚未选择';
  const server=$('exporter-server'), channel=$('exporter-channel');
  $('scope-label').textContent = local ? `${importItem.server_name} / #${importItem.channel_name}` :
    (server.value && channel.value ? `${server.selectedOptions[0].textContent} / #${channel.selectedOptions[0].textContent}` : exporterAccount.status ? '选择服务器和频道' : '正在恢复账号连接…');
}

function importMembers(preserve=false) {
  const checked = new Set(preserve ? [...$('import-members').querySelectorAll('input:checked')].map(n=>n.value) :
    importPreferences.server === importItem?.server && importPreferences.channel === importItem?.channel ? importPreferences.members || [] : []);
  const entries = Object.entries(importItem?.members || {});
  importMemberScope=importItem ? importItem.server+'/'+importItem.channel : '';
  $('import-members').innerHTML = '<legend class="visually-hidden">选择要总结的成员</legend>' + entries.map(([id,name])=>
    `<label class="daily-member" title="${escapeHTML(name)} · ${escapeHTML(id)}"><input type="checkbox" value="${escapeHTML(id)}" ${checked.has(id) ? 'checked' : ''}><span><span class="member-choice-name">${escapeHTML(name)}</span><small>${importItem.member_counts[id]} 条 · ${escapeHTML(id.slice(-4))}</small></span></label>`).join('');
  $('import-member-search').hidden = entries.length < 8;
  $('import-count').textContent = `${importItem?.analysis_count || 0} 条范围内消息 · ${entries.length} 位发言人`;
  $('import-analysis').hidden = !entries.length;
}

function renderImportHistory() {
  const items = importHistory;
  $('import-history-panel').hidden = !items.length;
  $('import-history').innerHTML = items.map(item=>`<button type="button" data-import-report="${escapeHTML(item.id)}" data-import-id="${escapeHTML(item.import_id)}">${escapeHTML(item.window?.label || item.date)} · ${escapeHTML(item.member?.name || {daily:'频道日报',weekly:'周期报告',discussion:'讨论摘要'}[item.kind] || '成员摘要')}${item.timezone === 'America/Los_Angeles' ? ' · 洛杉矶' : item.timezone === 'UTC' ? ' · UTC' : ''}</button>`).join('');
}

async function loadImportHistory(sequence=importSequence) {
  if (!importItem) return;
  const item=importItem;
  const value=await api('/api/import-history?'+new URLSearchParams({server:item.server,channel:item.channel}));
  if (sequence !== importSequence || item !== importItem) return;
  importHistory=value.reports;
  renderImportHistory();
}

function importControls() {
  const busy=importBusy || importReading;
  for (const id of ['report-source','import-file','import-select','import-date','import-timezone','import-period','import-kind','exporter-server','exporter-channel','exporter-read']) $(id).disabled=busy;
  $('exporter-read').disabled=busy || exporterAccount.status !== 'connected';
  $('import-generate').disabled=busy || !importItem || !bootstrap.config.base_url || !bootstrap.config.model;
  syncImportForm();
}

function showImportReport(report) {
  importReport = report;
  const layout=bootstrap.layouts.find(l=>l.id === $('import-layout').value) || currentLayout();
  $('import-result').innerHTML = discordCopyTools()+(isPeriodic(report.kind) ? reportHTML(report,'import-',layout) : summaryHTML(report, null, 'import-'));
  bindRealDelivery(report,$('import-result'));
}

function applyImportItem(item, preserve=false) {
  importItem = item;
  importHistory = [];
  importReport = null;
  $('import-result').textContent = '';
  $('import-file-note').textContent = `${importFromAccount ? '上次读取' : '本地记录'} ${item.analysis_count} 条消息${item.context_count ? `，另 ${item.context_count} 条引用背景` : ''}`;
  importMembers(preserve);
  renderImportHistory();
  updateImportScope();
  importControls();
}

async function importCatalog() {
  const data = await api('/api/imports');
  const previous = importItem?.id;
  options('import-select', {'':'选择本地记录', ...Object.fromEntries(data.items.map(item=>[item.id,`${item.filename} · ${item.server_name} / #${item.channel_name}`]))});
  if (previous && data.items.some(i=>i.id === previous)) $('import-select').value = previous;
  return data.items;
}

async function selectImport(id, keepDate=false) {
  const sequence = ++importSequence;
  importFromAccount = false;
  importReport = null; $('import-result').textContent = '';
  const item = await api('/api/import?' + new URLSearchParams({import_id:id, timezone:$('import-timezone').value}));
  if (sequence !== importSequence) return;
  applyImportItem(item);
  if (!keepDate || !item.dates.includes($('import-date').value)) { $('import-date').value = item.dates.at(-1) || bootstrap.date; seedImportTimes($('import-date').value); }
  $('import-select').value = id;
  await loadImportHistory(sequence);
  message('import-status','本地记录已就绪，可以生成报告。读取和导入均未调用模型。');
}

function renderExporterAccount(state) {
  const enabled = state.enabled === true;
  const connected = enabled && state.status === 'connected';
  if ((exporterAccount.enabled && !enabled) || (exporterAccount.user_id && exporterAccount.user_id !== state.user_id)) {
    invalidateSelectedScope();
  }
  exporterAccount = {...state,status:state.status || 'unconfigured'};
  $('exporter-enable').hidden = enabled;
  $('exporter-disable').hidden = !enabled;
  $('exporter-form').hidden = !enabled;
  $('exporter-token').disabled = !enabled;
  if (!enabled) $('exporter-token').value = '';
  const servers = connected ? state.servers || {} : {};
  const previous = $('exporter-server').value || importPreferences.server;
  options('exporter-server', connected ? servers : {'':'先连接读取账号'});
  if (previous in servers) $('exporter-server').value = previous;
  const server = $('exporter-server').value;
  const previousChannel = $('exporter-channel').value || importPreferences.channel;
  const channels = connected ? state.channels?.[server] || {} : {};
  options('exporter-channel', connected && server ? channels : {'':'先选择服务器'});
  if (previousChannel in channels) $('exporter-channel').value = previousChannel;
  $('exporter-account-state').textContent = !enabled ? '个人账号读取已关闭。' : connected ? `已连接：${state.username}。凭据保存在本机。` : state.has_token ? '读取凭据已保存，请连接以发现服务器。' : '请填写读取凭据并连接。';
  $('exporter-settings').open = !connected;
  importControls();
  updateLiveReadiness();
}

async function prepareImportRecords(sequence=++importSequence, preserve=false) {
  if (!exporterAccount.enabled) throw new Error('请先启用个人账号读取。');
  if (importReading) throw new Error('正在读取记录，请等待完成。');
  applyImportQuickRange(); syncImportForm();
  if (!$('exporter-range').reportValidity()) return null;
  const data = {server:$('exporter-server').value, channel:$('exporter-channel').value,...importTimeRequest()};
  if (!data.server || !data.channel) throw new Error('请先连接账号并选择可读频道。');
  importReading=true;
  saveImportPreferences();
  importItem=null; importReport=null; importHistory=[];
  $('import-file-note').textContent='正在读取所选时间范围；完成后显示这次的消息与引用数量。';
  $('import-analysis').hidden=true; $('import-result').textContent=''; renderImportHistory(); updateImportScope(); importControls();
  try {
  message('import-status','正在后台读取记录；首次使用可能需要准备组件，不会调用模型…');
  const job = await api('/api/export-records', data);
  let state;
  do {
    state = await api('/api/export-records?id=' + encodeURIComponent(job.id));
    if (state.status === 'running') await new Promise(resolve=>setTimeout(resolve,700));
  } while (state.status === 'running');
  if (sequence !== importSequence) return null;
  if (state.status !== 'complete') throw new Error(state.note);
  importFromAccount = true;
  applyImportItem(state.item, preserve);
  await importCatalog();
  await loadImportHistory(sequence);
  await refreshSelectedAccount();
  if (sequence === importSequence) message('import-status', state.item.dates.length ? '记录已自动准备好，选好内容即可生成。' : '这个时间范围没有可分析的发言，可调整周期。');
  return state.item;
  } finally { importReading=false; importControls(); }
}

async function discoverExportChannels() {
  const sequence = ++importSequence;
  importItem = null; importReport = null;
  $('import-analysis').hidden = true; $('import-result').textContent = ''; updateImportScope();
  const server = $('exporter-server').value;
  const value = await api('/api/exporter/channels', {server});
  if (sequence !== importSequence) return;
  exporterAccount.channels ||= {};
  exporterAccount.channels[server] = value.channels;
  options('exporter-channel', value.channels);
  if (server === importPreferences.server && importPreferences.channel in value.channels) $('exporter-channel').value=importPreferences.channel;
  if (Object.keys(value.channels).length && accountReportsVisible()) await prepareImportRecords(sequence);
  else if (Object.keys(value.channels).length) { saveImportPreferences(); await refreshSelectedAccount(); }
  else message('import-status','此服务器中没有当前账号可读的文字频道。',true);
}

async function loadImportPage() {
  if (importReading || importBusy) { updateImportScope(); return; }
  const sequence = ++importSequence;
  const [account, files] = await Promise.all([api('/api/exporter'), importCatalog()]);
  if (sequence !== importSequence || !usingAccountSource()) return;
  if (!importPreferences.server && files.length && files[0].origin?.kind === 'account-export') {
    const last=files[0]; importPreferences={server:last.server,channel:last.channel};
    $('import-date').value=last.origin.date; $('import-timezone').value=last.origin.timezone; seedImportTimes(last.origin.date);
  }
  renderExporterAccount(account);
  if (!accountReportsVisible()) { await refreshSelectedAccount(); return; }
  const pending=pendingReportJob();
  if (pending?.source === 'account' && pending.request?.server === importPreferences.server && pending.request.channel === importPreferences.channel
      && files.some(item=>item.id === pending.request.import_id)) {
    const item=await api('/api/import?'+new URLSearchParams({import_id:pending.request.import_id,timezone:pending.request.timezone || 'Asia/Shanghai'}));
    if (sequence !== importSequence || !accountReportsVisible()) return;
    importFromAccount=item.origin?.kind === 'account-export';
    applyImportItem(item); await loadImportHistory(sequence);
    return;
  }
  if (importItem) { updateImportScope(); await refreshSelectedAccount(); return; }
  const cached=files.find(item=>item.origin?.kind === 'account-export' && item.origin.account_id === account.user_id && item.server === $('exporter-server').value && item.channel === $('exporter-channel').value);
  if (cached) {
    const item=await api('/api/import?'+new URLSearchParams({import_id:cached.id,timezone:$('import-timezone').value}));
    if (sequence !== importSequence || !accountReportsVisible()) return;
    importFromAccount=true; applyImportItem(item); await loadImportHistory(sequence); await refreshSelectedAccount();
    message('import-status','已打开本机上次读取的记录；生成前会补齐所选周期。'); return;
  }
  if (account.status === 'connected' && Object.keys(account.servers || {}).length) await discoverExportChannels();
  else if (files.length) await selectImport(files[0].id);
}

function configureImportUI() {
  $('report-source-field').hidden = false;
  options('import-period',{day:'选择单日',today:'今天',yesterday:'昨天',last24:'最近 1 天（24 小时）',last3:'最近 3 天',last7:'最近 7 天',custom:'指定周期 · 精确到分钟'});
  try { $('report-source').value=localStorage.getItem('chatpick-report-source') || 'bot'; } catch { /* 使用默认 Bot */ }
  $('import-date').value = importPreferences.date || bootstrap.date;
  seedImportTimes($('import-date').value);
  for (const [id,key] of [['import-timezone','timezone'],['import-start','start_time'],['import-end','end_time'],['import-kind','kind'],['import-period','period']]) {
    if (importPreferences[key]) $(id).value=importPreferences[key];
  }
  applyImportQuickRange();
  options('import-layout',Object.fromEntries(bootstrap.layouts.map(l=>[l.id,l.name])));
  $('import-layout').value=bootstrap.default_layout;
  syncImportForm();
  for (const id of ['account-setup-button','connection-account']) $(id).addEventListener('click',event=>task(event.currentTarget,'global-status',async()=>{
    if (activePage !== 'reports') document.querySelector('[data-page="reports"]').click();
    await loadImportPage();
    $('exporter-settings').open=true;
    $('exporter-settings').scrollIntoView({block:'start'});
  }));
  $('report-source').addEventListener('change',event=>{
    ++importSequence; ++reportSequence;
    invalidateSelectedScope();
    try { localStorage.setItem('chatpick-report-source',event.target.value); } catch { /* 本轮仍可使用 */ }
    updateLiveReadiness();
    task(event.target,'global-status',async()=>{
      if (usingAccountSource()) { await loadImportPage(); await refreshSelectedAccount(); }
      await refreshDailyTask(true);
      if (activePage === 'questions') await loadHistory();
      if (activePage === 'alerts') await loadAlerts();
    });
  });
  for (const [id, enabled] of [['exporter-enable', true], ['exporter-disable', false]]) {
    $(id).addEventListener('click', event => task(event.currentTarget, 'import-status', async () => {
      const state = await api('/api/exporter', {enabled});
      if (!enabled) ++importSequence;
      renderExporterAccount(state);
      $('exporter-settings').open = true;
    }));
  }
  $('exporter-form').querySelector('button[type="submit"]').textContent = '保存并连接';
  $('exporter-form').addEventListener('submit',event=>{
    event.preventDefault();
    task(event.submitter,'import-status',async()=>{
      await api('/api/exporter',{token:$('exporter-token').value});
      $('exporter-token').value = '';
      renderExporterAccount(await api('/api/exporter/connect',{}));
      if (accountReportsVisible()) await discoverExportChannels();
    });
  });
  $('exporter-connect').addEventListener('click',event=>task(event.currentTarget,'import-status',async()=>{
    renderExporterAccount(await api('/api/exporter/connect',{}));
    if (accountReportsVisible()) await discoverExportChannels();
  }));
  $('exporter-use-account').addEventListener('click',event=>task(event.currentTarget,'import-status',async()=>{
    importFromAccount=true; importItem=null;
    renderExporterAccount(exporterAccount);
    await discoverExportChannels();
  }));
  $('exporter-clear').addEventListener('click',event=>task(event.currentTarget,'import-status',async()=>{
    ++importSequence;
    renderExporterAccount(await api('/api/exporter',{clear_token:true}));
    importFromAccount=false;
    message('import-status','已清除读取凭据；本地日报仍可查看。');
  }));
  $('exporter-server').addEventListener('change',event=>{
    invalidateSelectedScope();
    task(event.currentTarget,'global-status',async()=>{await discoverExportChannels();await refreshSelectedAccount();await refreshDailyTask(true);});
  });
  $('exporter-channel').addEventListener('change',event=>{
    invalidateSelectedScope();
    saveImportPreferences();
    task(event.currentTarget,'global-status',async()=>{
      if (accountReportsVisible()) await prepareImportRecords();
      await refreshSelectedAccount();await refreshDailyTask(true);
      if (activePage==='questions') await loadHistory();
      if (activePage==='alerts') await loadAlerts();
    });
  });
  $('account-sync-enabled').addEventListener('change',event=>task(event.currentTarget,'global-status',async()=>{
    const {source,...scope}=selectedDataScope();
    await api('/api/account-sync',{...scope,enabled:event.currentTarget.checked});
    await refreshSelectedAccount();
  }));
  $('exporter-range').addEventListener('submit',event=>{event.preventDefault();task(event.submitter,'import-status',()=>prepareImportRecords());});
  $('import-date').addEventListener('change',()=>{
    seedImportTimes($('import-date').value);
    ++importSequence; importReport = null; $('import-result').textContent = '';
    if (importFromAccount) task($('exporter-read'),'import-status',()=>prepareImportRecords(importSequence,true));
  });
  for (const id of ['import-period','import-start','import-end']) $(id).addEventListener('change',()=>{
    if (id === 'import-period') applyImportQuickRange();
    else $('import-period').value='custom';
    syncImportForm(); ++importSequence; importReport=null; $('import-result').textContent='';
    if (!$('exporter-range').checkValidity()) return;
    queueImportRangeRead();
    saveImportPreferences();
  });
  for (const id of ['import-start','import-end']) $(id).addEventListener('blur',queueImportRangeRead);
  $('import-kind').addEventListener('change',()=>{
    if ($('import-kind').value === 'weekly') $('import-period').value='custom';
    syncImportForm(); saveImportPreferences();
  });
  $('import-members').addEventListener('change',saveImportPreferences);
  for (const id of ['import-mode','import-layout']) $(id).addEventListener('change',()=>{
    syncImportForm();
    if (!importReport) return;
    setReadingMode(importReport,$('import-mode').value); showImportReport(importReport);
  });
  $('import-timezone').addEventListener('change',()=>{
    const item=importItem,fromAccount=importFromAccount;
    invalidateSelectedScope();
    applyImportQuickRange(); syncImportForm();
    if (fromAccount) task($('exporter-read'),'import-status',()=>prepareImportRecords());
    else if (item) task($('import-generate'),'import-status',()=>selectImport(item.id,true));
  });
  $('import-file').addEventListener('change',event=>{
    const file = event.target.files[0]; if (!file || importBusy) return;
    const sequence = ++importSequence;
    task($('import-file'),'import-status',async()=>{
      if (file.size > 25*1024*1024) throw new Error('文件超过 25 MiB，请按日期分段导出。');
      message('import-status','正在检查文件身份、范围和时间…');
      const item = await api('/api/imports',{filename:file.name,content:await file.text()});
      if (sequence !== importSequence) return;
      await importCatalog();
      await selectImport(item.id);
    });
  });
  $('import-select').addEventListener('change',event=>{
    if (event.currentTarget.value) task(event.currentTarget,'import-status',()=>selectImport(event.currentTarget.value));
  });
  $('import-member-search').addEventListener('input',event=>{
    const query=event.target.value.trim().toLowerCase();
    $('import-members').querySelectorAll('label').forEach(label=>label.hidden=!label.textContent.toLowerCase().includes(query));
  });
  $('import-report-form').addEventListener('submit',event=>{
    event.preventDefault(); if (!importItem || importBusy || importReading) return;
    const kind=$('import-kind').value;
    const members=kind === 'member' ? [...$('import-members').querySelectorAll('input:checked')].map(n=>n.value) : [''];
    if (!members.length) return message('import-status','请选择至少一位成员。',true);
    const sequence = ++importSequence;
    if (!$('exporter-range').reportValidity()) return;
    applyImportQuickRange(); syncImportForm();
    let request={...importTimeRequest(),kind,period:$('import-period').value !== 'day' ? 'weekly' : 'daily',mode:$('import-mode').value,topic:$('import-topic').value};
    task(event.submitter,'import-status',async()=>{
      importBusy=true; let completed=0;
      importControls();
      try {
        if (importFromAccount && !await prepareImportRecords(sequence,true)) return;
        request={...request,...importTimeRequest()};
        const item=importItem;
        for (const member of members) {
          if (sequence !== importSequence || !accountReportsVisible()) break;
          message('import-status',`正在整理 ${item.members[member] || '本频道'} 的聊天（${completed+1}/${members.length}）…`);
          const report=await reportJob('account',{...request,import_id:item.id,server:item.server,channel:item.channel,member},text=>{
            if (sequence === importSequence) message('import-status',`正在整理 ${item.members[member] || '本频道'}（${completed+1}/${members.length}） · ${text}`);
          });
          completed++;
          if (sequence === importSequence) showImportReport(report);
        }
        if (sequence === importSequence) message('import-status',`已保存 ${completed} 份报告。`);
      } finally {
        importBusy=false;
        importControls();
        if (sequence === importSequence && importItem) {
          await loadImportHistory(sequence);
        }
      }
    });
  });
  $('import-history').addEventListener('click',event=>{
    const button=event.target.closest('[data-import-report]'); if (!button || !importItem) return;
    const item=importItem, sequence=++importSequence;
    task(button,'import-status',async()=>{
      const report=await api('/api/import-report?'+new URLSearchParams({import_id:button.dataset.importId,server:item.server,channel:item.channel,id:button.dataset.importReport}));
      if (sequence !== importSequence) return;
      showImportReport(report); message('import-status','已打开保存的报告，没有调用模型。');
    });
  });
  $('import-result').addEventListener('click',event=>{
    const copy=event.target.closest('[data-copy-discord]');
    if (copy && importReport) { copyReportToDiscord(importReport,copy); return; }
    if (event.target.closest('[data-full-report]') && importReport) {
      setReadingMode(importReport,'full'); showImportReport(importReport); return;
    }
    const button=event.target.closest('[data-source]'); if (!button) return;
    $('import-sources').open=true;
    const source=$('import-source-'+button.dataset.source);
    source?.scrollIntoView({behavior:'instant',block:'center'});
    if (source) { source.tabIndex=-1; source.focus({preventScroll:true}); }
  });
}
