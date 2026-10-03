import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { mkdtemp, readFile, writeFile, chmod, stat, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { SecondCursor } from './index.mjs';
import { runAgent, fileCheckpointStore, WorkflowCheckpointError } from './agent.mjs';

const task = 'Submit exactly one fixture invoice and report its receipt';
const targets = [{ browserId: 'fixture', tabId: 7 }];
const call = { id: 'submit', name: 'browser_command', arguments: { browser_id: 'fixture', tab_id: 7, operation: 'click', selector: '#submit' } };
async function fixture(t) {
  const dir = await mkdtemp(join(tmpdir(), 'sc-workflow-'));
  const path = join(dir, 'checkpoint.json');
  const state = { count: 0, sessions: 0, status: 'running', requests: [], operations: new Map(), drop: false, wait: false, afterSubmit: null, targets: [], onReconcile: null };
  const server = createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk;
    const packet = JSON.parse(raw), mcp = packet.jsonrpc === '2.0';
    const tool = mcp ? packet.params?.name : packet.tool, args = (mcp ? packet.params?.arguments : packet.args) ?? {};
    state.requests.push({tool,args});
    const send = (data) => { if (response.destroyed) return; const result = {content:[{type:'text',text:JSON.stringify(data)}]}; response.writeHead(200, {'Content-Type':'application/json'}); response.end(JSON.stringify(mcp ? {jsonrpc:'2.0',id:packet.id,result} : result)); };
    if (packet.method === 'initialize') { const id = request.headers['mcp-session-id'] ?? `s-${++state.sessions}`; response.writeHead(200, {'Content-Type':'application/json','Mcp-Session-Id':id}); return response.end(JSON.stringify({jsonrpc:'2.0',id:packet.id,result:{serverInfo:{name:'fixture'}}})); }
    if (tool === 'tools/list') { response.writeHead(200, {'Content-Type':'application/json'}); return response.end(JSON.stringify([{name:'browser_command'}])); }
    if (tool === 'session') return send({session_id:`s-${++state.sessions}`});
    if (tool === 'task') return send({task:{id:'task-1',status:state.status,targets:state.targets}});
    if (tool === 'operation_status') { await state.onReconcile?.(); return send(state.operations.get(args.operation_id) ?? {operation_id:args.operation_id,state:'unknown',outcome_known:false}); }
    if (tool === 'get_window_state') return send({snapshot_id:`window-${state.requests.length}`,window:{id:args.window_id},text:'Observed native fixture'});
    if (tool === 'browser_snapshot') return send({snapshot_id:`snapshot-${state.requests.length}`,freshness:{status:'current'},text:`Submitted: ${state.count}`});
    if (['browser_command','batch','click'].includes(tool)) {
      const saved = JSON.parse(await readFile(path,'utf8'));
      assert.equal(saved.pending.operationId, args.operation_id, 'ID must be durable before dispatch');
      state.count++;
      state.operations.set(args.operation_id,{operation_id:args.operation_id,state:'completed',outcome_known:true,result_expired:false,result:{receipt:'invoice-1',count:state.count}});
      await state.afterSubmit?.();
      if (state.drop) return request.socket.destroy();
      if (state.wait) return;
      return send({receipt:'invoice-1',count:state.count});
    }
    send({ok:true});
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  t.after(async()=>{ server.closeAllConnections(); await new Promise(resolve=>server.close(resolve)); await rm(dir,{recursive:true,force:true}); });
  const options = (transport='http') => ({client:new SecondCursor({url,token:'secret-runtime-token',transport,timeoutMs:400}),task,workflow:{id:'invoice',store:fileCheckpointStore(path),targets}});
  return {dir,path,state,url,options};
}
const doneAdapter = {next:async({history}) => history.some(x=>x.role==='tool' && x.id==='submit' && x.result) ? {text:'Done'} : {calls:[call],progress:{stage:'submitting'}}};

test('persisted operation IDs, progress, terminal results, file mode, and opt-in content', async t=>{
  const f=await fixture(t); const result=await runAgent({...f.options(),adapter:doneAdapter});
  assert.equal(result.text,'Done'); assert.equal(f.state.count,1);
  const saved=JSON.parse(await readFile(f.path,'utf8')); assert.equal(saved.progress.stage,'submitting'); assert.equal(saved.status,'completed'); assert.equal(saved.sessionId,'s-1');
  assert(!JSON.stringify(saved).includes('secret-runtime-token')); assert.equal((await stat(f.path)).mode & 0o777,0o600);
  const requests=f.state.requests.length;
  const repeated=await runAgent({...f.options(),adapter:{next(){throw Error('must not run');}}});
  assert.equal(repeated.text,'Done'); assert.equal(f.state.requests.length,requests);
});

for (const transport of ['http','mcp']) test(`${transport}: process death after submission reconciles without duplicate`, async t=>{
  const f=await fixture(t); f.state.wait=true;
  let child;
  f.state.afterSubmit=async()=>{ child.kill('SIGKILL'); };
  const script=`import {SecondCursor} from ${JSON.stringify(new URL('./index.mjs',import.meta.url).href)}; import {runAgent,fileCheckpointStore} from ${JSON.stringify(new URL('./agent.mjs',import.meta.url).href)}; await runAgent({client:new SecondCursor({url:${JSON.stringify(f.url)},token:'secret-runtime-token',transport:${JSON.stringify(transport)}}),task:${JSON.stringify(task)},workflow:{id:'invoice',store:fileCheckpointStore(${JSON.stringify(f.path)}),targets:${JSON.stringify(targets)}},adapter:{next:async()=>({calls:[${JSON.stringify(call)},{id:'stale',name:'browser_command',arguments:{browser_id:'fixture',tab_id:7,operation:'click',selector:'#other'}}],progress:{stage:'submitted-next'}})}});`;
  child=spawn(process.execPath,['--input-type=module','--eval',script],{stdio:['ignore','pipe','pipe']});
  let stderr=''; child.stderr.on('data',x=>stderr+=x); const [code,signal]=await once(child,'exit'); assert.equal(signal,'SIGKILL',stderr); assert.equal(code,null); assert.equal(f.state.count,1);
  f.state.wait=false; f.state.afterSubmit=null;
  const resumed=await runAgent({...f.options(transport),adapter:{next:async({history,progress})=>{ assert.equal(progress.stage,'submitted-next'); assert(history.some(x=>x.id==='submit'&&x.result?.receipt==='invoice-1')); assert(history.some(x=>x.id==='stale'&&x.error)); assert(history.some(x=>x.name==='browser_snapshot'&&x.recovered)); return {text:'Recovered'}; }}});
  assert.equal(resumed.text,'Recovered'); assert.equal(f.state.count,1); assert.equal(f.state.sessions,1);
});

test('unknown, expired, interrupted and uncertain outcomes never replay', async t=>{
  const f=await fixture(t); f.state.drop=true;
  const first=await runAgent({...f.options(),adapter:doneAdapter}); assert.equal(first.stopped,'recovery_required');
  const checkpoint=JSON.parse(await readFile(f.path,'utf8')); const op=checkpoint.pending.operationId;
  for(const outcome of [{state:'unknown',outcome_known:false},{state:'completed',outcome_known:true,result_expired:true},{state:'interrupted',outcome_known:false},{state:'completed',outcome_known:false,result:{}},{state:'completed',outcome_known:true,result:{outcome:{state:'uncertain'}}}]) {
    f.state.operations.set(op,{operation_id:op,...outcome});
    const result=await runAgent({...f.options(),adapter:{next(){throw Error('must not continue');}}}); assert.equal(result.stopped,'recovery_required'); assert.equal(f.state.count,1);
  }
});

test('pause/cancel require host action and no current approval is reused', async t=>{
  const f=await fixture(t); const options=f.options();
  const first=await runAgent({...options,maxSteps:1,onApproval:()=>false,adapter:doneAdapter}); assert.equal(first.stopped,'step_limit'); assert.equal(f.state.count,0);
  for(const status of ['paused','needs_user','cancelled','interrupted','completed','failed']) { f.state.status=status; const r=await runAgent({...f.options(),adapter:doneAdapter}); assert.equal(r.stopped,`task_${status}`); assert.equal(f.state.count,0); }
  f.state.status='running'; let approvals=0;
  await runAgent({...f.options(),adapter:doneAdapter,onApproval:()=>{approvals++;return true;}}); assert.equal(approvals,1); assert.equal(f.state.count,1);
});

test('mismatch, corrupted or insecure checkpoint fails before any model work', async t=>{
  const f=await fixture(t); await runAgent({...f.options(),adapter:doneAdapter}); const original=await readFile(f.path,'utf8');
  for(const override of [{task:'different'}, {workflow:{...f.options().workflow,id:'different'}},{workflow:{...f.options().workflow,targets:[{windowId:99}]}}]) await assert.rejects(runAgent({...f.options(),...override,adapter:doneAdapter}),WorkflowCheckpointError);
  await writeFile(f.path,'{broken'); await assert.rejects(runAgent({...f.options(),adapter:doneAdapter}),WorkflowCheckpointError);
  await writeFile(f.path,original); await chmod(f.path,0o644); await assert.rejects(runAgent({...f.options(),adapter:doneAdapter}),WorkflowCheckpointError);
  await chmod(f.path,0o600); const alias=join(f.dir,'alias.json'); await symlink(f.path,alias); await assert.rejects(runAgent({...f.options(),workflow:{id:'invoice',targets,store:fileCheckpointStore(alias)},adapter:doneAdapter}),WorkflowCheckpointError);
});

test('concurrent owner, size bound, and storage failure prevent dispatch', async t=>{
  const f=await fixture(t); let unblock; const blocked=new Promise(resolve=>unblock=resolve); let entered; const ready=new Promise(resolve=>entered=resolve);
  const running=runAgent({...f.options(),adapter:{next:async()=>{entered();await blocked;return{text:'Done'};}}}); await ready;
  await assert.rejects(runAgent({...f.options(),adapter:doneAdapter}),error=>error.code==='CHECKPOINT_LOCKED'); unblock(); await running;
  await assert.rejects(fileCheckpointStore(f.path,{maxBytes:8}).acquire().then(async lease=>{try{await lease.load();}finally{await lease.release();}}),WorkflowCheckpointError);
  const failingStore={async acquire(){return{load:async()=>null,save:async()=>{throw Error('disk full');},release:async()=>{}};}};
  await assert.rejects(runAgent({...f.options(),workflow:{id:'another',targets,store:failingStore},adapter:doneAdapter}),/disk full/); assert.equal(f.state.count,0);
});

test('callback result is preserved, and credentials are rejected before storage', async t=>{
  const f=await fixture(t); const options=f.options(); options.client.onResult=({tool})=>{if(tool==='browser_command')throw Error('host callback broke');};
  const r=await runAgent({...options,adapter:doneAdapter}); assert.equal(r.text,'Done'); assert.equal(f.state.count,1);
  const g=await fixture(t); await assert.rejects(runAgent({...g.options(),adapter:{next:async()=>({text:'secret-runtime-token'})}}),error=>error.code==='CHECKPOINT_CREDENTIAL'); assert.equal(g.state.count,0);
});

test('failure of the durable pending write prevents the mutation', async t=>{
  const f=await fixture(t), real=fileCheckpointStore(f.path);
  const store={async acquire(){const lease=await real.acquire();return{...lease,async save(state){if(state.pending)throw Error('pending write failed');await lease.save(state);}};}};
  await assert.rejects(runAgent({...f.options(),workflow:{id:'invoice',targets,store},adapter:doneAdapter}),/pending write failed/);
  assert.equal(f.state.count,0); const saved=JSON.parse(await readFile(f.path,'utf8'));assert.equal(saved.pending,null);
});

test('pause during approval prevents dispatch and a later resume requests approval again', async t=>{
  const f=await fixture(t);let approvals=0;
  const first=await runAgent({...f.options(),adapter:doneAdapter,onApproval(){approvals++;f.state.status='paused';return true;}});
  assert.equal(first.stopped,'task_paused');assert.equal(f.state.count,0);
  f.state.status='running';await runAgent({...f.options(),adapter:doneAdapter,onApproval(){approvals++;return true;}});
  assert.equal(f.state.count,1);assert.equal(approvals,2);
});

test('lost unjournaled task control never falls back to operation replay', async t=>{
  const f=await fixture(t), options=f.options(), original=options.client.call.bind(options.client);
  options.client.call=async(name,args)=>{if(name==='task'&&args.action==='pause'){const error=Error('response lost');error.outcomeUncertain=true;throw error;}return original(name,args);};
  const first=await runAgent({...options,adapter:{next:async()=>({calls:[{id:'pause',name:'task',arguments:{action:'pause'}}]})}});
  assert.equal(first.stopped,'recovery_required');assert.equal(first.reason,'untracked_control');
  const before=f.state.requests.filter(x=>x.tool==='operation_status').length;
  const resumed=await runAgent({...f.options(),adapter:doneAdapter});assert.equal(resumed.reason,'untracked_control');
  assert.equal(f.state.requests.filter(x=>x.tool==='operation_status').length,before);assert.equal(f.state.count,0);
});

test('new target tracking is separate from configured targets and recovery history has paired calls', async t=>{
  const f=await fixture(t);let rounds=0;
  await runAgent({...f.options(),maxSteps:1,adapter:{next:async()=>({calls:[{id:'read-new',name:'browser_snapshot',arguments:{browser_id:'fixture',tab_id:8}}]})}});
  const saved=JSON.parse(await readFile(f.path,'utf8'));assert.equal(saved.initialTargets.length,1);assert.equal(saved.targets.length,2);
  await runAgent({...f.options(),adapter:{next:async({history})=>{
    const pending=new Set();
    for(const item of history){if(item.role==='assistant')for(const c of item.calls??[])pending.add(c.id);else{assert(pending.delete(item.id),`orphan result ${item.id}`);}}
    assert.equal(pending.size,0);assert(f.state.requests.filter(x=>x.tool==='browser_snapshot').slice(1).every(x=>x.args.full===true));rounds++;return{text:'Done'};
  }}});assert.equal(rounds,1);
});

test('malformed nested checkpoint fields and identity mismatches fail before dispatch', async t=>{
  const f=await fixture(t);await runAgent({...f.options(),adapter:doneAdapter});const original=JSON.parse(await readFile(f.path,'utf8'));
  for(const alter of [s=>s.version=2,s=>s.pending={},s=>s.queue=[{name:'click'}],s=>s.targets=[{windowId:-1}],s=>s.history=[{role:'tool'}],s=>s.completedCalls=-1]){
    const data=structuredClone(original);alter(data);await writeFile(f.path,JSON.stringify(data));
    await assert.rejects(runAgent({...f.options(),adapter:doneAdapter}),WorkflowCheckpointError);
  }
  await writeFile(f.path,JSON.stringify(original));
  const mismatch=f.options();mismatch.client.sessionId='another-session';await assert.rejects(runAgent({...mismatch,adapter:doneAdapter}),error=>error.code==='CHECKPOINT_MISMATCH');
  await assert.rejects(runAgent({...f.options('mcp'),adapter:doneAdapter}),error=>error.code==='CHECKPOINT_MISMATCH');
});

test('fresh observation failure stops continuation and adapter errors leave durable history', async t=>{
  const f=await fixture(t);await runAgent({...f.options(),maxSteps:1,adapter:doneAdapter});
  const next=f.options(),original=next.client.call.bind(next.client);
  next.client.call=(name,args)=>name==='browser_snapshot'?Promise.reject(Error('observation unavailable')):original(name,args);
  const blocked=await runAgent({...next,adapter:{next(){throw Error('must not run');}}});assert.equal(blocked.reason,'observation_failed');assert.equal(f.state.count,1);
  await assert.rejects(runAgent({...f.options(),adapter:{next(){throw Error('model unavailable');}}}),/model unavailable/);
  const saved=JSON.parse(await readFile(f.path,'utf8'));assert(saved.history.some(x=>x.id==='submit'&&x.result));assert.equal(saved.pending,null);
});

test('uncertain batch steps and malformed results stop without continuing', async t=>{
  for(const result of [{steps:[{uncertain:true}]},{steps:[{result:{outcome:{state:'uncertain'}}}]},{steps:{invalid:true}}]){
    const f=await fixture(t),opts=f.options(),original=opts.client.call.bind(opts.client);
    opts.client.call=async(name,args)=>{const real=await original(name,args);return name==='browser_command'?result:real;};
    const r=await runAgent({...opts,adapter:doneAdapter});assert.equal(r.reason,'outcome_uncertain');assert.equal(f.state.count,1);
  }
});

test('invalid history calls, unmatched results and orphan queues are rejected', async t=>{
  const f=await fixture(t);await runAgent({...f.options(),adapter:doneAdapter});const original=JSON.parse(await readFile(f.path,'utf8'));
  for(const alter of [s=>s.history[0].calls='broken',s=>s.history[1].id='orphan',s=>{s.status='active';s.queue=[call];},s=>s.history[0].calls=[{id:'submit',name:12}]]){
    const data=structuredClone(original);alter(data);await writeFile(f.path,JSON.stringify(data));await assert.rejects(runAgent({...f.options(),adapter:doneAdapter}),WorkflowCheckpointError);
  }
});

test('invalid tool results and too many discovered targets cannot create unresumable checkpoints', async t=>{
  const f=await fixture(t);await runAgent({...f.options(),adapter:doneAdapter});const original=JSON.parse(await readFile(f.path,'utf8'));
  for(const alter of [s=>delete s.history[1].result,s=>s.history[1].error=12,s=>s.history[1].result='invalid']){
    const data=structuredClone(original);alter(data);await writeFile(f.path,JSON.stringify(data));await assert.rejects(runAgent({...f.options(),adapter:doneAdapter}),WorkflowCheckpointError);
  }
  const g=await fixture(t),many=Array.from({length:128},(_,n)=>({browserId:'fixture',tabId:n+1}));
  await assert.rejects(runAgent({...g.options(),workflow:{...g.options().workflow,targets:many},adapter:{next:async()=>({calls:[{...call,arguments:{...call.arguments,tab_id:129}}]})}}),WorkflowCheckpointError);
  assert.equal(g.state.count,0);const saved=JSON.parse(await readFile(g.path,'utf8'));assert.equal(saved.targets.length,128);assert.equal(saved.pending,null);
});

test('nested batch args and legacy JSON targets are durable before dispatch and freshly read on resume', async t=>{
  const f=await fixture(t);f.state.drop=true;
  const batch={id:'cross-window',name:'batch',arguments:{window_id:41,steps:[
    {tool:'click',element_index:1},
    {tool:'click',window_id:42,element_index:1},
    {tool:'click',window_id:999,args:{window_id:43,element_index:1}},
    {tool:'click',window_id:998,args:JSON.stringify({window_id:44,element_index:1})}
  ]}};
  f.state.afterSubmit=async()=>{
    const saved=JSON.parse(await readFile(f.path,'utf8'));
    assert.deepEqual(saved.targets.filter(x=>x.windowId).map(x=>x.windowId),[41,42,43,44]);
  };
  const failed=await runAgent({...f.options(),adapter:{next:async()=>({calls:[batch]})}});assert.equal(failed.stopped,'recovery_required');
  f.state.drop=false;f.state.afterSubmit=null;
  const recovered=await runAgent({...f.options(),adapter:{next:async()=>({text:'Recovered all windows'})}});
  assert.equal(recovered.text,'Recovered all windows');assert.equal(f.state.count,1);
  assert.deepEqual(f.state.requests.filter(x=>x.tool==='get_window_state').map(x=>x.args.window_id),[41,42,43,44]);
  assert(f.state.requests.filter(x=>x.tool==='get_window_state').every(x=>x.args.full===true));
});

test('resume merges runtime-resolved app and browser targets even when recorded during reconciliation', async t=>{
  const f=await fixture(t);f.state.drop=true;
  const first=await runAgent({...f.options(),adapter:{next:async()=>({calls:[{id:'app-action',name:'click',arguments:{app:'Fixture Notes',element_index:1}}]})}});
  assert.equal(first.stopped,'recovery_required');
  const before=JSON.parse(await readFile(f.path,'utf8'));assert(!before.targets.some(x=>x.windowId===91));
  f.state.onReconcile=async()=>{f.state.targets=[{key:'window:91',window_id:91,app:'Fixture Notes'},{key:'browser:resolved:tab:8',browser_id:'resolved',tab_id:8}];};
  const resumed=await runAgent({...f.options(),adapter:{next:async({history})=>{
    assert(history.some(x=>x.role==='tool'&&x.name==='get_window_state'&&x.result?.window?.id===91));
    assert(f.state.requests.some(x=>x.tool==='browser_snapshot'&&x.args.browser_id==='resolved'&&x.args.tab_id===8&&x.args.full===true));
    return{text:'Recovered resolved targets'};
  }}});
  assert.equal(resumed.text,'Recovered resolved targets');assert.equal(f.state.count,1);
});

test('browser target discovery preserves outer command precedence and inherited browser IDs', async t=>{
  const f=await fixture(t);
  const command={id:'nested-command',name:'browser_command',arguments:{browser_id:'fixture',operation:'click',tab_id:8,arguments:{browser_id:'wrong',tab_id:999,selector:'#submit'}}};
  await runAgent({...f.options(),maxSteps:1,adapter:{next:async()=>({calls:[command]})}});
  const saved=JSON.parse(await readFile(f.path,'utf8'));
  assert(saved.targets.some(x=>x.browserId==='fixture'&&x.tabId===8));assert(!saved.targets.some(x=>x.browserId==='wrong'||x.tabId===999));
  const g=await fixture(t), inherited={id:'inherited-tab',name:'batch',arguments:{browser_id:'fixture',tab_id:7,steps:[{tool:'browser_click',tab_id:998,browser_id:'also-wrong',args:{ref:'node-1',tab_id:999,browser_id:'wrong'}}]}};
  await runAgent({...g.options(),maxSteps:1,adapter:{next:async()=>({calls:[inherited]})}});
  assert.deepEqual(JSON.parse(await readFile(g.path,'utf8')).targets,targets);
});
