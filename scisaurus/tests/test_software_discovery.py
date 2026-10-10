import io
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.software_discovery import (
    PublicSourceClient, DiscoveryFailure, accepted_survey_sources, digest,
    public_addresses, public_url, retain_sources,
)
from scisaurus.runtime.software_workbench import SoftwareWorkbench
from scisaurus.runtime.software_workbench import selection_contract
from scisaurus.runtime.specialists import SpecialistDispatcher
from scisaurus.runtime.models import ModelResult


class Response(io.BytesIO):
    def __init__(self, body=b'', status=200, **headers):
        super().__init__(body)
        self.status, self.headers = status, headers
    def read1(self, size):
        return self.read(size)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.pages = {}
        self.client = PublicSourceClient(self.root,deadline=time.monotonic()+30,opener=self.open)

    def open(self, request, *, timeout):
        self.calls.append(request.full_url)
        if request.full_url.endswith('/robots.txt'):
            return Response(status=404)
        return Response(**self.pages[request.full_url])

    def test_html_follows_links_and_pages_preserving_capture(self):
        self.pages['https://example.org/paper'] = dict(body=b'<h1>Mechanism</h1><script>ignore</script><a href="/code">Repository</a>', **{'Content-Type':'text/html'})
        first = self.client.read('https://example.org/paper',max_chars=5)
        self.assertFalse(first['complete'])
        self.assertNotIn('ignore',first['text'])
        self.assertEqual(first['links'],['https://example.org/code'])
        self.assertEqual((self.root/'captures'/first['capture_sha256']).read_bytes(),self.pages['https://example.org/paper']['body'])
        rest = self.client.read('https://example.org/paper',start=first['next_start'])
        self.assertEqual(first['text_sha256'],rest['text_sha256'])

    def test_pagination_uses_immutable_capture_when_live_page_changes(self):
        url='https://example.org/doc'
        self.pages[url]=dict(body=b'original text',**{'Content-Type':'text/plain'})
        work=SoftwareWorkbench(self.root,deadline=time.monotonic()+20,source_opener=self.open)
        first=work.execute({'operation':'fetch_source','arguments':{'url':url,'max_chars':4}})
        self.pages[url]['body']=b'changed text'
        second=work.execute({'operation':'fetch_source','arguments':{'url':url,'start':4,'capture_ref':first['receipt_ref']}})
        self.assertEqual(second['result']['text'],'inal text')
        self.assertEqual(first['result']['capture_sha256'],second['result']['capture_sha256'])
        self.assertEqual(self.calls.count(url),1)

    def test_provider_challenge_is_a_failure_not_empty_search(self):
        url='https://html.duckduckgo.com/html/?q=mechanism'
        self.pages[url]=dict(body=b'challenge',status=202,**{'Content-Type':'text/html'})
        with patch.dict('os.environ',{},clear=True), self.assertRaises(DiscoveryFailure) as caught:
            self.client.search('mechanism')
        self.assertEqual(caught.exception.record['outcome'],'challenge')
        self.assertEqual((self.root/'captures'/caught.exception.record['capture_sha256']).read_bytes(),b'challenge')

    def test_search_result_and_verified_empty_marker(self):
        url='https://html.duckduckgo.com/html/?q=mechanism'
        self.pages[url]=dict(body=b'<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fcode">Code</a>')
        with patch.dict('os.environ',{},clear=True):
            result=self.client.search('mechanism')
            self.assertEqual(result['discoveries'],[{'title':'Code','url':'https://example.org/code'}])
            self.pages[url]=dict(body=b'No results found')
            self.assertEqual(self.client.search('mechanism')['discoveries'],[])
            self.pages[url]=dict(body=b'Unrecognized page')
            with self.assertRaises(DiscoveryFailure): self.client.search('mechanism')

    def test_robots_denial_never_fetches_source(self):
        def blocked(request, *, timeout):
            self.calls.append(request.full_url)
            return Response(b'User-agent: *\nDisallow: /paper\n')
        self.client.opener=blocked
        with self.assertRaises(DiscoveryFailure) as caught: self.client.read('https://example.org/paper')
        self.assertEqual(caught.exception.record['outcome'],'robots_denied')
        self.assertEqual(self.calls,['https://example.org/robots.txt'])

    def test_redirect_rechecks_policy_and_private_dns_rejected(self):
        self.pages['https://example.org/paper']=dict(status=302,Location='https://other.org/doc')
        self.pages['https://other.org/doc']=dict(body=b'doc',**{'Content-Type':'text/plain'})
        result=self.client.read('https://example.org/paper')
        self.assertEqual(result['text'],'doc')
        self.assertIn('https://other.org/robots.txt',self.calls)
        with patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValidationError):public_addresses('example.org')
        for url in ('file:///etc/passwd','http://example.org','https://user:secret@example.org','https://example.org:444','https://example.org./'):
            with self.assertRaises(ValidationError):public_url(url)

    def test_scope_and_snapshot_integrity_are_checked_before_cached_read(self):
        catalog=retain_sources(self.root,[{'text':'software https://github.com/upstream/engine','title':'Paper','representation':'abstract'}])
        ref=catalog[0]['source_ref']
        work=SoftwareWorkbench(self.root,deadline=time.monotonic()+20,evidence_refs=[ref])
        read={'operation':'read_evidence','arguments':{'source_ref':ref}}
        result=work.execute(read)
        self.assertEqual(result['result']['representation'],'abstract')
        self.assertEqual(result['result']['links'],['https://github.com/upstream/engine'])
        found=work.execute({'operation':'search_evidence','arguments':{'terms':['unmatched','software']}})
        self.assertEqual(len(found['result']['matches']),1)
        other=SoftwareWorkbench(self.root,deadline=time.monotonic()+20)
        with self.assertRaises(ValidationError):other.execute(read)
        with self.assertRaises(ValidationError):work.execute({'operation':'read_evidence','arguments':{'source_ref':{}}})
        (self.root/'evidence'/(ref.split(':')[-1]+'.json')).write_text('{}')
        with self.assertRaises(ValidationError):work.execute(read)

    def test_source_error_retains_exact_http_and_cooldown(self):
        self.pages['https://example.org/doc']=dict(body=b'limited',status=429,**{'Retry-After':'10'})
        work=SoftwareWorkbench(self.root,deadline=time.monotonic()+20,source_opener=self.open)
        result=work.execute({'operation':'fetch_source','arguments':{'url':'https://example.org/doc'}})
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual(result['discovery']['http_status'],429)
        self.assertGreater(result['retry_not_before_epoch'],time.time())

    def test_incomplete_transport_is_a_failed_receipt_with_partial_segment(self):
        class Broken(Response):
            def read1(self,size):raise http.client.IncompleteRead(b'partial segment',100)
        def opener(request,*,timeout):
            return Response(status=404) if request.full_url.endswith('/robots.txt') else Broken()
        work=SoftwareWorkbench(self.root,deadline=time.monotonic()+20,source_opener=opener)
        result=work.execute({'operation':'fetch_source','arguments':{'url':'https://example.org/doc'}})
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual(result['discovery']['outcome'],'transport_error')
        self.assertEqual(result['discovery']['request_url'],'https://example.org/doc')
        self.assertFalse(result['discovery']['complete_source_available'])
        self.assertEqual((self.root/'captures'/result['discovery']['partial_segment_sha256']).read_bytes(),b'partial segment')
        self.assertEqual(json.loads(next((self.root/'actions').glob('*.json')).read_text())['status'],'finished')

    def test_accepted_bundle_reads_exact_versions_not_new_head(self):
        (self.root/'state').mkdir(); (self.root/'objects/sha256').mkdir(parents=True)
        db=sqlite3.connect(self.root/'state/control.sqlite')
        db.execute('CREATE TABLE artifacts(logical_id TEXT,version INTEGER,manifest_json TEXT)')
        db.execute('CREATE TABLE accepted_heads(logical_id TEXT,accepted_version INTEGER)')
        def put(logical,version,value):
            body=canonical_bytes(value); sha=digest(body);ref=f'artifact:{logical}@{version}'
            (self.root/'objects/sha256'/sha).write_bytes(body)
            db.execute('INSERT INTO artifacts VALUES(?,?,?)',(logical,version,json.dumps({'body_hash':sha,'artifact_ref':ref})))
            return ref,sha
        work,_=put('kb/works/example',1,{'work_id':'example','title':'Original title'})
        source,sha=put('kb/abstracts/example',1,{'work_id':'example','text':'Exact source','representation':'abstract'})
        put('kb/abstracts/example',2,{'work_id':'example','text':'New head','representation':'abstract'})
        score,_=put('command/scores/current',1,{'schema_version':'literature-survey-score-1','survey':{'question':'Does the design work?'}})
        put('kb/surveys/current',1,{'schema_version':'literature-survey-3','score_ref':score,'source_refs':[source],'work_refs':[work]})
        db.execute("INSERT INTO accepted_heads VALUES('kb/surveys/current',1)");db.commit();db.close()
        result=accepted_survey_sources(self.root, survey_ref='artifact:kb/surveys/current@1', question='Does the design work?')
        self.assertEqual(result['sources'][0]['text'],'Exact source')
        self.assertEqual(result['sources'][0]['origin_sha256'],sha)
        with self.assertRaises(ValidationError):
            accepted_survey_sources(self.root, survey_ref='artifact:kb/surveys/current@2')
        with self.assertRaises(ValidationError):
            accepted_survey_sources(self.root, question='Another question')
        (self.root/'objects/sha256'/sha).write_text('{}')
        with self.assertRaises(ValidationError):accepted_survey_sources(self.root)

    def test_no_store_explicitly_unavailable(self):
        self.assertEqual(accepted_survey_sources(self.root)['status'],'unavailable')

    def test_composer_binds_transitive_survey_catalog_and_refreshes_identity(self):
        from types import SimpleNamespace
        from scisaurus.runtime.composer import ComposerRunner
        class Captured(Exception):
            pass
        source={'origin_ref':'artifact:kb/abstracts/example@1','origin_sha256':'a'*64,
                'bundle_ref':'artifact:kb/surveys/current@1','bundle_sha256':'b'*64,
                'work_id':'example','title':'Scientific mechanism','doi':None,'url':None,
                'representation':'abstract','identity_verified':True,'text':'Exact accepted source'}
        stage={'id':'experiment','kind':'experiment','depends_on':['bridge']}
        runner=SimpleNamespace(root=self.root,context={'survey':{'project_dir':'accepted-survey/continuations/cycle-11',
                'survey_ref':source['bundle_ref']}},workflow={'stages':[
            {'id':'survey','kind':'survey','project_dir':'accepted-survey','depends_on':[]},
            {'id':'unrelated','kind':'survey','project_dir':'unrelated-survey','depends_on':[]},
            {'id':'bridge','kind':'argument','depends_on':['survey']},stage]},
            store=SimpleNamespace(head=lambda _:None), _software_author_backend_config=lambda:None)
        captured=[]
        def publish(logical,kind,body,actor):
            captured.append((logical,body))
            raise Captured()
        runner._publish=publish
        with patch('scisaurus.runtime.software_discovery.accepted_survey_sources',
                   return_value={'status':'available','bundle_ref':source['bundle_ref'],
                                 'bundle_sha256':source['bundle_sha256'],'sources':[source]}) as load:
            with self.assertRaises(Captured):
                ComposerRunner._assess_scientific_software(runner,stage,{}, {'topic':{'id':'question','research_question':'Does the design work?'}})
            load.assert_called_once_with('accepted-survey/continuations/cycle-11', survey_ref=source['bundle_ref'], question='Does the design work?')
            first=captured[-1]
            catalog=first[1]['evidence_catalog']
            self.assertEqual(catalog[0]['origin_ref'],source['origin_ref'])
            self.assertIn(catalog[0]['source_ref'],first[1]['source_ref_catalog'])
            retained=json.loads((self.root/'scientific-software/evidence'/
                                 (catalog[0]['source_ref'].split(':')[-1]+'.json')).read_text())
            self.assertEqual(retained['text'],source['text'])
            source['text']='New accepted source'
            with self.assertRaises(Captured):
                ComposerRunner._assess_scientific_software(runner,stage,{}, {'topic':{'id':'question','research_question':'Does the design work?'}})
            self.assertNotEqual(first[0],captured[-1][0])

    def test_brave_key_not_in_output_and_redirect_not_followed(self):
        url='https://api.search.brave.com/res/v1/web/search?q=mechanism'
        self.pages[url]=dict(body=b'{"web":{"results":[{"url":"https://example.org","title":"Code"}]}}')
        with patch.dict('os.environ',{'BRAVE_SEARCH_API_KEY':'test-secret'}):
            result=self.client.search('mechanism')
        self.assertNotIn('test-secret',json.dumps(result))
        self.assertEqual(result['discoveries'][0]['title'],'Code')
        self.pages[url]=dict(status=302,Location='https://other.org')
        with patch.dict('os.environ',{'BRAVE_SEARCH_API_KEY':'test-secret'}), self.assertRaises(DiscoveryFailure):
            self.client.search('mechanism')
        self.assertNotIn('https://other.org',self.calls)
        self.pages[url]=dict(body=b'{"error":"upstream failure"}')
        with patch.dict('os.environ',{'BRAVE_SEARCH_API_KEY':'test-secret'}), self.assertRaises(DiscoveryFailure) as caught:
            self.client.search('mechanism')
        self.assertEqual(caught.exception.record['outcome'],'parse_error')

    def test_each_new_tool_observation_has_its_own_format_repair(self):
        catalog=retain_sources(self.root,[{'text':'mechanism code https://github.com/upstream/engine','title':'Paper'}])
        ref=catalog[0]['source_ref']
        final=selection_contract();final.update(decision='hold',summary='Fit review incomplete')
        final['software_selection'].update(strategy='unavailable',rationale='Candidate not yet assessed')
        values=['invalid',{'tool_action':{'operation':'read_evidence','arguments':{'source_ref':ref}}},
                {'response':{'tool_action':{'operation':'search_evidence','arguments':{'terms':['code']}}}},
                {'tool_action':{'operation':'search_evidence','arguments':{'terms':['code']}}},final]
        results=[ModelResult(value if isinstance(value,str) else json.dumps(value),'fixture',{'model_calls':1},.01,'stop',1) for value in values]
        dispatcher=SpecialistDispatcher({'protocol':'openai_compatible','base_url':'http://127.0.0.1:1/v1','model':'fixture','timeout_seconds':10,'max_output_tokens':1000},
            deadline=time.monotonic()+20,software_workspace=str(self.root))
        with patch('scisaurus.runtime.specialists.ModelClient') as client, \
                patch.object(SoftwareWorkbench,'_check_environment',return_value={'fixture':True}):
            client.return_value.complete.side_effect=results
            report=dispatcher._execute({'assigned_role':'methods.methodologist','role_id':'methodologist',
                '_software_tools':True,'_response_contract':'software_selection',
                '_prompt':json.dumps({'software_assessment_request':{'evidence_catalog':catalog}}),
                'quota':{'max_calls':None,'max_output_tokens':10000}}, {})
        self.assertEqual(report['status'],'succeeded')
        self.assertEqual(report['usage']['model_calls'],5)
        self.assertEqual(len(report['retry_history']),2)
        self.assertEqual(len(report['software_tool_results']),3)

    def test_repeated_invalid_response_without_tool_progress_still_exhausts(self):
        dispatcher=SpecialistDispatcher({'protocol':'openai_compatible','base_url':'http://127.0.0.1:1/v1','model':'fixture','timeout_seconds':10,'max_output_tokens':1000},
            deadline=time.monotonic()+20,software_workspace=str(self.root))
        with patch('scisaurus.runtime.specialists.ModelClient') as client, \
                patch.object(SoftwareWorkbench,'_check_environment',return_value={'fixture':True}):
            client.return_value.complete.return_value=ModelResult('invalid','fixture',{'model_calls':1},.01,'stop',1)
            report=dispatcher._execute({'assigned_role':'methods.methodologist','role_id':'methodologist',
                '_software_tools':True,'_response_contract':'software_selection','_prompt':'{}',
                'quota':{'max_calls':None,'max_output_tokens':10000}}, {})
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['usage']['model_calls'],2)

    def test_distinct_parsed_contract_errors_receive_their_own_correction(self):
        final=selection_contract();final.update(decision='hold',summary='Prerequisites incomplete')
        final['software_selection'].update(strategy='unavailable',rationale='No admitted execution')
        wrong=json.loads(json.dumps(final))
        wrong['software_selection']['environment_ref']='software:sha256:'+'a'*64
        values=[{'response':final},wrong,final]
        results=[ModelResult(json.dumps(value),'fixture',{'model_calls':1},.01,'stop',1) for value in values]
        dispatcher=SpecialistDispatcher({'protocol':'openai_compatible','base_url':'http://127.0.0.1:1/v1',
            'model':'fixture','timeout_seconds':10,'max_output_tokens':1000},
            deadline=time.monotonic()+20,software_workspace=str(self.root))
        with patch('scisaurus.runtime.specialists.ModelClient') as client, \
                patch.object(SoftwareWorkbench,'_check_environment',return_value={'fixture':True}):
            client.return_value.complete.side_effect=results
            report=dispatcher._execute({'assigned_role':'methods.methodologist','role_id':'methodologist',
                '_software_tools':True,'_response_contract':'software_selection','_prompt':'{}',
                'quota':{'max_calls':None,'max_output_tokens':10000}}, {})
        self.assertEqual(report['status'],'succeeded')
        self.assertEqual(report['usage']['model_calls'],3)
        self.assertEqual(len(report['retry_history']),2)
        requests=client.return_value.complete.call_args_list
        self.assertIn('non-reuse selection must not claim',str(requests[2]))

    def test_different_malformed_json_positions_share_one_correction(self):
        results=[ModelResult(value,'fixture',{'model_calls':1},.01,'stop',1) for value in ['{','{"broken":}']]
        dispatcher=SpecialistDispatcher({'protocol':'openai_compatible','base_url':'http://127.0.0.1:1/v1',
            'model':'fixture','timeout_seconds':10,'max_output_tokens':1000},
            deadline=time.monotonic()+20,software_workspace=str(self.root))
        with patch('scisaurus.runtime.specialists.ModelClient') as client, \
                patch.object(SoftwareWorkbench,'_check_environment',return_value={'fixture':True}):
            client.return_value.complete.side_effect=results
            report=dispatcher._execute({'assigned_role':'methods.methodologist','role_id':'methodologist',
                '_software_tools':True,'_response_contract':'software_selection','_prompt':'{}',
                'quota':{'max_calls':None,'max_output_tokens':10000}}, {})
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['usage']['model_calls'],2)


if __name__ == '__main__':unittest.main()
