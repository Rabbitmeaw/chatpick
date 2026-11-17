const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/import-ui.js','utf8');

function setup(api=async()=>({})) {
  const fields={'report-source':{value:'account'},'exporter-server':{value:'300'},'exporter-channel':{value:'400'},'import-timezone':{value:'America/Los_Angeles'},'question-history':{value:''}};
  const context=vm.createContext({api,isLive:()=>true,activePage:'questions',scopeSequence:0,
    bootstrap:{selected_server:'100',selected_channel:'200',members:{bot:'Bot成员'}},
    $:id=>fields[id],options:(id,values)=>{fields[id].choices=values;},localStorage:{getItem:()=>null},URLSearchParams});
  vm.runInContext(source.slice(0,source.indexOf('function importMembers(')),context);
  vm.runInContext("exporterAccount={status:'connected',user_id:'900'};",context);
  return {context,fields};
}

test('all personal pages keep the selected personal account and channel rather than Bot scope',()=>{
  const {context,fields}=setup();
  for (const page of ['reports','questions','activity','alerts']) {
    context.activePage=page;
    assert.deepEqual(JSON.parse(JSON.stringify(context.selectedDataScope())),{
      server:'300',channel:'400',source:'account',account_id:'900',timezone:'America/Los_Angeles'});
  }
  fields['report-source'].value='bot';
  assert.deepEqual(JSON.parse(JSON.stringify(context.selectedDataScope())),{server:'100',channel:'200'});
});

test('late account metadata cannot replace the new source channel',async()=>{
  let resolve;
  const {context,fields}=setup(()=>new Promise(done=>{resolve=done;}));
  const pending=context.refreshSelectedAccount();
  fields['exporter-channel'].value='401';context.scopeSequence++;
  resolve({members:{old:'旧频道成员'}});await pending;
  assert.equal(vm.runInContext('accountScopeData',context),null);
});

test('personal question history requests include identity and never silently use Bot data',async()=>{
  const requests=[];let items=[];
  const {context,fields}=setup(async(path)=>{requests.push(path);return {items};});
  context.questionConversation='';
  const app=fs.readFileSync('web/app.js','utf8');
  vm.runInContext(app.slice(app.indexOf('async function loadHistory('),app.indexOf('async function restoreQuestions(')),context);
  await context.loadHistory();
  const params=new URL(requests[0],'http://localhost').searchParams;
  assert.equal(params.get('server'),'300');assert.equal(params.get('channel'),'400');
  assert.equal(params.get('source'),'account');assert.equal(params.get('account_id'),'900');
  assert.equal(fields['question-history'].choices[''],'还没有问答记录');
  assert.equal(fields['question-history'].disabled,true);
  items=[{id:'conversation-one',title:'之前的问题',start_date:'2026-10-21',end_date:'2026-10-22'}];
  await context.loadHistory();
  assert.equal(fields['question-history'].choices[''],'查看以前的问答');
  assert.equal(fields['question-history'].disabled,false);
  assert.match(fields['question-history'].choices['conversation-one'],/之前的问题/);
});

test('changing personal scope removes the old report and selections before another page can reuse them',()=>{
  const {context,fields}=setup();
  for (const id of ['import-result','report-output','import-members','import-analysis','activity-output','alerts-output','alert-rule-list','daily-panel','import-history','import-history-panel']) fields[id]={textContent:'旧频道内容',hidden:false};
  context.activitySequence=0;context.questionSequence=0;
  context.lastReport={};context.parentReport={};
  context.resetDailyScope=()=>{};context.clearQuestions=()=>{};context.renderImportHistory=()=>{};
  vm.runInContext("importItem={server:'300',channel:'400'}; importReport={report_id:'old'}; importHistory=[];",context);
  context.invalidateSelectedScope();
  assert.equal(vm.runInContext('importItem',context),null);
  assert.equal(vm.runInContext('importReport',context),null);
  assert.equal(fields['import-result'].textContent,'');assert.equal(fields['import-members'].textContent,'');
  assert.equal(fields['import-analysis'].hidden,true);
});

test('personal credential form stays closed until explicitly enabled',()=>{
  const {context,fields}=setup();
  for (const id of ['exporter-settings','exporter-account-state','exporter-enable','exporter-disable','exporter-form','exporter-token']) fields[id]={};
  context.options=()=>{};context.importControls=()=>{};
  let resets=0;context.invalidateSelectedScope=()=>{resets++;};
  let ready=false;context.updateLiveReadiness=()=>{ready=true;};
  const code=source.slice(source.indexOf('function renderExporterAccount('),source.indexOf('async function prepareImportRecords('));
  vm.runInContext("exporterAccount={};",context);
  vm.runInContext(code,context);context.renderExporterAccount({enabled:false,status:'disabled',has_token:false,storage:'macos-keychain'});
  assert.equal(fields['exporter-settings'].open,true);
  assert.equal(ready,true);
  assert.equal(fields['exporter-form'].hidden,true);
  assert.equal(fields['exporter-token'].disabled,true);
  assert.equal(fields['exporter-enable'].hidden,false);
  context.renderExporterAccount({enabled:true,status:'saved',has_token:false,storage:'macos-keychain'});
  assert.equal(fields['exporter-form'].hidden,false);
  assert.equal(fields['exporter-token'].disabled,false);
  assert.equal(fields['exporter-disable'].hidden,false);
  context.renderExporterAccount({enabled:false,status:'disabled',has_token:false,storage:'macos-keychain'});
  assert.equal(fields['exporter-form'].hidden,true);
  assert.equal(fields['exporter-token'].disabled,true);
  assert.equal(resets,1);
});
