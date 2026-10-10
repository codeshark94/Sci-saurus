"""A single controller-bound laboratory interface survives authoring and replay."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.laboratory import LaboratoryBinding, provision_laboratory, seal_attestation
from scisaurus.runtime.programs import LocalProgramClient
from scisaurus.tests import test_laboratory as lab_tests, test_dsh_batch as batch_tests


class LaboratoryExecutionSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.work = self.root / 'work'
        self.work.mkdir()
        fixture = lab_tests.ProvisioningTests()
        executable, lock = fixture._wrapper(self.root)
        lab = fixture._laboratory(self.root, executable, lock)
        lab['runtimes'][0]['read_only_roots'].extend([
            str(Path(sys.executable).absolute().parent.parent),
            str(Path(sys.executable).resolve().parent.parent)])
        # A real native sandbox probe, never an unsandboxed attestation.
        attestation = provision_laboratory(lab, self.root / 'prep', deadline=time.monotonic() + 60)
        self.assertTrue(attestation["runtimes"][0]["verified"], attestation["runtimes"][0]["probe"])
        self.binding = LaboratoryBinding(lab, attestation)

    def test_native_child_invocation_and_source_replay(self):
        value = self.binding.execution_binding()
        surface = self.binding.execution_surface(self.work)
        self.assertEqual(list(json.loads(surface['environment']['SCI_LABORATORY_RUNTIMES'])), ['runtime'])
        source = self.work / 'executor.py'
        source.write_text('''import json, os, subprocess
from pathlib import Path
r = json.loads(os.environ['SCI_LABORATORY_RUNTIMES'])['runtime']
p = Path('solver.py')
p.write_text("import json; print(json.dumps({'value':7}))")
x = subprocess.run([r['executable'], str(p)], env={**os.environ, **r['environment']},
                   capture_output=True, text=True, check=True, timeout=10)
print(json.dumps({'solver':json.loads(x.stdout)}))
''')
        client = LocalProgramClient([sys.executable, str(source)], cwd=str(self.work), env={},
                                    timeout=30, max_bytes=50000, sandbox_required=True, laboratory=value)
        result = client.run({'original': 'input'})
        self.assertEqual(result['outcome'], 'ok', result)
        self.assertEqual(result['document'], {'solver': {'value': 7}})
        self.assertEqual(result['input'], {'original': 'input'})
        self.assertEqual(result['metadata']['laboratory_runtime_identities'], surface['identities'])
        replay = client.run({'original': 'input'})
        self.assertEqual(result['capture_sha256'], replay['capture_sha256'])

    def test_binding_drift_and_foreign_attestation_fail_closed(self):
        original = self.binding.execution_binding()
        wrong = deepcopy(original)
        wrong['configuration']['scope']['objective'] = 'changed'
        with self.assertRaisesRegex(ValidationError, 'another configuration'):
            LaboratoryBinding.from_execution_binding(wrong)
        for labels in (["runtime", "runtime"], ["foreign"], [[]]):
            with self.assertRaises(ValidationError):
                self.binding.for_execution(labels)
        extra = {**original, 'executable': sys.executable}
        with self.assertRaises(ValidationError):
            LaboratoryBinding.from_execution_binding(extra)
        runtime = self.binding.runtime('runtime')
        Path(runtime['executable']).write_text('#!/bin/sh\nexit 0\n')
        with self.assertRaisesRegex(ValidationError, 'drifted'):
            self.binding.execution_surface(self.work)

    def test_unverified_or_unsandboxed_runtime_is_not_exported(self):
        a = deepcopy(self.binding.attestation)
        a['runtimes'][0]['probe']['mode'] = 'unsandboxed'
        binding = LaboratoryBinding(self.binding.laboratory, seal_attestation(a))
        surface = binding.execution_surface(self.work)
        self.assertEqual(json.loads(surface['environment']['SCI_LABORATORY_RUNTIMES']), {})
        self.assertEqual(surface['read_only_paths'], ())
        with self.assertRaises(ValueError):
            LocalProgramClient([sys.executable], cwd=str(self.work), env={}, timeout=1,
                               max_bytes=100, laboratory=self.binding.execution_binding())

    def test_author_and_blinded_validator_receive_operational_inventory(self):
        from scisaurus.runtime.dsh_batch import DshAuthorClient, DshValidatorClient
        fixture = batch_tests.BatchTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        config = fixture.config()
        for client_type, output in ((DshAuthorClient, {'executor.py': b"print('source')", 'intent.json': b'{}'}),
                                    (DshValidatorClient, {'validator.py': b"print('validator')"})):
            client = client_type(config, root=self.root / client_type.__name__, runtime_python=sys.executable,
                                 laboratory=self.binding)
            self.assertTrue(client.runner.root.is_dir())
            seen = {}
            def run(task, **options):
                seen.update(task=task, **options)
                return {'files': output, 'usage': {'model_calls': 1}, 'receipt': 'owned', 'elapsed_seconds': 1}
            prompt = {'configured_input': {'science': 7}}
            with patch.object(client.runner, 'run', side_effect=run):
                client.complete(system='', prompt=json.dumps(prompt))
            context = json.loads(seen['inputs']['laboratory.json'])
            self.assertEqual(list(context['runtime_identities']), ['runtime'])
            self.assertGreater(context['host_resources']['storage']['free_bytes'], 0)
            self.assertIn('SCI_LABORATORY_RUNTIMES', context['execution']['runtime_access'])
            self.assertIn('laboratory.json', seen['task'])
            self.assertNotIn('executor_source', canonical_bytes(context).decode())

    def test_admission_registration_and_replay_preserve_solver_binding(self):
        import hashlib
        from scisaurus.tests import test_program_foundry as fixtures
        from scisaurus.runtime.program_admission import validate_program_candidate
        from scisaurus.runtime.program_gates import admit_program_candidate
        from scisaurus.runtime.capability_registry import register_capability, load_registry
        from scisaurus.runtime.program_sandbox import run_sandboxed
        value = fixtures.RegistryTests._candidate()
        document = fixtures.output_document()
        source = """import json,os,pathlib,subprocess
r = json.loads(os.environ['SCI_LABORATORY_RUNTIMES'])['runtime']
p = pathlib.Path('child.py')
p.write_text("print(7)")
x = subprocess.run([r['executable'],str(p)],env={**os.environ,**r['environment']},
                   capture_output=True,text=True,check=True,timeout=10)
if int(x.stdout) != 7:
    raise RuntimeError('incorrect child output')
""" + 'print(' + repr(json.dumps(document)) + ')'
        value['executor_source'] = source
        value['test_vector']['expected_output_sha256'] = fixtures.expected_digest(document)
        binding = self.binding.execution_binding()
        with self.assertRaisesRegex(ValidationError, 'forbidden modules'):
            validate_program_candidate(value)
        validate_program_candidate(value, laboratory_execution=binding)
        program = self.work / 'program.py'
        program.write_text(source)
        surface = self.binding.execution_surface(self.work)
        def execute(payload):
            return run_sandboxed([sys.executable,str(program)],workspace=self.work,
                                 input_bytes=payload,timeout_seconds=30,
                                 env=surface['environment'],read_only_paths=surface['read_only_paths'])
        admission = admit_program_candidate(value, execute=execute,
            validate=fixtures.GateTests()._validate(),
            readiness=lambda:fixtures.Result(b'{"status":"ready"}'),
            laboratory_execution=binding)
        requirements = self.root / 'requirements.txt'
        requirements.write_text('numpy==2.5.2')
        registry = register_capability(self.root / 'registry',value,admission,
            runtime_python=sys.executable,repo_root=self.root,requirements_file=requirements,
            laboratory_execution=binding)
        load_registry(self.root / 'registry')
        descriptor = json.loads(Path(registry['descriptor_path']).read_text())
        options = descriptor['experiment']['execution']['client']
        self.assertEqual(options['laboratory'],binding)
        actual = LocalProgramClient(**options).run(value['test_vector']['input'])
        self.assertEqual(actual['outcome'],'ok',actual)
        self.assertEqual(actual['document'],document)
        self.assertEqual(actual['metadata']['laboratory_execution_sha256'],
                         hashlib.sha256(canonical_bytes(binding)).hexdigest())
        with self.assertRaisesRegex(ValidationError, 'does not bind'):
            register_capability(self.root / 'foreign',value,admission,
                runtime_python=sys.executable,repo_root=self.root,requirements_file=requirements,
                laboratory_execution=self.binding.for_execution([]).execution_binding())
