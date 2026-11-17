'use strict';

function isLive() { return Boolean(bootstrap && bootstrap.offline === false); }

let autoSyncState = null;
let autoSyncVersion = null;
let autoSyncPolling = false;

function renderAutoSync(state) {
  if (state.server !== bootstrap.selected_server || state.channel !== bootstrap.selected_channel) return;
  autoSyncState = state;
  const time = state.finished ? new Date(state.finished).toLocaleTimeString('zh-CN',{hour12:false}) : '';
  const threadGap = state.status === 'complete' && state.threads_complete === false;
  const text = !state.enabled ? '后台同步已暂停' : state.status === 'failed' ? '同步暂未完成 · 将自动重试' :
    threadGap ? '部分记录已同步 · 子线程未补齐' :
    state.status === 'complete' ? `已同步${time ? ' · ' + time : ''}` : '正在自动同步记录…';
  $('auto-sync-status').hidden = false;
  $('auto-sync-status').textContent = text;
  $('auto-sync-status').title = state.note || text;
  $('auto-sync-status').classList.toggle('sync-failed',state.status === 'failed' || threadGap);
  $('auto-sync-detail').textContent = `${text}。${state.note || ''}`;
  $('auto-sync-toggle').textContent = state.enabled ? '暂停后台同步' : '恢复后台同步';
}

async function refreshLiveMembers(sequence) {
  const data = await api('/api/bootstrap?' + new URLSearchParams({server:bootstrap.selected_server,channel:bootstrap.selected_channel,remember:'0'}));
  if (sequence !== scopeSequence) return;
  bootstrap.members = data.members;
  bootstrap.sync = data.sync;
  for (const id of ['member','focus-member','rule-member']) {
    const previous = $(id).value;
    options(id,id === 'member' ? data.members : {'':id==='rule-member' ? '所有成员' : '不额外关注',...data.members});
    if (previous in data.members || id === 'focus-member' && !previous) $(id).value = previous;
  }
  renderDailyMembers(false);
}

async function pollLiveSync() {
  if (!isLive() || autoSyncPolling || scopeLoading || !bootstrap.selected_channel) return;
  autoSyncPolling = true;
  const sequence = scopeSequence;
  try {
    const state = await api('/api/auto-sync');
    if (sequence !== scopeSequence || state.channel !== bootstrap.selected_channel || state.server !== bootstrap.selected_server) return;
    renderAutoSync(state);
    if (state.status === 'complete' && state.version !== autoSyncVersion) {
      await refreshLiveMembers(sequence);
      if (sequence === scopeSequence) autoSyncVersion = state.version;
      if (sequence === scopeSequence && activePage==='alerts') await loadAlerts();
    }
  } catch {
    if (sequence === scopeSequence) $('auto-sync-status').textContent = '无法连接本机服务，请重新启动拾聊';
  } finally { autoSyncPolling = false; }
}

async function activateLiveSync(enabled) {
  if (!isLive() || !bootstrap.selected_channel) return;
  const sequence = scopeSequence;
  const data = {server:bootstrap.selected_server,channel:bootstrap.selected_channel};
  if (enabled !== undefined) data.enabled = enabled;
  const state = await api('/api/auto-sync',data);
  if (sequence !== scopeSequence) return;
  autoSyncVersion = null;
  renderAutoSync(state);
}

function updateLiveReadiness() {
  if (!isLive()) return;
  const selected=selectedDataScope();
  const scoped = Boolean(selected.server && selected.channel && (selected.source !== 'account' || selected.account_id));
  const configured = Boolean(bootstrap.config.base_url && bootstrap.config.model);
  $('server').disabled = !Object.keys(bootstrap.servers).length;
  $('channel').disabled = !Object.keys(bootstrap.channels).length;
  $('scope-label').textContent = scoped ? `${bootstrap.servers[bootstrap.selected_server]} / #${bootstrap.channels[bootstrap.selected_channel]}` : '尚未选择真实频道';
  const personal=usingAccountSource();
  $('connection-guide').hidden = personal && !exporterAccount.status || scoped && configured || activePage !== 'reports';
  $('connection-account').hidden=!personal || scoped;
  $('connection-bot').hidden=personal || scoped;
  $('connection-model').hidden=configured;
  $('connection-guide-note').textContent=!scoped ? (personal && exporterAccount.status === 'connected' ? '账号已连接，请在左侧选择服务器和频道。' : '先连接 Discord，再选择你想总结的频道。') : '频道已就绪。填写模型服务提供的地址、模型名和密钥后即可生成。';
  $('generate-report').disabled = !scoped || !configured;
  $('ask-question').disabled = !scoped || !configured || questionBusy;
  $('server-help').textContent = scoped ? '自动同步当前频道及可读子线程。' : '到Bot接入页连接，并发现服务器和可读频道。';
  $('sync-scope').textContent = scoped ? `当前范围：${bootstrap.servers[bootstrap.selected_server]} / #${bootstrap.channels[bootstrap.selected_channel]}。只读同步，不调用模型。` : '请先连接Bot，并发现服务器中的可读频道。';
  $('report-delivery').hidden = true;
  const previous = $('sync-bot').value;
  const eligible = botList.filter(bot => bot.status === 'connected' && bot.channels?.[bootstrap.selected_server]?.[bootstrap.selected_channel]);
  options('sync-bot', eligible.length ? Object.fromEntries(eligible.map(bot => [bot.id,bot.name])) : {'':'先连接并发现可读频道'});
  if (eligible.some(bot => bot.id === previous)) $('sync-bot').value = previous;
  $('start-sync').disabled = !scoped || !eligible.length;
  const last = bootstrap.sync;
  $('continue-sync').hidden = !(last?.status === 'partial' && last.bot_id === $('sync-bot').value &&
    last.start_date === $('sync-start').value && last.end_date === $('sync-end').value);
  if (typeof updateImportScope === 'function') updateImportScope();
}

function resetLiveBotForm(bot=null) {
  $('bot-id').value = bot?.id || '';
  $('bot-name').value = bot?.name || '';
  $('bot-token').value = '';
  $('clear-bot-token').checked = false;
  $('bot-form-title').textContent = bot ? '编辑 Bot' : '添加 Bot';
  $('bot-scope').textContent = '保存后，手动连接并按名称选择服务器与频道。';
  $('bot-token-state').textContent = bot?.has_token ? '已保存Token，留空不会覆盖；不会回传完整Token。' :
    (bootstrap.config.storage === 'macos-keychain' ? 'Token保存到本机系统钥匙串，不写入消息库。' : 'Token仅在本次运行期间保留，关闭服务后需要重填。');
  $('bot-status').hidden = true;
}

function renderLiveBots() {
  $('bot-list').innerHTML = botList.length ? botList.map(bot => `<article class="live-bot-card"><div class="section-head"><strong>${escapeHTML(bot.name)}</strong><button type="button" data-edit-bot="${escapeHTML(bot.id)}">编辑</button></div><p class="hint">${bot.status === 'connected' ? '身份已验证：' + escapeHTML(bot.username) : '尚未连接'}${bot.message_content === false && bot.status === 'connected' ? ' · 需启用Message Content Intent' : ''}</p><button type="button" data-connect-bot="${escapeHTML(bot.id)}">连接并发现服务器</button>${bot.status === 'connected' ? `<label>服务器<select id="bot-server-${escapeHTML(bot.id)}">${Object.entries(bot.servers).map(([id,name]) => `<option value="${escapeHTML(id)}">${escapeHTML(name)}</option>`).join('') || '<option value="">尚未加入服务器</option>'}</select></label><button type="button" data-discover-bot="${escapeHTML(bot.id)}" ${Object.keys(bot.servers).length ? '' : 'disabled'}>发现可读频道</button><p class="hint"><a href="https://discord.com/oauth2/authorize?client_id=${encodeURIComponent(bot.application_id)}&scope=bot&permissions=66560" target="_blank" rel="noopener noreferrer">邀请此Bot加入服务器 ↗</a> · 仅申请查看频道和读取历史</p>` : ''}</article>`).join('') : '<div class="empty"><strong>添加你的第一个 Bot</strong>填写名称和Bot Token后保存，再点击连接。</div>';
  updateLiveReadiness();
}

async function refreshLiveScope(server=bootstrap.selected_server, channel=bootstrap.selected_channel) {
  ++scopeSequence;
  clearReport(); clearQuestions();
  bootstrap = await api('/api/bootstrap?' + new URLSearchParams({server:server || '',channel:channel || ''}));
  fillScope();
  await loadBots();
  await activateLiveSync();
  updateLiveReadiness();
}

async function runLiveSync(resume=false) {
  if (scopeLoading || !$('sync-form').reportValidity()) return;
  const scope = scopeSequence;
  const data = {bot_id:$('sync-bot').value,server:bootstrap.selected_server,channel:bootstrap.selected_channel,
    start_date:$('sync-start').value,end_date:$('sync-end').value,max_pages:Number($('sync-pages').value),resume};
  $('continue-sync').hidden = true;
  message('sync-status','正在核验Bot身份与频道权限，只读同步，不调用模型。');
  const job = await api('/api/sync',data);
  let state;
  do {
    state = await api('/api/sync?id=' + encodeURIComponent(job.id));
    if (scope === scopeSequence) message('sync-status',`已读取 ${state.pages || 0} 页、${state.messages || 0} 条范围内消息。${state.note || '正在同步…'}`,state.status === 'failed');
    if (state.status === 'running') await new Promise(resolve => setTimeout(resolve,900));
  } while (state.status === 'running');
  if (scope !== scopeSequence) return;
  await refreshLiveScope(data.server,data.channel);
  $('sync-start').value = data.start_date; $('sync-end').value = data.end_date;
  $('question-start').value = data.start_date; $('question-end').value = data.end_date;
  $('date').value = $('kind').value === 'daily' ? data.end_date : data.start_date;
  $('range-end').value = data.end_date;
  syncDateRange(); updateLiveReadiness();
  message('sync-status',`${state.status === 'complete' ? '本次同步完成' : state.status === 'partial' ? '同步尚未完成，可继续' : '同步失败'}：${state.messages || 0} 条，${state.pages || 0} 页。${state.note || ''}`,state.status === 'failed');
}

function configureLiveUI() {
  for (const [value,label] of Object.entries({today:'今天',yesterday:'昨天',last24:'最近 1 天（24 小时）',last3:'最近 3 天'})) {
    const option=document.createElement('option'); option.value=value; option.textContent=label; $('range-preset').append(option);
  }
  document.title = '拾聊 ChatPick · 频道记录与总结';
  document.querySelector('.topbar>.badge').textContent = '真实接入';
  document.querySelector('.side-note').innerHTML = '<span class="dot"></span> 本机运行<p>先选频道与时间，再生成。<br>结果与原文保存在本机。</p>';
  $('source-notice').innerHTML = '<strong>仅当前频道</strong><span>生成时调用你配置的模型，可能产生费用。记录和报告保存在本机。</span>';
  document.querySelector('#page-reports .page-stamp').innerHTML = '真实模型<br><small>手动生成 · 附原文出处</small>';
  $('layout-help').textContent = '切换仅改变展示，使用同一份报告，不重新调用模型。';
  document.querySelector('#page-questions .page-heading>.badge').textContent = '真实模型 · 本频道';
  document.querySelector('#page-bots .page-heading p').textContent = '首次配置一次，之后选定频道即可自动同步。';
  document.querySelector('#page-bots .page-heading>.badge').textContent = '只读接入 · 自动同步';
  $('bot-notice').innerHTML = '<strong>先保存，再手动连接</strong><p>填写Bot Token后保存，不会自动请求Discord。点击连接会读取Bot身份及服务器列表；同步只读取所选频道，不发送消息。</p>';
  $('live-bot-help').hidden = false; $('live-sync').hidden = false;
  document.querySelector('#bot-form fieldset').hidden = true;
  document.querySelector('#bot-form details').hidden = true;
  $('bot-token').parentElement.firstChild.textContent = 'Bot Token';
  $('bot-token').placeholder = '粘贴Bot Token，留空保留已保存值';
  $('clear-bot-token').parentElement.lastChild.textContent = '清除已保存的Token';
  document.querySelector('#bot-form button.primary').textContent = '保存 Bot 配置';
  document.querySelector('#bot-list').nextElementSibling.textContent = '支持多个Bot。服务器和频道从连接结果中按名称选择，无需手填编号。';
  document.querySelector('#bot-list').previousElementSibling.querySelector('h2').textContent = '已保存的 Bot';
  $('runtime-help').innerHTML = '<summary>数据和运行说明</summary><p>记录来自Bot有权读取的服务器频道，保存在本机；不读取Discord客户端缓存。选定频道后自动补齐近期记录，并在本机服务运行时持续读取新增消息，网页可以关闭。退出服务、断网或电脑休眠会暂停，恢复后继续；生成或提问前自动补齐所选日期。</p><p>只同步所选频道及直属可读子线程的文字，不读取其他频道；未加入的归档私密线程、附件、图片、语音和外链正文不读取。权限不足、缺少引用或同步未完成会显示覆盖状态。近期编辑会随读取更新，较早的编辑及删除需在高级同步中重新完整同步。</p>';
  $('model-notice').innerHTML = '<strong>配置你的模型服务</strong><p>保存不会调用模型。测试连接只发送固定文本；生成报告和问答会发送所选范围的聊天原文，可能产生费用。</p>';
  document.querySelector('label[for="api-key"]').innerHTML = '密钥 <span class="english">API Key</span>';
  $('clear-key').parentElement.lastChild.textContent = '清除已保存的密钥';
  $('model-help').textContent = '填写服务商提供的模型名称，可直接输入，无需先读取模型列表。';
  $('check-config').textContent = '测试模型连接';
  $('live-model-advanced').hidden = false;
  for (const field of $('live-model-advanced').querySelectorAll('input,select')) field.disabled = false;
  $('advanced-help').textContent = '兼容性参数按服务商要求设置。只重试明确的限流响应；超时等未知结果不自动重试。';
  for (const [id,placeholder] of [['topic','例如：版本发布；默认总结全部讨论'],['focus-topic','可选，例如：新手体验']]) {
    const previous = $(id), input = document.createElement('input');
    input.id = id; input.name = previous.name; input.disabled = previous.disabled;
    input.placeholder = placeholder; input.maxLength = 200;
    input.value = id === 'topic' ? '本频道全部讨论' : '';
    previous.replaceWith(input);
  }
  const questionHelp = document.querySelector('#question-form details');
  questionHelp.innerHTML = '<summary>问答说明与长度保护</summary><p class="hint">提问前自动补齐指定日期，只检索当前频道。原引擎每轮最多提供40条编号证据，输入长度也受预算约束；选入原文和引用不足以覆盖全部时会明确提示。输入预算可在模型高级设置中调整，单位为UTF-8字节长度，不是Token数。</p><p class="hint">一轮为一次提问和一次回答。每次最多携带最近6轮的裁短背景；聊天更新后自动重新检索并清除旧回答背景，无需手动新建对话。历史保留全文，但不会全部进入模型；答案须有本轮原文依据。</p>';
  document.querySelectorAll('[data-question]').forEach((button,index) => {
    button.dataset.question = index ? '大家对主要话题有哪些不同看法？' : '这个频道最近主要聊了什么？';
    button.textContent = index ? '不同看法' : '最近聊了什么';
  });
  $('question-input').placeholder='例如：这几天大家怎么看某个话题？';
  document.querySelector('#page-alerts .page-heading p').textContent = '保存关注，后续新发言和编辑命中时自动更新；只保存在本机。';
  $('alert-rule-form').hidden=false;$('alert-rule-list').hidden=false;
  $('alert-history-panel').open=false;
  $('alert-history-panel').querySelector('summary').textContent='查看历史中的匹配';
  $('alerts-form').querySelector('button').textContent='查看历史匹配';
  document.querySelector('#alerts-form .hint').textContent = '只查看所选日期的关键词命中，不把历史消息计为新提醒，不调用模型或向Discord投递。';
  document.querySelector('footer').textContent = '拾聊 ChatPick · 本机服务 · Discord只读';
  for (const id of ['sync-start','sync-end']) $(id).value = bootstrap.question_defaults[id === 'sync-start' ? 'start_date' : 'end_date'];
  document.querySelectorAll('[data-open-page]').forEach(button => button.addEventListener('click',() =>
    document.querySelector(`[data-page="${button.dataset.openPage}"]`).click()));
  $('bot-list').addEventListener('click',event => {
    const connect = event.target.closest('[data-connect-bot]'), discover = event.target.closest('[data-discover-bot]');
    if (!connect && !discover) return;
    const button = connect || discover;
    task(button,'global-status',async () => {
      if (connect) {
        const bot = await api('/api/bots/connect',{bot_id:connect.dataset.connectBot});
        await refreshLiveScope();
        message('global-status',`已验证 ${bot.username}。${Object.keys(bot.servers).length ? `发现 ${Object.keys(bot.servers).length} 个服务器，请选择服务器并发现可读频道。` : '该Bot尚未加入服务器。请点击卡片中的邀请链接，完成后再次连接。'}${bot.message_content ? '' : '需在Discord启用Message Content Intent后才能读取正文。'}`);
      } else {
        const bot_id = discover.dataset.discoverBot, server = $('bot-server-' + bot_id).value;
        const found = await api('/api/discord/channels',{bot_id,server});
        await refreshLiveScope(server,Object.keys(found.channels)[0] || '');
        message('global-status',Object.keys(found.channels).length ? `已发现 ${Object.keys(found.channels).length} 个可读文字频道。左侧选定频道后会自动同步，可直接生成或提问。` : '未发现有查看和读取历史权限的文字频道，请在Discord配置权限后重试。');
      }
    });
  });
  $('sync-form').addEventListener('submit',event => { event.preventDefault(); task(event.submitter,'sync-status',() => runLiveSync(false)); });
  $('continue-sync').addEventListener('click',event => task(event.currentTarget,'sync-status',() => runLiveSync(true)));
  for (const id of ['sync-start','sync-end','sync-bot']) $(id).addEventListener('change',() => {
    $('sync-end').setCustomValidity($('sync-end').value < $('sync-start').value ? '结束日期不能早于开始日期。' : '');
    updateLiveReadiness();
  });
  $('auto-sync-toggle').addEventListener('click',event => task(event.currentTarget,'sync-status',() => activateLiveSync(!autoSyncState?.enabled)));
  $('auto-sync-retry').addEventListener('click',event => task(event.currentTarget,'sync-status',() => activateLiveSync(true)));
  configureDailyUI();
  configureImportUI();
  setInterval(pollLiveSync,2000);
}
