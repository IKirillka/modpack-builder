"""Optional isolated Codex CLI child worker; subscription login, no model API key.

Native subagents can use the same claim/commit protocol. This runner never
silently substitutes a model, and only compact progress reaches the parent.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .analysis_roles import DEFAULT_MODEL, MODELS
from .facts import Facts, ReferenceResponse, referenced_prompt
from .store import Store


def observe_session(thread_id):
    root=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))/'sessions'
    import re
    if not isinstance(thread_id,str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',thread_id):
        return {}
    for path in root.rglob('*'+thread_id+'.jsonl'):
        observed={}
        with path.open() as stream:
            for line in stream:
                try:
                    row=json.loads(line)
                except ValueError:
                    continue
                p=row.get('payload',{})
                if row.get('type')=='turn_context':
                    observed={'model':p.get('model'),'reasoning_effort':p.get('effort') or p.get('reasoning_effort'),'session_id':thread_id}
        return observed
    return {}


def run_batch(store, batch, effort='medium', timeout=600):
    agent = batch.get('agent') or {'role': 'extract', 'model': DEFAULT_MODEL, 'effort': effort}
    model, effort, role = agent['model'], agent['effort'], agent['role']
    if model not in MODELS or effort not in ('low','medium','high') or role not in ('extract','retry'):
        raise ValueError('Unsupported worker model, reasoning effort or role')
    facts=Facts(store);base=store.job_dir(batch['job_id'])/'extraction-runs';base.mkdir(exist_ok=True)
    run_dir=base/batch['batch_id'];run_dir.mkdir(exist_ok=True)
    schema=run_dir/'schema.json';schema.write_text(json.dumps(ReferenceResponse.model_json_schema()))
    output=run_dir/'result.json';events=run_dir/'events.jsonl'
    prompt=referenced_prompt(batch)
    if role == 'retry':
        prompt = ('This is a retry of a factual record that did not validate or was not saved. '
                  'Address each previous_validation_error, verify evidence_ids against that project, '
                  'and preserve uncertainties. Do not make selection decisions.\n' + prompt)
    # New conversation, neutral working directory, no parent chat, user config,
    # project AGENTS, plugins or MCP servers. Auth remains in existing CODEX_HOME.
    with tempfile.TemporaryDirectory(prefix='curator-extract-') as temp:
        args=['codex','exec','--ignore-user-config','--strict-config','--skip-git-repo-check','-C',temp,
              '-m',model,'-c',f'model_reasoning_effort="{effort}"','-c','service_tier="default"',
              '--disable','apps','--disable','plugins','--disable','multi_agent','--enable','skip_host_skill_discovery','-s','read-only','--json','--output-schema',str(schema),'--output-last-message',str(output),'-']
        env={k:v for k,v in os.environ.items() if k not in ('OPENAI_API_KEY','CURSEFORGE_API_KEY')}
        start=time.monotonic()
        with events.open('w') as log,(run_dir/'stderr.log').open('w') as err:
            proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=log,stderr=err,text=True,env=env,start_new_session=True)
            proc.stdin.write(prompt);proc.stdin.close()
            try:
                while True:
                    try:
                        proc.wait(timeout=25)
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic()-start>timeout:
                            raise TimeoutError('Extraction worker timeout')
                        facts.heartbeat(batch['batch_id'])
            except BaseException:
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
                raise
        elapsed=time.monotonic()-start
    usage={};thread_id=None;model_calls=0;tools=0
    for line in events.read_text().splitlines():
        try:r=json.loads(line)
        except ValueError:continue
        if r.get('type')=='thread.started':thread_id=r.get('thread_id')
        if r.get('type')=='turn.completed':usage=r.get('usage',{});model_calls+=1
        if r.get('type')=='item.started' and r.get('item',{}).get('type') in ('command_execution','mcp_tool_call'):tools+=1
    observed=observe_session(thread_id)
    meta={'role':role,'requested_model':model,'requested_reasoning_effort':effort,'observed':observed,
          'usage':usage,'turns_reported':model_calls,'tool_calls':tools,'seconds':elapsed,'client':'codex exec child, existing Codex auth','fast':False}
    (run_dir/'metrics.json').write_text(json.dumps(meta,indent=2))
    if proc.returncode:
        raise RuntimeError(f'Codex worker {model} / {effort} failed ({proc.returncode}); inspect local stderr.log')
    if observed.get('model') != model or observed.get('reasoning_effort') != effort:
        actual = f"{observed.get('model', 'unknown')} / {observed.get('reasoning_effort', 'unknown')}"
        raise RuntimeError(f'MODEL_NOT_VERIFIED: requested {model} / {effort}, observed {actual}; facts not committed')
    # Envelope parse first, per-record validation/retry in commit.
    obj=json.loads(output.read_text())
    if not isinstance(obj,dict) or not isinstance(obj.get('records'),list):
        raise ValueError('Invalid facts response envelope')
    return facts.commit(batch['batch_id'],obj['records'],meta)


def run(job_id, max_batches=1, effort='medium', store=None):
    if not 1<=max_batches<=10000:
        raise ValueError('Explicit max_batches 1..10000 required')
    from filelock import FileLock, Timeout
    store = store or Store()
    try:
        with FileLock(store.job_dir(job_id) / "extraction.lock").acquire(timeout=0):
            return run_claims(store, job_id, max_batches, effort)
    except Timeout:
        return {'batches_completed': 0, 'status': 'already_running'}


def run_claims(store, job_id, max_batches, effort):
    facts=Facts(store);success=0
    for _ in range(max_batches):
        batch=facts.claim(job_id,'codex-cli-'+str(os.getpid()))
        if not batch.get('batch_id'):
            break
        try:
            run_batch(store,batch,effort);success+=1
        except Exception as e:
            facts.fail(batch['batch_id'],str(e))
            # Stop on transport/model errors instead of burning the whole queue.
            return {'batches_completed':success,'error':str(e),'progress':facts.progress(job_id)}
    return {'batches_completed':success,'progress':facts.progress(job_id)}


def launch(store,job_id,max_batches=1,effort='medium'):
    if not 1<=max_batches<=10000 or effort not in ('medium','high'):
        raise ValueError('Invalid explicit batch limit or effort')
    path=store.job_dir(job_id)/'extraction-worker.log'
    import sys
    with path.open('ab') as log:
        p=subprocess.Popen([sys.executable,'-m','mod_curator','extract-worker',job_id,'--batches',str(max_batches),'--effort',effort],
                           env={**os.environ,'MOD_CURATOR_HOME':str(store.root)},stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    return {'job_id':job_id,'worker_pid':p.pid,'max_batches':max_batches,'poll_after_seconds':60}
