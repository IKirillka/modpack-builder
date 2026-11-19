#!/usr/bin/env python3
"""Summarize completed, explicitly selected local benchmark runs; no model calls."""
import collections,datetime,hashlib,json
from pathlib import Path
from mod_curator.facts import FactResponse

def native_metrics(path):
    result={'observed_model':None,'observed_effort':None,'usage':None,'tool_calls':0,'mcp_calls':0,'parent_history_forked':False,'user_messages':0}
    first=last=None;updates=set()
    for line in path.open():
        row=json.loads(line);p=row.get('payload',{});first=first or row['timestamp'];last=row['timestamp']
        if row['type']=='turn_context':result.update(observed_model=p.get('model'),observed_effort=p.get('effort'))
        if row['type']=='response_item':
            if p.get('type') in ('function_call','custom_tool_call'):result['tool_calls']+=1
            if p.get('role')=='user':result['user_messages']+=1
        if row['type']=='event_msg' and p.get('type')=='token_count':
            result['usage']=p.get('info',{}).get('total_token_usage')
            if result['usage']:updates.add(result['usage']['input_tokens'])
    result['seconds']=(datetime.datetime.fromisoformat(last.replace('Z','+00:00'))-datetime.datetime.fromisoformat(first.replace('Z','+00:00'))).total_seconds()
    result['usage_updates']=len(updates);result['underlying_api_requests']='not exposed as a reliable counter'
    return result

if __name__=='__main__':
    base=Path('evidence/benchmark-v03');sessions=Path.home()/'.codex/sessions/2026/11/15'
    ids={'medium':'01a1e072-4b40-7503-bd76-a11cd724e713','high':'01a1e072-668b-7d42-9af9-870ef85730b7'}
    metrics={effort:native_metrics(next(sessions.glob('*'+sid+'.jsonl'))) for effort,sid in ids.items()}
    cli=[json.loads(p.read_text()) for p in (base/'cli-state').rglob('metrics.json')]
    raw=json.loads((base/'input.json').read_text());source={p['project_id']:p for p in raw['projects']}
    quality=[
      {'project_id':688768,'finding':'Medium omitted explicit Experience Lantern, kill-XP nugget and Sweeping Edge details; High retained them. Core XP/enchantment machinery retained by both.','route':'Targeted review for dense descriptions; retain High facts here.'},
      {'project_id':1281336,'finding':'Medium explicitly named completed accelerator/splitter/cannon features; High summarized them only as other completed planned-list entries.','route':'Do not assume High is always better; review this specific list, preserve named mechanics.'},
      {'project_id':659897,'finding':'Medium added library category although source describes compatibility/renderer modification, not a reusable API. High technical compatibility classification is more precise.','route':'Use High classification; no user exclusion is automatic.'},
      {'project_id':531834,'finding':'Description identifies ingredients/products and effects, but does not establish detailed production recipes in extracted text. Saved Sol recommendation was already conditional; neither Luna output proves recipe automation.','route':'Uncertain suitability for production, targeted source/images review if final candidate.'},
      {'project_id':888624,'finding':'Both retained actual Mechanical Chisel with recipe/filter behavior, despite decorative content; do not exclude by decorative category.','route':'Sol decides based on actual machine.'},
      {'project_id':820977,'finding':'Both distinguish legacy features/modules; High explicitly retains future multiblock oven/keg as planned.','route':'Preserve version/planned limitations; file-dependency solver remains separate.'},
      {'project_id':905861,'finding':'Medium initially changed your steam engines to their steam engines in a claimed verbatim excerpt. Strict validator rejected only that record; isolated one-record repair passed.','route':'Bounded per-record validation retry.'}]
    old=json.loads((base/'sol-existing.json').read_text())
    data={'unique_saved_descriptions':len(source),'description_characters':sum(len(p['description']) for p in source.values()),
      'selection':'12 known cases: technical, decorative, integrations, long/version-dense and ambiguous; convenience sample, not random or exhaustive',
      'sol_medium_baseline':{'records_reused':len(old),'new_model_calls':0,'model':'gpt-6.1-sol','effort':'medium','historical_tokens_for_these_records':None,'historical_seconds_for_these_records':None,'not_ground_truth':True},
      'native_luna':metrics,'native_validation':{'medium_first_pass':11,'medium_retried_records':1,'medium_after_retry':12,'high_first_pass':12},
      'isolated_cli_checks':cli,'cli_validation':{'medium_first_pass':8,'high_retry_only_records':4,'final_valid_records':12},
      'quality_review':quality,'subscription_quota_percentage':'not measurable from available client telemetry; tokens are not a quota percentage',
      'fact_cache_hits_on_initial_run':0,'fact_cache_reuse_expected_test':'12/12 on an unchanged repeat; verified programmatically without model call',
      'chosen_policy':'Luna Medium for clear bounded batches, High only for failed/dense designated subsets, Sol only targeted ambiguity/important final choices; never full Sol reread',
      'scope':'No full catalogue reanalysis or collection; original saved job/legacy decisions retained'}
    Path('evidence/benchmark-v03-summary.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
    print(json.dumps({k:{'model':v['observed_model'],'effort':v['observed_effort'],'seconds':v['seconds'],'usage':v['usage'],'tools':v['tool_calls']} for k,v in metrics.items()},ensure_ascii=False))
