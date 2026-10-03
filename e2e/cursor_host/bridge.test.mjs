import test from 'node:test';
import assert from 'node:assert/strict';
import { validateSelection, sameSelection, validateTools, restrictedOptions, validateCallback, validateStreamCall, runBridge } from './bridge.mjs';
import { PassThrough } from 'node:stream';
import { mkdtemp, mkdir, rm, access } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const selection = {id:'grok-4.7',params:[{id:'context',value:'256k'},{id:'reasoning_effort',value:'medium'},{id:'fast',value:'true'}]};
const catalog = [{id:'grok-4.7',parameters:selection.params.map(p=>({id:p.id,values:[{value:p.value}]}))}];
test('requires exact canonical model and every catalog parameter',()=>{
  assert.deepEqual(validateSelection(selection,catalog),selection);
  for(const bad of [
    {...selection,id:'auto'}, {...selection,params:selection.params.slice(1)},
    {...selection,params:[...selection.params,{id:'fast',value:'true'}]},
    {...selection,params:selection.params.map(p=>p.id==='fast'?{...p,value:'false'}:p)}
  ]) assert.throws(()=>validateSelection(bad,catalog));
});
test('selection equality ignores ordering, never missing or duplicate parameters',()=>{
  assert.ok(sameSelection(selection,{...selection,params:[...selection.params].reverse()}));
  assert.equal(sameSelection(selection,{...selection,params:selection.params.slice(1)}),false);
  assert.equal(sameSelection(selection,{...selection,params:[...selection.params,selection.params[0]]}),false);
});
test('tool names are unique identifiers with object schemas',()=>{
  const tool={name:'read_fixture',description:'Read fixture.',inputSchema:{type:'object',properties:{}}};
  assert.deepEqual(validateTools([tool]),[tool]);
  for(const bad of [[],[tool,tool],[{...tool,name:'../shell'}],[{...tool,inputSchema:null}]])
    assert.throws(()=>validateTools(bad));
});
test('SDK options exclude ambient settings and every non-MCP capability',()=>{
  const options=restrictedOptions(selection,'/empty',{},{});
  assert.deepEqual(options.tools,['mcp_tool_call','get_mcp_tools_tool_call']);
  assert.deepEqual(options.disallowedTools,['shell','task']);
  assert.deepEqual(options.mcpServers,{});
  assert.deepEqual(options.local.settingSources,[]);
  assert.equal(options.local.enableAgentRetries,false);
  assert.equal(options.cloud,undefined);
  assert.equal(options.local.dirs,undefined);
});

test('callbacks and observed MCP calls remain in the root fixture session',()=>{
  validateCallback({sessionId:'agent-1'},'agent-1');
  assert.throws(()=>validateCallback({sessionId:'child-1'},'agent-1'));
  validateStreamCall({name:'mcp',args:{providerIdentifier:'custom-user-tools',toolName:'read_fixture'}},['read_fixture']);
  for(const event of [
    {name:'shell'}, {name:'mcp',args:{providerIdentifier:'external'}},
    {name:'mcp',args:{toolName:'read_private_file'}}
  ]) assert.throws(()=>validateStreamCall(event,['read_fixture']));
});

async function fixture(t, sdk) {
  const root=await mkdtemp(join(tmpdir(),'cursor-bridge-test-'));
  t.after(()=>rm(root,{recursive:true,force:true}));
  const cwd=join(root,'workspace'),store_dir=join(root,'store');
  await mkdir(cwd);
  const input=new PassThrough(),output=new PassThrough(),events=[];
  let buffer='';
  output.on('data',chunk=>{
    buffer+=chunk.toString();
    let newline;
    while((newline=buffer.indexOf('\n'))>=0){events.push(JSON.parse(buffer.slice(0,newline)));buffer=buffer.slice(newline+1);}
  });
  const done=runBridge({sdk,input,output,identity:{audited:true,version:'test-sdk',hash:'test'}});
  input.write(JSON.stringify({type:'init',cwd,store_dir,model:selection,prompt:'Read fixture.',
    tools:[{name:'read_fixture',description:'Read.',inputSchema:{type:'object',properties:{}}}],max_steps:1,timeout_seconds:2})+'\n');
  return {input,events,done,store_dir};
}

test('cancel during model catalog lookup never starts an agent',async t=>{
  let resolveCatalog,enteredCatalog;
  const entered=new Promise(resolve=>enteredCatalog=resolve);
  const sdk={Cursor:{models:{list:()=>{enteredCatalog();return new Promise(r=>resolveCatalog=r);}}},
    Agent:{create:()=>assert.fail('Cancelled run created an agent')},JsonlLocalAgentStore:class{}};
  const run=await fixture(t,sdk);
  await entered;
  run.input.write('{"type":"cancel"}\n');
  resolveCatalog(catalog);
  await run.done;
  assert.equal(run.events.at(-1).status,'error');
  assert.equal(run.events.some(e=>e.type==='ready'),false);
  await assert.rejects(access(run.store_dir));
});

test('a resolved model mismatch cannot report success and still disposes the store',async t=>{
  let disposed=false;
  const sdk={Cursor:{models:{list:async()=>catalog}},JsonlLocalAgentStore:class{},Agent:{create:async()=>({
    agentId:'agent-1',model:selection,[Symbol.asyncDispose]:async()=>{disposed=true;},
    send:async()=>({status:'finished',async *stream(){},wait:async()=>({status:'finished',model:{id:'other',params:[]},result:'done'})})
  })}};
  const run=await fixture(t,sdk);await run.done;
  assert.equal(run.events.at(-1).status,'error');assert.equal(disposed,true);
  await assert.rejects(access(run.store_dir));
});

test('completed response precedes cleanup but terminal success follows disposal',async t=>{
  let disposed=false,runFixture;
  const sdk={Cursor:{models:{list:async()=>catalog}},JsonlLocalAgentStore:class{},Agent:{create:async()=>({
    agentId:'agent-1',model:selection,[Symbol.asyncDispose]:async()=>{
      assert.equal(runFixture.events.at(-1).event.type,'final_response');disposed=true;
    },
    send:async()=>({status:'finished',async *stream(){},wait:async()=>({status:'finished',model:selection,result:'done'})})
  })}};
  runFixture=await fixture(t,sdk);await runFixture.done;
  assert.equal(runFixture.events.at(-1).status,'finished');assert.equal(disposed,true);
  await assert.rejects(access(runFixture.store_dir));
});
