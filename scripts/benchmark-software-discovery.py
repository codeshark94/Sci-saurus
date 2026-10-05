#!/usr/bin/env python3
"""Run a recorded software assessment against exact accepted literature inputs."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.software_discovery import accepted_survey_sources, digest, retain_sources
from scisaurus.runtime.software_workbench import REVISION, selection_contract, tool_contract
from scisaurus.runtime.specialists import SpecialistDispatcher


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',required=True)
    parser.add_argument('--survey-project',required=True)
    parser.add_argument('--model-config',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--seconds',type=float,required=True)
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    if args.seconds <= 0:parser.error('seconds must be positive')
    root=Path(args.output).resolve()
    if root.exists() and not args.resume:parser.error('output must be a fresh directory or use --resume')
    root.mkdir(parents=True,exist_ok=True)
    original=json.loads(Path(args.request).read_text())
    bundle=accepted_survey_sources(args.survey_project)
    request={**original,'revision':REVISION,
             'evidence_catalog':retain_sources(root/'workbench',bundle['sources']),
             'evidence_availability':[{key:value for key,value in bundle.items() if key!='sources'}]}
    request['source_ref_catalog']=sorted(set([*request.get('source_ref_catalog',[]),
                                            *[row['source_ref'] for row in request['evidence_catalog']]]))
    envelope={'software_assessment_request':request,'output_contract':selection_contract(),
              'scientific_software_tools':tool_contract()}
    prior_refs=[]
    if args.resume:
        prior_input=json.loads((root/'input.json').read_text())
        prior_request=prior_input['software_assessment_request']
        if ({key:value for key,value in prior_request.items() if key!='revision'} !=
                {key:value for key,value in request.items() if key!='revision'}):
            parser.error('resume changed the exact scientific request or accepted sources')
        prior_report=json.loads((root/'report.json').read_text())
        prior_sha=digest(canonical_bytes(prior_report))
        (root/('report-'+prior_sha+'.json')).write_bytes(canonical_bytes(prior_report))
        prior_refs=list(dict.fromkeys(row['receipt_ref'] for row in prior_report.get('software_tool_results',[]) if row.get('receipt_ref')))
        prior_input_sha=digest(canonical_bytes(prior_input))
        (root/('input-'+prior_input_sha+'.json')).write_bytes(canonical_bytes(prior_input))
    (root/'input.json').write_bytes(canonical_bytes(envelope))
    source_identity={name:digest((Path(__file__).resolve().parents[1]/'scisaurus/runtime'/name).read_bytes())
                     for name in ('software_discovery.py','software_workbench.py','specialists.py')}
    (root/('activation-'+digest(canonical_bytes(source_identity))+'.json')).write_bytes(canonical_bytes(source_identity))
    model=json.loads(Path(args.model_config).read_text())
    def progress(event):
        with (root/'events.jsonl').open('a') as stream:stream.write(json.dumps(event,ensure_ascii=False)+'\n')
        if event.get('event')=='software_tool_completed':print(event.get('operation'),event.get('status'),flush=True)
    dispatcher=SpecialistDispatcher(model,deadline=time.monotonic()+args.seconds,
        software_workspace=str(root/'workbench'),on_progress=progress)
    started=time.time()
    report=dispatcher._execute({'assigned_role':'methods.methodologist','role_id':'methodologist',
        '_software_tools':True,'_response_contract':'software_selection','_prompt':json.dumps(envelope,ensure_ascii=False),
        '_software_receipt_refs':prior_refs,
        'quota':{'max_calls':None,'max_output_tokens':1000000,'max_output_tokens_per_call':32768}}, {})
    (root/'report.json').write_bytes(canonical_bytes(report))
    cumulative_usage={}
    for value in [report,*[json.loads(path.read_text()) for path in root.glob('report-*.json')]]:
        for key,count in value.get('usage',{}).items():
            if type(count) is int and count >= 0:cumulative_usage[key]=cumulative_usage.get(key,0)+count
    tools=report.get('software_tool_results',[])
    summary={'revision':REVISION,'input_sha256':digest(canonical_bytes(envelope)),
        'scientific_request_sha256':digest(canonical_bytes({key:value for key,value in request.items() if key!='revision'})),
        'resumed_revision':prior_request.get('revision') if args.resume else None,
        'source_identity':source_identity,'resumed_operation_refs':prior_refs,
        'original_request_sha256':digest(canonical_bytes(original)),
        'accepted_bundle_ref':bundle.get('bundle_ref'),'accepted_bundle_sha256':bundle.get('bundle_sha256'),
        'started_epoch':started,'finished_epoch':time.time(),'status':report.get('status'),'usage':report.get('usage'),
        'cumulative_usage':cumulative_usage,
        'operations':[{'operation':row['action']['operation'],'outcome':row['outcome'],'receipt_ref':row.get('receipt_ref')} for row in tools],
        'acceptance_scope':'tool-led operational discovery and reproduction; separate scientific reviewer and experiment admission remain required'}
    (root/'summary.json').write_bytes(canonical_bytes(summary))
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
