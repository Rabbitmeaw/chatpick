const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const app=fs.readFileSync('web/app.js','utf8');
const format=fs.readFileSync('web/discord-format.js','utf8');
const layout={id:'test',name:'合成版式',style:'classic',platform:'mobile',order:['hero','topics','actions','important']};
function fixture(kind='daily') {
  const topics=[['chat','看海','乙说周末想去看海。'],['news','产品见闻','甲转述产品消息，未经核实。'],['stock_opinion','XYZ的个人看法','甲认为条件成立才可能上涨，乙不同意。']]
    .map(([section,title,summary],index)=>({section,title,summary,id:String(index),messageCount:1,participantIds:['a'],participants:['甲'],heat:1,timeRange:'10-21 09:15',summarySourceMessageIds:[String(index)],messages:[],conclusions:[]}));
  const digest={mode:'full',hero:{headline:'头条',summary:'今天有观点、消息和旅行闲聊。'},overview:'简短回顾',topics,importantMessages:[],todos:[],unresolved:[],sectionMeta:{},summaryStats:{messageCount:3,activeUsers:2,topicCount:3}};
  for(const key of ['topics','importantMessages','todos','unresolved']) digest.sectionMeta[key]={hiddenCount:0};
  return {kind,server:'100',report_profile:{id:'us-stock-chat',version:1,server:'100'},digest,fullDigest:digest,
    selected_count:3,metrics:{replies:0},dimensions:{},subject:{name:'甲'},window:{label:'2026-10-21',timezone:'UTC',days:1},generation:{},messages:[]};
}
function context() {
  const sandbox=vm.createContext({parentReport:null,escapeHTML:String,sourceButtons:()=>'<button>原文</button>',
    sourcesHTML:()=>'',coverageHTML:()=>'',quoteHTML:()=>'',metricsHTML:()=>'',dimensionsHTML:()=>'',
    isPeriodic:kind=>['daily','weekly'].includes(kind)});
  vm.runInContext(format+'\n'+app.slice(app.indexOf('function serverOverviewHTML('),app.indexOf('function currentLayout(')),sandbox);
  vm.runInContext(app.slice(app.indexOf('function timeLabel('),app.indexOf('function windowSpan(')),sandbox);
  vm.runInContext(app.slice(app.indexOf('function quoteHTML('),app.indexOf('function sourcesHTML(')),sandbox);
  return sandbox;
}
test('daily and member reports use the same scoped section order without duplicate expansions',()=>{
  const sandbox=context();
  for(const kind of ['daily','member']) {
    const report=fixture(kind);
    const html=sandbox.reportHTML(report,'',layout);
    const titles=['今日速览','美股个人观点','消息与见闻','闲聊与互动'];
    for(let i=1;i<titles.length;i++) assert.ok(html.indexOf(titles[i-1])<html.indexOf(titles[i]));
    for(const topic of report.digest.topics) assert.equal(html.split(topic.summary).length-1,1);
    assert.match(html,/原文/);
    report.digest.topics=report.digest.topics.filter(topic=>topic.section==='chat');
    const sparse=sandbox.reportHTML(report,'',layout);
    assert.doesNotMatch(sparse,/美股个人观点|消息与见闻/);
  }
});
test('history, other servers and non-applicable kinds retain the original view',()=>{
  const sandbox=context();
  const report=fixture('member');
  delete report.report_profile;
  assert.match(sandbox.summaryHTML(report),/聊了什么/);
  report.report_profile={id:'us-stock-chat',version:1,server:'another'};
  assert.match(sandbox.summaryHTML(report),/聊了什么/);
  report.report_profile.server='100';report.kind='discussion';
  assert.doesNotMatch(sandbox.summaryHTML(report),/美股个人观点|今日速览/);
  report.kind='daily';report.digest.topics[0].section='unknown';
  assert.match(sandbox.reportHTML(report,'',layout),/其他内容/);
});
test('expanded daily speech uses the same chosen timezone as the report and topic times',()=>{
  const report=fixture();report.window.timezone='America/Los_Angeles';
  report.digest.topics[0].messages=[{id:'1',name:'乙',created:'2026-10-21T09:15:00+00:00',content:'去海边。',stance:'发言'}];
  const html=context().reportHTML(report,'',layout);
  assert.match(html,/02:15/);
  assert.doesNotMatch(html,/17:15/);
});
test('same asset timeline keeps dated summaries and low confidence next to expandable originals',()=>{
  for(const kind of ['daily','member']) {
    const report=fixture(kind),base=report.digest.topics[2];
    base.title='XYZ';base.summary='';base.timeline=[
      {...base,timeline:undefined,title:'先前条件',summary:'甲先前关注条件甲。',timeRange:'10-19 09:15',messages:[],review:{confidence:'low',reasons:['原词含义不明。']}},
      {...base,timeline:undefined,title:'后来补充',summary:'甲后来补充条件乙。',timeRange:'10-21 09:15',messages:[]}
    ];
    report.digest.overviewStyle='topic-index';
    const html=context().reportHTML(report,'',layout);
    assert.ok(html.indexOf('甲先前关注条件甲。')<html.indexOf('甲后来补充条件乙。'));
    assert.match(html,/置信度较低/);assert.match(html,/原词含义不明/);assert.match(html,/展开.*原文/);
    assert.match(html,/本期速览/);assert.doesNotMatch(html,/置信度.*%/);
  }
});
