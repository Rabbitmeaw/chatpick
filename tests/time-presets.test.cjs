const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('web/import-ui.js','utf8');
const functions = source.slice(source.indexOf('function localMinute('),source.indexOf('function queueImportRangeRead('));

function range(preset, zone, now) {
  const fields={'import-period':{value:preset},'import-timezone':{value:zone},'import-start':{value:''},'import-end':{value:''}};
  const context=vm.createContext({Intl,Date,$:id=>fields[id]});
  vm.runInContext(functions+'\napplyImportQuickRange(new Date('+JSON.stringify(now)+'));',context);
  return [fields['import-start'].value,fields['import-end'].value];
}

test('quick ranges distinguish local calendar days from rolling 24/72/168 hours',()=>{
  const now='2026-10-22T02:15:42Z';
  assert.deepEqual(range('today','America/Los_Angeles',now),['2026-10-21T00:00','2026-10-21T19:15']);
  assert.deepEqual(range('yesterday','America/Los_Angeles',now),['2026-10-20T00:00','2026-10-21T00:00']);
  assert.deepEqual(range('last24','Asia/Shanghai',now),['2026-10-21T10:15','2026-10-22T10:15']);
  assert.deepEqual(range('last3','UTC',now),['2026-10-19T02:15','2026-10-22T02:15']);
  assert.deepEqual(range('last7','UTC',now),['2026-10-15T02:15','2026-10-22T02:15']);
});

test('rolling ranges keep elapsed hours across daylight saving changes',()=>{
  assert.deepEqual(range('last24','America/Los_Angeles','2026-11-02T00:30:00Z'),['2026-10-31T17:30','2026-11-01T16:30']);
  assert.deepEqual(range('yesterday','America/Los_Angeles','2026-11-02T20:00:00Z'),['2026-11-01T00:00','2026-11-02T00:00']);
});

test('quick ranges hide manual fields without changing their request window, custom ranges remain editable',()=>{
  const fields={};
  for (const id of ['import-period','import-kind','import-date-field','import-date','import-start','import-end','import-start-field','import-end-field','import-time-help','import-options-summary','import-mode','import-member-fields','import-topic-field','import-topic','import-layout-field','import-generate','import-generation-help']) fields[id]={value:'',setCustomValidity(){}};
  fields['import-timezone']={value:'America/Los_Angeles',selectedOptions:[{textContent:'洛杉矶时间'}]};
  fields['import-kind'].value='daily';fields['import-mode'].value='compact';
  fields['import-period'].value='last24';fields['import-start'].value='2026-10-21T06:41';fields['import-end'].value='2026-10-22T06:41';
  const context=vm.createContext({$:id=>fields[id],importBusy:false,importReading:false,isPeriodic:kind=>['daily','weekly'].includes(kind)});
  vm.runInContext(source.slice(source.indexOf('function importTimeRequest('),source.indexOf('function seedImportTimes(')),context);
  context.syncImportForm();
  const request=JSON.parse(JSON.stringify(context.importTimeRequest()));
  assert.equal(fields['import-start-field'].hidden,true);assert.equal(fields['import-start'].disabled,true);
  assert.deepEqual(request,{start_time:'2026-10-21T06:41',end_time:'2026-10-22T06:41',timezone:'America/Los_Angeles'});
  assert.match(fields['import-time-help'].textContent,/洛杉矶时间.*2026-10-21 06:41 → 2026-10-22 06:41/);
  fields['import-period'].value='custom';context.syncImportForm();
  assert.equal(fields['import-start-field'].hidden,false);assert.equal(fields['import-start'].disabled,false);
  assert.equal(fields['import-end'].required,true);
  assert.deepEqual(JSON.parse(JSON.stringify(context.importTimeRequest())),request);
  fields['import-period'].value='day';fields['import-date'].value='2026-10-21';context.syncImportForm();
  assert.equal(fields['import-date-field'].hidden,false);assert.equal(fields['import-date'].disabled,false);
  assert.equal(fields['import-start-field'].hidden,true);
  assert.deepEqual(JSON.parse(JSON.stringify(context.importTimeRequest())),{date:'2026-10-21',timezone:'America/Los_Angeles'});
});

test('member rendering focuses on chat and keeps old conclusions as optional details',()=>{
  const app=fs.readFileSync('web/app.js','utf8');
  const context=vm.createContext({parentReport:null,escapeHTML:String,sourceButtons:()=>'',sourcesHTML:()=>'',coverageHTML:()=>'',quoteHTML:()=>''});
  vm.runInContext(app.slice(app.indexOf('function topicBodyHTML('),app.indexOf('function reportHTML(')),context);
  const report={kind:'member',offline:false,source:{},subject:{name:'成员'},selected_count:1,window:{label:'2026-10-21 10:00 → 2026-10-21 12:30',timezone:'Asia/Shanghai',precision:'minute'},
    digest:{mode:'compact',summaryStats:{messageCount:1,activeUsers:1,topicCount:1},topics:[{title:'一段闲聊',summary:'原本的闲聊内容。',messageCount:1,timeRange:'10:01',messages:[],summarySourceMessageIds:['1'],conclusions:[]}],
      todos:[],unresolved:[],sectionMeta:{topics:{hiddenCount:0},todos:{hiddenCount:0},unresolved:{hiddenCount:0}}}};
  const html=context.summaryHTML(report);
  assert.match(html,/聊了什么/);assert.match(html,/10:00 → 2026-10-21 12:30/);
  assert.doesNotMatch(html,/待办|待跟进|主要结论|未提取到/);
  report.digest.topics[0].conclusions.push({sender:'成员',text:'旧结论仍然保留',sourceMessageIds:['1']});
  const restored=context.summaryHTML(report);
  assert.match(restored,/<details class="summary-section"><summary>补充观点/);
  assert.match(restored,/旧结论仍然保留/);
});

test('a rolling 24-hour range crossing two dates is displayed as 24 hours',()=>{
  const app=fs.readFileSync('web/app.js','utf8'), context=vm.createContext({Date,Math});
  vm.runInContext(app.slice(app.indexOf('function windowSpan('),app.indexOf('function metricsHTML(')),context);
  assert.equal(context.windowSpan({precision:'minute',days:2,start:'2026-10-21T10:24:00Z',end:'2026-10-22T10:24:00Z'}),'24 小时');
  assert.equal(context.windowSpan({precision:'minute',start:'2026-10-21T10:24:00Z',end:'2026-10-21T10:55:00Z'}),'31 分钟');
});

test('automatic preparation after reload keeps saved members until the matching picker exists',()=>{
  const fields={};
  for (const id of ['exporter-server','exporter-channel','import-kind','import-period','import-date','import-start','import-end','import-timezone']) fields[id]={value:''};
  fields['exporter-server'].value='one';fields['exporter-channel'].value='selected';
  fields['import-members']={querySelectorAll:()=>[]};
  const context=vm.createContext({$ :id=>fields[id],localStorage:{setItem(){}},importMemberScope:'',importPreferences:{server:'one',channel:'selected',members:['person']}});
  vm.runInContext(source.slice(source.indexOf('function saveImportPreferences('),source.indexOf('function importTimeRequest(')),context);
  context.saveImportPreferences();assert.deepEqual(Array.from(context.importPreferences.members),['person']);
  fields['exporter-channel'].value='another';context.saveImportPreferences();assert.equal(context.importPreferences.members.length,0);
  fields['exporter-channel'].value='selected';context.importMemberScope='one/selected';context.saveImportPreferences();assert.equal(context.importPreferences.members.length,0);
});
