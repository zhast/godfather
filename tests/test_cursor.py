"""Offline Cursor backend regression checks. Never invoke the installed CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import godfather_cursor as c
import godfather_stream as stream
import godfather_router as router
import agent_oversight as a

FAKE = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
if args==['--list-models']:
 print('opus - Opus\\nopus-fast - Opus Fast\\nbad - Fable (NO ZDR)');sys.exit()
if args==['status']:
 print('Not logged in' if os.getenv('CURSOR_SIGNED_OUT') else 'Logged in');sys.exit()
if args==['about']:
 print('Subscription Tier: Enterprise');sys.exit()
with open(os.environ['CAPTURE'],'a') as f:
 f.write(json.dumps(dict(args=args,config=os.getenv('CURSOR_CONFIG_DIR'),data=os.getenv('CURSOR_DATA_DIR'),path=os.getenv('PATH'),prompt=sys.stdin.read()))+'\\n')
print(json.dumps(dict(type='system',subtype='init',session_id='chat-cursor-123')))
if os.getenv('CURSOR_FAIL'):
 print('Usage limit reached: spend limit reached',file=sys.stderr);sys.exit(1)
print(json.dumps(dict(type='result',subtype='success',is_error=False,result='Cursor done',usage=dict(inputTokens=20,outputTokens=4))))
'''

class CursorTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name).resolve();self.fake=self.root/'cursor-agent';self.fake.write_text(FAKE);self.fake.chmod(0o700)
  self.env=dict(os.environ,HOME=str(self.root),PATH=str(self.root)+os.pathsep+os.environ['PATH'],CURSOR_AGENT_BIN=str(self.fake),AGENT_RUNS_DIR=str(self.root/'runs'),XDG_CONFIG_HOME=str(self.root/'config'),XDG_STATE_HOME=str(self.root/'state'),CWD=str(self.root),CAPTURE=str(self.root/'calls'),CURSOR_MODEL='opus',GODFATHER_QUOTA_PROBE='0')
  for key in list(self.env):
   if key.startswith(('AGENT_ROUTER','GODFATHER_')) or key in ('BASH_ENV','ZDOTDIR','AGENT_RESUME_ID','AGENT_NUDGE','CURSOR_HOME','CURSOR_CONFIG_DIR','CURSOR_DATA_DIR'):
    self.env.pop(key,None)
  self.env['GODFATHER_QUOTA_PROBE']='0'
  self.patch=patch.dict(os.environ,self.env,clear=True);self.patch.start();self.addCleanup(self.patch.stop)
 def call(self,*args):
  return subprocess.run(args,env=dict(os.environ),capture_output=True,text=True,timeout=45)
 def test_catalog_gate_cache_and_fast(self):
  self.assertEqual('opus-fast',c.resolve(dict(model='opus'),str(self.fake)))
  self.assertEqual('opus',c.resolve(dict(model='opus',fast=False),str(self.fake)))
  with self.assertRaisesRegex(ValueError,'NO ZDR'):c.resolve(dict(model='bad'),str(self.fake))
  self.assertEqual('bad',c.resolve(dict(model='bad',allow_no_zdr=True),str(self.fake)))
  self.fake.write_text('#!/bin/sh\nexit 1\n')
  self.assertEqual('opus-fast',c.resolve(dict(model='opus'),str(self.fake)))
 def test_launcher_resume_and_artifacts(self):
  os.environ['CURSOR_HOME']=str(self.root/'company-account')
  prompt=self.root/'prompt';prompt.write_text('Do the task')
  result=self.call(str(ROOT/'bin/cursor-run'),str(prompt),'direct')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  base=self.root/'runs/direct'
  run=json.loads(Path(str(base)+'.run.json').read_text())
  self.assertEqual('chat-cursor-123',run['session_id'])
  self.assertEqual('opus-fast',run['model'])
  self.assertEqual('Cursor done\n',Path(str(base)+'.last.md').read_text())
  self.assertIn('session=chat-cursor-123',Path(str(base)+'.models.log').read_text())
  self.assertEqual(20,run['tokens_in'])
  call=json.loads((self.root/'calls').read_text().splitlines()[0])
  self.assertIn('Godfather oversight',call['prompt']);self.assertIn('.shims',call['path'])
  self.assertEqual(call['config'],call['data'])
  os.environ['CURSOR_HOME']=str(self.root/'different-account')
  result=self.call(str(ROOT/'bin/agents-nudge'),'--wait','direct','Continue')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  args=json.loads((self.root/'calls').read_text().splitlines()[-1])['args']
  self.assertEqual('chat-cursor-123',args[args.index('--resume')+1])
  self.assertEqual(str(self.root/'company-account'),json.loads((self.root/'calls').read_text().splitlines()[-1])['config'])
 def test_router_and_review_mode(self):
  config=self.root/'config/godfather/models.toml';config.parent.mkdir(parents=True)
  config.write_text('[[models]]\nbackend="cursor"\nmodel="opus"\neffort="high"\n')
  result=self.call(str(ROOT/'bin/godfather'),'run','--no-isolate','-p','Do task','routed')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  self.assertIn('opus-fast', (self.root/'runs/routed.models.log').read_text())
  self.assertEqual('opus-fast',a.display_model(dict(cli='cursor',model='opus-fast',model_key='cursor:opus')))
  os.environ['CURSOR_SIGNED_OUT']='1'
  self.assertIsNone(router.backend_ready('cursor')[0])
 def test_stream_fixtures_errors_and_unknown(self):
  book=stream.Book('cursor',started_at=1)
  for i,line in enumerate((ROOT/'tests/fixtures/cursor-stream.jsonl').read_text().splitlines()):book.observe_line(line,i+2)
  self.assertEqual(20,book.tokens_in);self.assertEqual(4,book.tokens_out)
  self.assertEqual('stream',stream.classify('cursor',dict(type='future_event'))['kind'])
  from godfather_errors import classify
  for line in (ROOT/'tests/fixtures/cursor-errors.jsonl').read_text().splitlines():
   event=json.loads(line);self.assertEqual(event.pop('expected'),classify('cursor',event)['category'])
  for event,category in [(dict(type='error',error=dict(statusCode=401)),'auth'),(dict(type='error',message='Not logged in'),'auth'),(dict(type='result',is_error=True,error=dict(statusCode=402)),'quota'),(dict(type='error',message='limit reached'),'quota'),(dict(type='error',error=dict(code='RESOURCE_EXHAUSTED')),'quota'),(dict(type='error',message='server overloaded'),'capacity'),(dict(type='error',message='connection reset'),'network')]:
   self.assertEqual(category,classify('cursor',event)['category'])
 def test_usage_http_and_gate(self):
  payloads=[dict(planInfo=dict(planName='Enterprise',billingCycleEnd='1796083200000')),dict(planUsage=dict(totalPercentUsed=12),spendLimitUsage=dict(overallUsed=100,overallLimit=200)),dict(activeGrants=[dict(totalCents='200000',remainingCents='500')]),dict(hardLimit=2147483647)]
  import io
  def response(request,timeout):
   self.assertEqual(b'{}',request.data);self.assertEqual('Bearer private',request.get_header('Authorization'))
   return io.StringIO(json.dumps(payloads.pop(0)))
  with patch.object(c,'token',return_value='private'),patch.object(c.urllib.request,'urlopen',side_effect=response) as http:
   data=c.usage();self.assertEqual(4,http.call_count);self.assertEqual(data,c.usage());self.assertEqual(4,http.call_count)
  self.assertFalse(c.included({},data));self.assertTrue(c.included(dict(included_floor_cents=499),data))
  self.assertFalse(c.included({},dict(planUsage=dict(totalPercentUsed=100))))
  self.assertTrue(c.included({},{}));self.assertIn('usage unknown',c.describe({}))
  text=c.describe(data);self.assertIn('Enterprise',text);self.assertIn('$5.00/$2000.00',text);self.assertIn('$1.00/$2.00',text)
  self.assertIn('included usage not reported',c.describe(dict(planInfo=dict(planName='Enterprise'))))
 def test_schema_and_permissions(self):
  model=router.parse_models('[[models]]\nbackend="cursor"\nmodel="opus"\neffort="high"\nfast=false\nallow_no_zdr=true\nincluded_only=true\nhome="'+str(self.root/'account')+'"')[0]
  self.assertFalse(model['fast']);self.assertEqual(str(self.root/'account'),model['home'])
  from godfather_permissions import compile_profile
  profile=compile_profile(dict(cli='cursor',cwd=str(self.root),base=str(self.root/'runs/p')))
  self.assertTrue(any('Cursor' in t for t in profile['tradeoffs']))
 def test_doctor(self):
  with patch.object(a,'cli_binary',side_effect=lambda cli:str(self.fake) if cli=='cursor' else None),patch('builtins.print') as output:
   a.doctor(live=True)
   self.assertTrue(any('Enterprise' in str(call) for call in output.call_args_list))
 def configure_pair(self, cursor_first=True, cursor_options=''):
  from test_router import FAKE as other
  grok=self.root/'grok';grok.write_text(other);grok.chmod(0o700)
  auth=self.root/'.grok/auth.json';auth.parent.mkdir();auth.write_text('{"fake":true}')
  config=self.root/'config/godfather/models.toml';config.parent.mkdir(parents=True)
  blocks=['[[models]]\nbackend="cursor"\nmodel="opus"\neffort="high"\n'+cursor_options,'[[models]]\nbackend="grok"\nmodel="grok-test"\neffort="high"\n']
  config.write_text('\n'.join(blocks if cursor_first else blocks[::-1]))
 def test_quota_refusal_falls_back(self):
  self.configure_pair();os.environ['CURSOR_FAIL']='1'
  result=self.call(str(ROOT/'bin/godfather'),'run','--no-isolate','-p','Do task','fallback')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  log=(self.root/'runs/fallback.models.log').read_text()
  self.assertIn('HANDOFF grok:grok-test',log)
  failed=json.loads((self.root/'runs/fallback.run.json').read_text())
  self.assertEqual('quota',failed['failure']['category'])
  self.assertEqual('quota',failed['status'])
  self.assertNotIn('returned no successful terminal result',(self.root/'runs/fallback.log').read_text())
  events=[json.loads(line) for line in (self.root/'runs/fallback.stream.jsonl').read_text().splitlines()]
  self.assertFalse(any(event['type']=='result' for event in events))
 def test_auto_review_refuses_cursor_without_review_fix(self):
  self.configure_pair(cursor_first=False)
  result=self.call(str(ROOT/'bin/godfather'),'run','--no-isolate','--review-by','auto','-p','Do task','reviewed')
  self.assertNotEqual(0,result.returncode,result.stdout+result.stderr)
  self.assertFalse((self.root/'calls').exists())
  self.assertIn('no eligible models',(self.root/'runs/reviewed.review.models.log').read_text())
 def test_no_zdr_and_included_skip(self):
  self.configure_pair()
  config=self.root/'config/godfather/models.toml'
  config.write_text(config.read_text().replace('model="opus"','model="bad"'))
  result=self.call(str(ROOT/'bin/godfather'),'run','--no-isolate','-p','Do task','zdr')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  self.assertIn('NO ZDR',(self.root/'runs/zdr.models.log').read_text())
  self.assertFalse((self.root/'calls').exists())
 def test_usage_failure_never_blocks(self):
  with patch.object(c,'token',return_value='secret'),patch.object(c.urllib.request,'urlopen',side_effect=OSError('offline')):
   self.assertEqual({},c.usage())
  self.assertTrue(c.included({},{}))
 def test_included_only_route_gate_and_quota(self):
  self.configure_pair(cursor_options='included_only=true\n')
  c.cached('usage',300,lambda:dict(planUsage=dict(totalPercentUsed=100)))
  result=self.call(str(ROOT/'bin/godfather'),'run','--no-isolate','-p','Do task','included')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  self.assertIn('included allowance exhausted',(self.root/'runs/included.models.log').read_text())
  self.assertFalse((self.root/'calls').exists())
  result=self.call(str(ROOT/'bin/godfather'),'quota')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  self.assertIn('cursor opus eligible=no',result.stdout)
  self.assertIn('plan used=100%',result.stdout)

 def test_long_prompt_uses_stdin_not_argv(self):
  prompt=self.root/'long-prompt';body='LONG_PROMPT_SENTINEL'+('x'*3*1024*1024);prompt.write_text(body)
  result=self.call(str(ROOT/'bin/cursor-run'),str(prompt),'long')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  call=json.loads((self.root/'calls').read_text().splitlines()[0])
  self.assertTrue(call['prompt'].endswith(body));self.assertIn('Godfather oversight',call['prompt'])
  self.assertNotIn('LONG_PROMPT_SENTINEL',' '.join(call['args']))
 def test_ask_and_plan_drop_force_and_enable_sandbox(self):
  prompt=self.root/'prompt';prompt.write_text('Inspect only')
  for mode in ('ask','plan'):
   os.environ['CURSOR_MODE']=mode
   result=self.call(str(ROOT/'bin/cursor-run'),str(prompt),mode)
   self.assertEqual(0,result.returncode,result.stdout+result.stderr)
   args=json.loads((self.root/'calls').read_text().splitlines()[-1])['args']
   self.assertEqual('enabled',args[args.index('--sandbox')+1]);self.assertNotIn('--force',args)
   self.assertEqual(mode,args[args.index('--mode')+1])
 def test_explicit_review_excludes_configured_fast_model_block(self):
  self.configure_pair()
  result=self.call(str(ROOT/'bin/godfather'),'run','--no-isolate','--review-by','opus','--review-fix','-p','Do task','same')
  self.assertNotEqual(0,result.returncode,result.stdout+result.stderr)
  calls=(self.root/'calls').read_text().splitlines();self.assertEqual(1,len(calls))
  self.assertIn('no eligible models',(self.root/'runs/same.review.models.log').read_text())
 def test_review_fix_can_use_cursor(self):
  self.configure_pair(cursor_first=False)
  result=self.call(str(ROOT/'bin/godfather'),'run','--no-isolate','--review-by','auto','--review-fix','-p','Do task','fix')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  args=json.loads((self.root/'calls').read_text().splitlines()[-1])['args']
  self.assertIn('--force',args);self.assertEqual('disabled',args[args.index('--sandbox')+1])
 def test_tool_oneof_skips_default_fields(self):
  book=stream.Book('cursor',started_at=1)
  for i,state in enumerate(('started','completed')):
   event=dict(type='tool_call',subtype=state,call_id='oneof',tool_call=dict(hookAdditionalContexts=[],unknownDefault={},editToolCall=dict(args=dict(path='changed.py'))))
   beat=book.observe_line(json.dumps(event),i+2)
   self.assertIn('editToolCall changed.py',beat['detail']);self.assertTrue(beat['file_change'])
   self.assertEqual('open' if state=='started' else 'close',beat['tool_state'])
  self.assertFalse(book.open_tools)
  event=dict(type='tool_call',subtype='started',tool_call=dict(hookAdditionalContexts=[],taskToolCall=dict(args=dict(description='Subtask'))))
  self.assertIn('taskToolCall',stream.classify('cursor',event)['detail'])
 def test_passive_doctor_never_starts_cursor(self):
  auth=self.root/'.cursor/auth.json';auth.parent.mkdir();auth.write_text('{"fake":true}')
  with patch.object(a,'cli_binary',side_effect=lambda cli:str(self.fake) if cli=='cursor' else None),patch.object(a.subprocess,'run',side_effect=AssertionError('CLI launched')),patch('builtins.print') as output:
   a.doctor()
   self.assertTrue(any('cursor: installed; login: saved login present' in str(call) for call in output.call_args_list))

 def test_mixed_cursor_and_codex_bridge_schema(self):
  models=router.parse_models('[[models]]\nbackend="codex"\nmodel="work"\neffort="high"\nprotocol="app-server"\n'
                            '[[models]]\nbackend="cursor"\nmodel="opus"\neffort="high"\nfast=false\nincluded_only=true\n')
  self.assertEqual('app-server',models[0]['protocol']);self.assertFalse(models[1]['fast'])
  with self.assertRaisesRegex(ValueError,'supported only for codex'):
   router.parse_models('[[models]]\nbackend="cursor"\nmodel="opus"\neffort="high"\nprotocol="app-server"')

 def test_isolated_lessons_resources_done_excerpt_and_resume(self):
  import godfather_safety as safety
  self.configure_pair()
  project=self.root/'project';project.mkdir();os.environ['CWD']=str(project)
  safety.add_lesson(project,'Verify the parser before finishing.')
  result=self.call(str(ROOT/'bin/godfather'),'run','-p','Do task','integrated')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  base=self.root/'runs/integrated';run=a.read(str(base)+'.run.json')
  self.assertEqual(str(project),run['project_cwd'])
  self.assertIn(dict(kind='scratch',path=run['cwd'],owned=True),run['resources'])
  self.assertIn('Project lessons:',json.loads((self.root/'calls').read_text().splitlines()[0])['prompt'])
  self.assertIn('report="Cursor done"',safety.done_detail(base,'exit=0 tokens=20/4'))
  os.environ['CWD']=str(self.root)
  result=self.call(str(ROOT/'bin/agents-nudge'),'--wait','integrated','Verify again')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  resumed=a.read(str(base)+'.run.json')
  self.assertEqual(run['resources'],resumed['resources'])
  self.assertEqual(str(project),resumed['project_cwd'])
  self.assertIn('Verify the parser before finishing.',json.loads((self.root/'calls').read_text().splitlines()[-1])['prompt'])

 def test_structured_quota_wait_resumes_exact_cursor_chat(self):
  import godfather_lifecycle as life
  self.configure_pair()
  config=self.root/'config/godfather/models.toml'
  config.write_text(config.read_text().rsplit('[[models]]',1)[0])
  reset=int(time.time())+120
  error=dict(type='result',subtype='error',is_error=True,error=dict(code='RESOURCE_EXHAUSTED',resetsAt=reset,message='quota exhausted'))
  self.fake.write_text(FAKE.replace("if os.getenv('CURSOR_FAIL'):",
      "if '--resume' not in args:\n print("+repr(json.dumps(error))+");sys.exit(1)\nif os.getenv('CURSOR_FAIL'):"))
  os.environ['GODFATHER_QUEUE_DAEMON']='1'
  result=self.call(str(ROOT/'bin/godfather'),'run','--no-isolate','-p','Do task','waiting')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  base=self.root/'runs/waiting';run=a.read(str(base)+'.run.json')
  self.assertEqual(('waiting_quota','quota','structured'),(run['status'],run['failure']['category'],run['failure']['source']))
  self.assertEqual(reset+60,run['resume_at'])
  job=a.read(life.queue_path('waiting'));job['resume_at']=time.time()-1
  from godfather_safety import atomic
  atomic(life.queue_path('waiting'),job)
  self.assertEqual(0,life.execute_quota(base))
  self.assertEqual('done',a.read(str(base)+'.run.json')['status'])
  args=json.loads((self.root/'calls').read_text().splitlines()[-1])['args']
  self.assertEqual('chat-cursor-123',args[args.index('--resume')+1])

 def test_review_rounds_resume_cursor_worker_with_saved_options(self):
  self.configure_pair()
  grok=self.root/'grok'
  reviewer="""marker=Path.home()/'review-count'
count=int(marker.read_text()) if marker.exists() else 0
marker.write_text(str(count+1))
reply='```json\\n'+json.dumps(dict(findings=[] if count else [dict(severity='high',file='parser.py',line=1,problem='clear bug')]))+'\\n```'
"""
  grok.write_text(grok.read_text().replace("elif cli == 'grok':\n    print(json.dumps(dict(type='text', data='OK')))",
      "elif cli == 'grok':\n"+''.join('    '+line+'\n' for line in reviewer.splitlines())+"    print(json.dumps(dict(type='text', data=reply)))"))
  result=self.call(str(ROOT/'bin/godfather'),'run','--no-isolate','--review-by','auto','--review-rounds','1','-p','Do task','rounds')
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  calls=[json.loads(line) for line in (self.root/'calls').read_text().splitlines()]
  self.assertEqual(2,len(calls));self.assertIn('--resume',calls[1]['args']);self.assertIn('clear bug',calls[1]['prompt'])
  run=a.read(self.root/'runs/rounds.run.json')
  self.assertEqual(('done',1,1),(run['status'],run['review_round'],run['review_rounds']))
  self.assertEqual('1',run['launcher_env']['GODFATHER_REVIEW_ROUNDS'])

 def test_parallel_plan_cursor_tasks_resume_their_saved_chats(self):
  self.configure_pair()
  project=self.root/'project';project.mkdir()
  # The bundled CLI uses <data>/chats/md5(absolute workspace)/<id>/store.db.
  extra="""workspace=__import__('hashlib').md5(os.path.abspath(os.getcwd()).encode()).hexdigest()
db=Path(os.environ['CURSOR_DATA_DIR'])/'chats'/workspace/'chat-cursor-123'/'store.db'
db.parent.mkdir(parents=True,exist_ok=True);db.touch()
if '--resume' in args: Path('fixed').touch()
"""
  self.fake.write_text(FAKE.replace("print(json.dumps(dict(type='system'",extra+"print(json.dumps(dict(type='system'",1))
  spec=self.root/'plan.toml'
  spec.write_text('schema=1\n[plan]\nname="cursorplan"\nrepo='+json.dumps(str(project))+'\nmax_parallel=2\n'+''.join(
      '\n[[tasks]]\nid="'+task+'"\nbrief="Do task"\ndeps=[]\ntask_type="edit"\ncheck="test -f fixed"\nmax_fix_rounds=1\n' for task in ('a','b')))
  result=self.call(str(ROOT/'bin/godfather'),'plan','run',str(spec))
  self.assertEqual(0,result.returncode,result.stdout+result.stderr)
  calls=[json.loads(line) for line in (self.root/'calls').read_text().splitlines()]
  self.assertEqual(4,len(calls));self.assertEqual(2,sum('--resume' in call['args'] for call in calls))
  import godfather_plan as plan
  import godfather_store as store
  self.assertEqual({'done'},{row['status'] for row in store.plan_runs(self.root/'runs','cursorplan')})
  for task in ('a','b'):
   run=a.read(self.root/('runs/cursorplan-'+task+'.try0.run.json'))
   self.assertTrue(run['resources']);self.assertTrue(plan._resumable(run,{}))
   self.assertFalse(plan._resumable(dict(run,cursor_home=str(self.root/'other-account')),{}))
   self.assertFalse(plan._resumable(run,dict(model_hints=['grok:grok-test'])))

 def test_passive_doctor_reports_cursor_install_drift(self):
  import io
  from contextlib import redirect_stdout
  prefix=self.root/'installed';prefix.mkdir()
  name='godfather_cursor.py';(prefix/name).write_text('# old installed helper\n')
  (prefix/'.godfather-install.json').write_text(json.dumps(dict(_source_bin=str(ROOT/'bin'),**{name:'old-digest'})))
  output=io.StringIO();drift=a.install_drift
  with redirect_stdout(output),patch.object(a,'install_drift',side_effect=lambda:drift(prefix)),patch.object(a,'cli_binary',return_value=None):
   a.doctor()
  self.assertIn('install differs from source: '+name,output.getvalue())
