const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const app=fs.readFileSync('web/app.js','utf8');
const helpers=fs.readFileSync('web/discord-format.js','utf8')+'\n'+app.slice(app.indexOf('function discordCopyTools('),app.indexOf('function message('));
function fixture() {
  const row={id:'300',server:'100',channel:'200',source_channel:'201',author_id:'1',name:'成员',content:'隐藏原文不复制'};
  return {kind:'member',server:'100',channel:'200',server_name:'示例社区',channel_name:'闲聊',subject:{id:'1',name:'成员'},
    selected_count:1,window:{label:'2026-10-21 09:15 → 2026-10-22 10:20',timezone:'America/Los_Angeles'},
    messages:[row],contexts:[],coverage:{status:'partial',note:'一处引用未补齐。'},dimensions:{},
    digest:{mode:'full',overview:'不应重复复制的成员总览',summaryStats:{messageCount:1,activeUsers:1},
      topics:[{title:'出游',summary:'周末去看海，暂时不确定天气。',summarySourceMessageIds:['300'],conclusions:[]}],
      importantMessages:[{content:'摘要页面不展示的重要消息',sourceMessageIds:['300']}],todos:[],unresolved:[],sectionMeta:{topics:{hiddenCount:1}}}};
}
function context(clipboard={writeText:async()=>{}}) {
  const sandbox=vm.createContext({navigator:{clipboard},escapeHTML:String,bootstrap:{servers:{},channels:{}},
    isPeriodic:kind=>kind==='daily' || kind==='weekly',
    discordSourceURL:m=>m.server && m.id ? `https://discord.com/channels/${m.server}/${m.source_channel || m.channel}/${m.id}` : ''});
  vm.runInContext(helpers,sandbox);return sandbox;
}

test('copies only current snapshot summary with minute timezone, coverage and true thread source',()=>{
  const report=fixture(),before=JSON.stringify(report),parts=context().discordReportParts(report,true),text=parts.join('\n');
  assert.match(text,/## 拾聊 · 成员发言摘要 · 成员/);
  assert.match(text,/2026-10-21 09:15 → 2026-10-22 10:20/);assert.match(text,/America\/Los\\_Angeles/);
  assert.match(text,/示例社区/);assert.match(text,/周末去看海/);assert.match(text,/\[原文 1\]\(https:\/\/discord.com\/channels\/100\/201\/300\)/);
  assert.match(text,/引用未补齐/);assert.match(text,/未展示/);
  assert.doesNotMatch(text,/隐藏原文不复制|不应重复复制|摘要页面不展示的重要消息|<button|待办|待跟进|```/);
  assert.equal(JSON.stringify(report),before);
  const plain=context().discordReportParts(report).join('\n');
  assert.doesNotMatch(plain,/discord\.com\/channels|\[原文/);
  assert.match(plain,/周末去看海/);assert.match(plain,/引用未补齐/);
  assert.doesNotMatch(context().discordCopyTools(),/\schecked(?:\s|>)/);
});

test('server-specific grouping is shared with copies, omits empty blocks and leaves other scopes unchanged',()=>{
  const report=fixture();report.kind='daily';report.subject=null;
  const original=context().discordReportParts(report).join('\n');
  report.report_profile={id:'us-stock-chat',version:1,server:'100'};
  report.digest.topics[0].section='chat';report.digest.topics[0].timeRange='10-21 09:15 → 10-21 09:20';
  const opinion={...report.digest.topics[0],title:'XYZ的个人看法',summary:'甲认为只有满足条件才可能上涨。',section:'stock_opinion',conclusions:[{sender:'甲',text:'不重复展开'}]};
  report.digest.topics.push(opinion);
  const text=context().discordReportParts(report).join('\n');
  assert.ok(text.indexOf('今日速览')<text.indexOf('美股个人观点'));
  assert.ok(text.indexOf('美股个人观点')<text.indexOf('闲聊与互动'));
  assert.doesNotMatch(text,/消息与见闻|不重复展开|摘要页面不展示的重要消息/);
  assert.equal((text.match(/甲认为只有满足条件才可能上涨/g)||[]).length,1);
  assert.match(text,/10-21 09:15/);
  assert.match(text,/1 个候选条目未展示/);
  report.digest.topics.pop();report.report_profile.server='another';
  assert.equal(context().discordReportParts(report).join('\n'),original);
  report.report_profile.server='100';report.kind='weekly';
  assert.doesNotMatch(context().discordReportParts(report).join('\n'),/今日速览|美股个人观点|闲聊与互动/);
  delete report.report_profile;report.kind='member';
  assert.doesNotMatch(context().discordReportParts(report).join('\n'),/今日速览|美股个人观点|闲聊与互动/);
});

test('periodic and discussion copies preserve real sections without empty placeholders or unrelated authors',()=>{
  const report=fixture();report.kind='weekly';report.subject=null;report.digest.overview='本期聊了旅行。';
  report.digest.todos=[{task:'带上相机',owner:'小甲',deadline:'周六',sourceMessageIds:['300']}];
  let text=context().discordReportParts(report).join('\n');
  assert.match(text,/## 拾聊 · 周报/);assert.match(text,/本期聊了旅行/);assert.match(text,/明确约定/);assert.match(text,/小甲/);assert.match(text,/周六/);
  report.dimensions={member:{kind:'member',subject:{name:'目标甲'},generation:{status:'complete'},coverage:{note:'仅目标甲发言'},
    digest:{topics:[{title:'甲的看法',summary:'甲认为可以先看天气。',summarySourceMessageIds:['300']}],todos:[],unresolved:[],importantMessages:[]}}};
  text=context().discordReportParts(report).join('\n');assert.match(text,/关注维度 · 成员发言摘要 · 目标甲/);assert.match(text,/甲认为可以先看天气/);
  report.kind='discussion';report.subject={name:'旅行'};
  text=context().discordReportParts(report).join('\n');assert.match(text,/多人讨论摘要 · 旅行/);
});

test('long copies fit regular Discord messages without broken emoji, markdown escapes or source links',()=>{
  const report=fixture();report.digest.topics[0].summary='😀星星 *观点* @everyone <@123456> 原话。'.repeat(160);
  const parts=context().discordReportParts(report,true);
  assert.ok(parts.length>1);assert.ok(parts.every(part=>part.length<=2000));
  for(const part of parts) {
    assert.ok(!part.isWellFormed || part.isWellFormed());
    assert.doesNotMatch(part,/@everyone|<@123456>|(?<!\\)\*观点\*/);
    assert.doesNotMatch(part,/\\$/m);
  }
  const text=parts.join('\n');assert.equal((text.match(/原话。/g)||[]).length,160);
  assert.match(text,/\[原文 1\]\(https:\/\/discord.com\/channels\/100\/201\/300\)/);
});
test('dated asset entries and qualitative confidence survive copy with sources still optional',()=>{
  const report=fixture();report.kind='daily';report.report_profile={id:'us-stock-chat',version:1,server:'100'};
  report.digest.overviewStyle='topic-index';
  report.digest.topics=[{title:'XYZ',summary:'',section:'stock_opinion',timeline:[
    {title:'较早条件',timeRange:'10-19 09:15',summary:'甲的较早看法。',summarySourceMessageIds:['300'],review:{confidence:'low',reasons:['未解释的原词。']}},
    {title:'后来条件',timeRange:'10-21 09:15',summary:'甲的后来补充。',summarySourceMessageIds:['300']}
  ]}];
  const text=context().discordReportParts(report).join('\n');
  assert.ok(text.indexOf('甲的较早看法')<text.indexOf('甲的后来补充'));
  assert.match(text,/10-19 09:15/);assert.match(text,/10-21 09:15/);assert.match(text,/置信度较低/);
  assert.match(text,/未解释的原词/);assert.doesNotMatch(text,/discord\.com\/channels/);
  assert.match(context().discordReportParts(report,true).join('\n'),/discord\.com\/channels/);
});

test('copy advances only after clipboard succeeds, and a denied clipboard keeps the same part',async()=>{
  const report=fixture();report.digest.topics[0].summary='原话😀。'.repeat(1000);
  const writes=[];let denied=false;
  const sandbox=context({writeText:async text=>{if(denied)throw new Error('denied');writes.push(text);}});
  const status={textContent:''},button={disabled:false,dataset:{},textContent:'复制到 Discord',
    closest:()=>({querySelector:()=>status})};
  await sandbox.copyReportToDiscord(report,button);
  assert.equal(writes.length,1);assert.match(status.textContent,/第 1/);assert.match(button.textContent,/第 2/);
  const next=button.dataset.copyPart;denied=true;await sandbox.copyReportToDiscord(report,button);
  assert.equal(button.dataset.copyPart,next);assert.equal(writes.length,1);assert.match(status.textContent,/未能复制/);
  assert.equal(button.disabled,false);
});

test('desktop copy uses the native text bridge and preserves the browser fallback',async()=>{
  const report=fixture(),writes=[];
  const sandbox=context({writeText:async()=>{throw new Error('native bridge must be used');}});
  sandbox.window={chatpickCopyText:async text=>writes.push(text)};
  const status={textContent:''},button={disabled:false,dataset:{},textContent:'复制到 Discord',
    closest:()=>({querySelector:()=>status})};
  await sandbox.copyReportToDiscord(report,button);
  assert.equal(writes.length,1);assert.match(writes[0],/周末去看海/);
  assert.match(status.textContent,/已复制/);
  const before=button.dataset.copyPart;
  sandbox.window.chatpickCopyText=async()=>{throw new Error('bridge rejected');};
  await sandbox.copyReportToDiscord(report,button);
  assert.equal(button.dataset.copyPart,before);assert.match(status.textContent,/未能复制/);
});

test('the checkbox controls copied sources and changing it restarts multipart copying',async()=>{
  const report=fixture(),writes=[],status={textContent:''},option={checked:false,disabled:false};
  const button={disabled:false,dataset:{copyPart:'0'},textContent:'复制到 Discord'};
  const tools={querySelector:selector=>selector==='[data-copy-sources]' ? option : selector==='[data-copy-discord]' ? button : status};
  button.closest=option.closest=()=>tools;
  const sandbox=context({writeText:async text=>writes.push(text)});
  await sandbox.copyReportToDiscord(report,button);
  assert.doesNotMatch(writes[0],/discord\.com\/channels/);
  button.dataset.copyPart='3';button.textContent='复制第 4/5 段';option.checked=true;
  sandbox.resetDiscordCopyOptions(option);
  assert.equal(button.dataset.copyPart,'0');assert.equal(button.textContent,'复制到 Discord');assert.equal(status.textContent,'');
  await sandbox.copyReportToDiscord(report,button);
  assert.match(writes[1],/discord\.com\/channels\/100\/201\/300/);
  assert.equal(option.disabled,false);
});
