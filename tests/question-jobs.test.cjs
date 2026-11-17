const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const helpers=source.slice(source.indexOf('function pendingQuestionJob('),source.indexOf('function discordCopyTools('));
const run=source.slice(source.indexOf('async function runQuestion('),source.indexOf("$('question-history').addEventListener"));
const scope={source:'account',account_id:'900',server:'100',channel:'200',timezone:'America/Los_Angeles'};
const request={...scope,question:'大家怎么看？',start_date:'2026-10-20',end_date:'2026-10-21',conversation_id:''};

function setup(api) {
  const values=new Map(),waits=[],fields=Object.fromEntries(['question-start','question-end','question-input','question-status','ask-question','question-messages'].map(id=>[id,{
    value:'',hidden:false,disabled:false,setCustomValidity(){},querySelector:()=>({textContent:''})
  }]));
  let serial=0;
  const context=vm.createContext({api,JSON,URLSearchParams,questionBusy:false,questionSequence:0,scopeSequence:0,
    questionTurns:[],questionConversation:'',selectedDataScope:()=>scope,isLive:()=>true,$:id=>fields[id],
    renderQuestions(){},loadHistory:async()=>{},updateLiveReadiness(){},message(){},
    crypto:{randomUUID:()=>String(++serial).padStart(32,'0')},
    sessionStorage:{getItem:key=>values.get(key),setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)},
    setTimeout:(callback,ms)=>{waits.push(ms);callback();}});
  vm.runInContext(helpers,context);
  return {context,values,waits,fields};
}

test('question polling stays alive beyond 300 seconds and includes the frozen account and channel',async()=>{
  const ticks=[1,91,299,450],calls=[],progress=[];
  const {context,waits,values}=setup(async(path,data)=>{
    calls.push({path,data});
    return ticks.length ? {id:'1'.padStart(32,'0'),status:'running',elapsed_seconds:ticks.shift(),model_wait_seconds:300}
      : {status:'complete',result:{answer:'原回答'}};
  });
  assert.equal((await context.questionJob(request,text=>progress.push(text))).answer,'原回答');
  assert.equal(calls.filter(call=>call.data).length,1);
  const params=new URL(calls[1].path,'http://localhost').searchParams;
  for (const [key,value] of Object.entries(scope)) assert.equal(params.get(key),value);
  assert.match(progress.at(-1),/450 秒/);assert.deepEqual(waits,[2000,2000,2000,2000]);assert.equal(values.size,0);
});

test('a lost request connection is recovered with the original id and service interruption is never reissued',async()=>{
  let broken=true;const ids=[];
  const {context,values}=setup(async(_path,data)=>{
    if (data) {ids.push(data.id);if(broken)throw new Error('lost response');}
    return {status:'interrupted',error:'服务已重启，未自动重发'};
  });
  await assert.rejects(context.questionJob(request,()=>{}),/同一任务/);
  assert.equal(values.size,1);broken=false;
  await assert.rejects(context.questionJob({...request},()=>{}),/未自动重发/);
  assert.equal(ids.length,2);assert.equal(ids[0],ids[1]);assert.equal(values.size,0);
});

test('new conversation detaches the old wait without leaking it to another account or reusing its id',async()=>{
  const ids=[];
  const {context}=setup(async(_path,data)=>{ids.push(data.id);throw new Error('lost response');});
  await assert.rejects(context.questionJob(request,()=>{}));
  context.selectedDataScope=()=>({...scope,account_id:'901'});context.detachQuestionJob();
  assert.equal(context.pendingQuestionJob().detached,undefined);
  context.selectedDataScope=()=>scope;context.detachQuestionJob();
  assert.equal(context.pendingQuestionJob().detached,true);
  await assert.rejects(context.questionJob(request,()=>{}));
  assert.notEqual(ids[0],ids[1]);
});

test('the completed answer remains visible if refreshing the history list fails',async()=>{
  const {context}=setup();vm.runInContext(run,context);
  const response={question:request.question,scope,conversation_id:'saved',asked_at:'2026-10-21T00:00:00Z',answer:'已保存的回答'};
  context.questionJob=async()=>response;
  context.loadHistory=async()=>{throw new Error('history connection lost');};
  await context.runQuestion(request);
  assert.equal(context.questionTurns.length,1);
  assert.equal(context.questionTurns[0].response.answer,'已保存的回答');
  assert.equal(context.questionBusy,false);
});

test('restoring a completed follow-up does not duplicate the answer already in its original history',async()=>{
  const {context}=setup();vm.runInContext(run,context);
  const response={scope,conversation_id:'saved',asked_at:'2026-10-21T00:00:00Z',answer:'原回答'};
  context.questionTurns=[{question:request.question,response}];context.questionJob=async()=>response;
  await context.runQuestion(request);
  assert.equal(context.questionTurns.length,1);assert.equal(context.questionTurns[0].response.answer,'原回答');
});

test('a late answer cannot replace the new channel conversation or enable its send button',async()=>{
  const {context,fields}=setup();vm.runInContext(run,context);
  let resolve;context.questionJob=()=>new Promise(done=>{resolve=done;});
  const pending=context.runQuestion(request);
  context.scopeSequence++;context.questionTurns=[{question:'新频道的问题'}];
  context.selectedDataScope=()=>({...scope,channel:'201'});
  resolve({scope,conversation_id:'old',asked_at:'2026-10-21T00:00:00Z'});await pending;
  assert.equal(context.questionTurns[0].question,'新频道的问题');assert.equal(fields['ask-question'].disabled,true);
});

test('changing scope during history restoration cannot restore the old dates or resume the old question',async()=>{
  const {context,fields}=setup();vm.runInContext(run,context);
  let resolve;context.restoreQuestions=()=>new Promise(done=>{resolve=done;});
  let calls=0;context.runQuestion=async()=>{calls++;};
  context.pendingQuestionJob=()=>({id:'1'.padStart(32,'0'),request:{...request,conversation_id:'old'}});
  const pending=context.resumeQuestionJob();
  context.scopeSequence++;context.selectedDataScope=()=>({...scope,channel:'201'});
  resolve(false);await pending;
  assert.equal(fields['question-start'].value,'');assert.equal(calls,0);
});

test('resuming a waiting question releases the navigation controls before the model finishes',async()=>{
  const {context}=setup();vm.runInContext(run,context);
  let release;context.questionJob=()=>new Promise(done=>{release=done;});
  context.pendingQuestionJob=()=>({id:'1'.padStart(32,'0'),request});
  let settled=false;context.resumeQuestionJob().then(()=>{settled=true;});
  await Promise.resolve();assert.equal(settled,true);assert.equal(context.questionBusy,true);
  release({scope,conversation_id:'saved',asked_at:'2026-10-21T00:00:00Z'});
  await Promise.resolve();
});

test('a completed answer replaces its pending bubble after older messages have been prepended',async()=>{
  const {context}=setup();vm.runInContext(run,context);
  let release;context.questionJob=()=>new Promise(done=>{release=done;});
  const waiting=context.runQuestion(request);
  context.questionTurns.unshift({question:'更早的问题',response:{scope,conversation_id:'old',asked_at:'2026-10-20T00:00:00Z',answer:'更早的回答'}});
  release({scope,conversation_id:'saved',asked_at:'2026-10-21T00:00:00Z',answer:'本次回答'});await waiting;
  assert.equal(context.questionTurns.length,2);
  assert.equal(context.questionTurns[0].response.answer,'更早的回答');
  assert.equal(context.questionTurns[1].response.answer,'本次回答');
});
