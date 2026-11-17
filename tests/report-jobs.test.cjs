const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const app=fs.readFileSync('web/app.js','utf8');
const helper=app.slice(app.indexOf('function pendingReportJob('),app.indexOf('function message('));

function setup(api) {
  const values=new Map(), waits=[];
  const context=vm.createContext({api,JSON,URLSearchParams,crypto:{randomUUID:()=> 'a'.repeat(32)},
    sessionStorage:{getItem:key=>values.get(key),setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)},
    setTimeout:(callback,ms)=>{waits.push(ms);callback();}});
  vm.runInContext(helper,context);
  return {context,values,waits};
}

test('long model wait uses repeated short polls beyond 90 seconds and returns once',async()=>{
  const progress=[],queries=[],ticks=[1,91,180,299];let posts=0;
  const {context,values,waits}=setup(async(path,data)=>{
    queries.push(path);
    if (data) posts++;
    return ticks.length ? {id:'a'.repeat(32),status:'running',elapsed_seconds:ticks.shift(),model_wait_seconds:300}
      : {status:'complete',result:{report_id:'saved'}};
  });
  const result=await context.reportJob('account',{server:'one',channel:'selected'},text=>progress.push(text));
  assert.equal(result.report_id,'saved');assert.equal(posts,1);assert.equal(queries.length,5);
  assert.equal(waits.length,4);assert.ok(waits.every(ms=>ms===2000));
  assert.match(progress[3],/299 秒/);assert.equal(values.size,0);
});

test('a lost progress connection retains the task id for a refresh without automatic model retry',async()=>{
  const ids=[];let broken=true;
  const {context,values}=setup(async(path,data)=>{
    if (data) {ids.push(data.id);return {id:data.id,status:'running',elapsed_seconds:30,model_wait_seconds:300};}
    if (broken) throw new Error('connection lost');
    return {status:'complete',result:{report_id:'same-original-result'}};
  });
  const request={server:'one',channel:'selected'};
  await assert.rejects(context.reportJob('bot',request,()=>{}),/同一任务/);
  assert.equal(ids.length,1);assert.equal(values.size,1);
  broken=false;
  const result=await context.reportJob('bot',{channel:'selected',server:'one'},()=>{});
  assert.equal(result.report_id,'same-original-result');assert.equal(ids[0],ids[1]);assert.equal(values.size,0);
});

test('refresh restores the frozen input file before polling and skips an unrelated re-export',async()=>{
  const source=fs.readFileSync('web/import-ui.js','utf8');
  const calls=[],item={id:'input',server:'one',channel:'selected',origin:{kind:'account-export'}};
  const context=vm.createContext({URLSearchParams,importReading:false,importBusy:false,importSequence:0,importItem:null,
    importPreferences:{server:'one',channel:'selected'},accountReportsVisible:()=>true,usingAccountSource:()=>true,renderExporterAccount:()=>{},
    importCatalog:async()=>[item],pendingReportJob:()=>({source:'account',request:{import_id:'input',server:'one',channel:'selected',timezone:'UTC'}}),
    api:async path=>{calls.push(path);return path==='/api/exporter' ? {status:'connected',servers:{one:'所选服务器'}} : item;},
    applyImportItem:restored=>assert.equal(restored.id,'input'),loadImportHistory:async()=>{},
    discoverExportChannels:()=>{throw new Error('unexpected re-export');}});
  vm.runInContext(source.slice(source.indexOf('async function loadImportPage('),source.indexOf('function configureImportUI(')),context);
  await context.loadImportPage();
  assert.equal(calls.length,2);assert.match(calls[1],/import_id=input&timezone=UTC/);
});
