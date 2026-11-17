const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

function setup(api) {
  const fields=Object.fromEntries(['page-questions','question-welcome','question-input','ask-question',
    'question-status','question-history','question-start','question-end','older-questions'].map(id=>[id,{
    value:'',hidden:false,classList:{toggle(){}},setCustomValidity(){}
  }]));
  let html='<article class="question-turn"></article>';
  fields['question-messages']={get innerHTML(){return html;},set innerHTML(value){html=value;}};
  fields['question-scroll']={scrollTop:17,get scrollHeight(){
    return (html.match(/class="question-turn"/g)||[]).length*300+(fields['older-questions'].hidden?0:50);
  }};
  const context=vm.createContext({api,$:id=>fields[id],URLSearchParams,scopeLoading:false,scopeSequence:0,
    questionSequence:0,questionConversation:'saved',historyBefore:'2',questionTurns:[{question:'当前问题',error:'旧错误'}],
    isLive:()=>false,selectedDataScope:()=>({server:'one',channel:'only'}),
    fullTimeLabel:()=>'',escapeHTML:value=>String(value),message(){},options(){}});
  const source=fs.readFileSync('web/app.js','utf8');
  vm.runInContext(source.slice(source.indexOf('function updateQuestionState('),source.indexOf("$('question-history').addEventListener")),context);
  return {context,fields};
}

test('loading older answers preserves the visible message even when the pagination button disappears',async()=>{
  const requests=[];
  const {context,fields}=setup(async path=>{
    requests.push(path);
    return {turns:[{seq:1,question:'更早的问题',error:'历史错误'}],has_more:false,
      start_date:'2026-10-20',end_date:'2026-10-21',stale:false};
  });
  const before=fields['question-scroll'].scrollHeight;
  fields['question-start'].value='2026-10-20';fields['question-end'].value='2026-10-21';
  await context.restoreQuestions('saved','2');
  assert.equal(fields['question-scroll'].scrollTop,17+fields['question-scroll'].scrollHeight-before);
  assert.equal(fields['older-questions'].hidden,true);
  assert.equal(fields['question-start'].value,'2026-10-20');
  assert.equal(fields['question-welcome'].hidden,true);
  assert.match(requests[0],/^\/api\/conversation\?/);
  assert.equal(requests.length,1);
  context.clearQuestions();
  assert.equal(fields['question-welcome'].hidden,false);
  assert.equal(fields['question-scroll'].scrollTop,0);
  assert.equal(context.questionTurns.length,0);
  assert.equal(context.questionConversation,'');
  assert.equal(requests.length,1);
});

test('a late history response after a scope change cannot replace the current chat or move its scroll',async()=>{
  let resolve;
  const {context,fields}=setup(()=>new Promise(done=>{resolve=done;}));
  const pending=context.restoreQuestions('saved');
  context.scopeSequence++;
  resolve({turns:[],has_more:false});
  await pending;
  assert.equal(fields['question-scroll'].scrollTop,17);
  assert.equal(context.questionTurns[0].question,'当前问题');
  assert.equal(fields['question-start'].value,'');
});

test('loading earlier history keeps the pending answer active and preserves the next question draft',async()=>{
  const {context,fields}=setup(async()=>({turns:[{seq:1,question:'更早的问题',error:'历史错误'}],has_more:false}));
  const pending={question:'正在生成的问题',pending:'等待中'};
  context.questionTurns=[pending];context.questionBusy=true;context.questionSequence=7;
  fields['question-input'].value='下一次想问的草稿';
  await context.restoreQuestions('saved','2');
  assert.equal(context.questionBusy,true);assert.equal(context.questionSequence,7);
  assert.equal(context.questionTurns.at(-1),pending);
  assert.equal(fields['question-input'].value,'下一次想问的草稿');
});
