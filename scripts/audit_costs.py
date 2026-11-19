#!/usr/bin/env python3
"""Read local state and selected existing event logs; no model or network calls."""
import argparse,collections,json,hashlib
from pathlib import Path
from mod_curator.store import Store
from mod_curator.engine import Engine
from mod_curator.facts import normalize

def measure(job_id, paths=()):
 s=Store();j=s.get(job_id);projects=list(j['projects'].values());described=[p for p in projects if p.get('description')]
 calls=collections.Counter();repeats=collections.Counter();response_sizes=[];session_models=[];contexts=0
 for path in paths:
  for line in Path(path).open():
   try:r=json.loads(line)
   except ValueError:continue
   p=r.get('payload',{})
   if r.get('type')=='turn_context':
    session_models.append(p.get('model'));contexts+=1
   if r.get('type')=='response_item' and p.get('type') in ('function_call','custom_tool_call'):
    name=p.get('name','')
    if 'minecraft_mod_curator' in name:
     args=p.get('arguments',p.get('input',''));calls[name]+=1
     if 'analysis_batch' in name:repeats[args if isinstance(args,str) else json.dumps(args,sort_keys=True)]+=1
   if r.get('type')=='response_item' and p.get('type') in ('function_call_output','custom_tool_call_output'):
    response_sizes.append(len(json.dumps(p.get('output',''),ensure_ascii=False).encode()))
 sample=described[:25]
 def size(x):return len(json.dumps(x,ensure_ascii=False).encode())
 old_shape=[p for p in sample]
 old_filtered=[{k:v for k,v in p.items() if k!='metadata'} for p in sample]
 return {'job_id':job_id,'projects':len(projects),'descriptions':len(described),'legacy_decisions':len(j['decisions']),
 'raw_description_characters':sum(len(p['description']) for p in described),'normalized_description_characters':sum(len(normalize(p['description'])) for p in described),
 'maximum_description_characters':max(map(lambda p:len(p['description']),described)),
 'sample_25_full_project_bytes':size(old_shape),'sample_25_without_metadata_bytes':size(old_filtered),
 'status_full_bytes':size(Engine(s).status(job_id)),'direct_mcp_calls_in_supplied_logs':dict(calls),
 'repeated_analysis_request_signatures':sum(v-1 for v in repeats.values()),'examined_log_files':len(paths),
 'largest_tool_response_bytes':max(response_sizes,default=0),'session_models_seen':sorted(set(session_models)),
 'limitations':'Only directly recorded MCP calls are counted. CLI bridge requests/model request counts and subscription quota consumption are not attributable from these logs. Repeated request signatures can be legitimate after save; not proof of duplicated semantic decisions.',
 'safeguard':'Read-only diagnosis; no extraction, reanalysis or collection started.'}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('job_id');p.add_argument('--logs-list',type=Path);a=p.parse_args()
 paths=a.logs_list.read_text().splitlines() if a.logs_list else []
 print(json.dumps(measure(a.job_id,paths),ensure_ascii=False,indent=2))
