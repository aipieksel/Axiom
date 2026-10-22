#!/usr/bin/env python3
"""Source-only positive and refusing controls for native seed CI evidence."""

import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('native_seeds', ROOT / 'scripts/lib/ci-native-seeds.py')
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)
WORKFLOW = (ROOT / '.github/workflows/ci.yml').read_text()
DOCUMENT = proof.parse(WORKFLOW)
TARGETS = {'windows-x86_64', 'windows-aarch64'}


def bootstrap(job):
    return next(step for step in job['steps'] if 'bootstrap-from-seed.sh' in step.get('run', ''))


class NativeSeedTests(unittest.TestCase):
    def test_current_workflow_proves_both_architectures(self):
        targets, notes = proof.native_seeds(DOCUMENT)
        self.assertEqual(targets, TARGETS)
        self.assertTrue(any('inherited applicability: grammar:' in note for note in notes))

    def test_display_names_and_comments_are_not_evidence(self):
        document = copy.deepcopy(DOCUMENT)
        for name in ('windows', 'windows-arm'):
            job = document['jobs'][name]
            job['name'] = 'Anything'
            bootstrap(job)['name'] = 'Anything else'
            bootstrap(job)['run'] = '# observation only\n' + bootstrap(job)['run'] + '\n# end'
        document['jobs']['renamed-native-job'] = document['jobs'].pop('windows')
        self.assertEqual(proof.native_seeds(document)[0], TARGETS)

    def test_optional_machine_observation_is_not_required(self):
        document = copy.deepcopy(DOCUMENT)
        for name in ('windows', 'windows-arm'):
            step = bootstrap(document['jobs'][name])
            step['run'] = '\n'.join(line for line in step['run'].splitlines()
                                    if not line.lstrip().startswith(('printf ', '"$(uname -m)"')))
        self.assertEqual(proof.native_seeds(document)[0], TARGETS)

    def test_job_and_step_refusals(self):
        controls = {
            'job guard': lambda job, step: job.update({'if': '${{ always() }}'}),
            'job disabled': lambda job, step: job.update({'if': 'false'}),
            'job advisory': lambda job, step: job.update({'continue-on-error': 'true'}),
            'step guard': lambda job, step: step.update({'if': "runner.os == 'Windows'"}),
            'step disabled': lambda job, step: step.update({'if': 'false'}),
            'step advisory': lambda job, step: step.update({'continue-on-error': '${{ true }}'}),
            'step PowerShell': lambda job, step: step.update({'shell': 'pwsh'}),
            'no Bash default': lambda job, step: job.pop('defaults'),
            'default working directory': lambda job, step: job['defaults']['run'].update({'working-directory': 'other'}),
            'dynamic runner': lambda job, step: job.update({'runs-on': '${{ matrix.os }}'}),
            'other architecture': lambda job, step: job.update({'runs-on':
                'windows-11-arm' if job['runs-on'] == 'windows-2025' else 'windows-2025'}),
            'step action': lambda job, step: step.update({'uses': './action'}),
            'step environment': lambda job, step: step.update({'env': {'BASH_ENV': 'skip.sh'}}),
            'working directory': lambda job, step: step.update({'working-directory': 'other'}),
            'job environment': lambda job, step: job.update({'env': {'PATH': 'fake-tools'}}),
            'container': lambda job, step: job.update({'container': 'other'}),
            'missing dependency': lambda job, step: job.update({'needs': 'absent'}),
            'self dependency': lambda job, step: job.update({'needs': job['_name']}),
        }
        for name, target in (('windows', 'windows-x86_64'), ('windows-arm', 'windows-aarch64')):
            for label, mutate in controls.items():
                with self.subTest(job=name, control=label):
                    document = copy.deepcopy(DOCUMENT)
                    job = document['jobs'][name]
                    job['_name'] = name
                    mutate(job, bootstrap(job))
                    self.assertNotIn(target, proof.native_seeds(document)[0])

    def test_workflow_environment_cannot_replace_tools(self):
        document = copy.deepcopy(DOCUMENT)
        document['env'] = {'BASH_ENV': 'skip.sh'}
        self.assertEqual(proof.native_seeds(document)[0], set())

    def test_explicit_bash_and_literal_true_conditions(self):
        document = copy.deepcopy(DOCUMENT)
        for name in ('windows', 'windows-arm'):
            job = document['jobs'][name]
            step = bootstrap(job)
            job.pop('defaults')
            job['if'] = 'true'
            step.update({'shell': 'bash', 'if': '${{ true }}', 'continue-on-error': 'false'})
        self.assertEqual(proof.native_seeds(document)[0], TARGETS)

    def test_recipe_refusals(self):
        controls = {
            'removed strict mode': lambda run: run.replace('set -euo pipefail', ''),
            'no pipefail': lambda run: run.replace('set -euo pipefail', 'set -eu'),
            'disabled errexit': lambda run: run.replace('set -euo pipefail', 'set +e'),
            'ignored bootstrap failure': lambda run: run.replace('--install "$seeded"', '--install "$seeded" || true'),
            'echoed bootstrap': lambda run: run.replace('./scripts/bootstrap-from-seed.sh', 'echo ./scripts/bootstrap-from-seed.sh'),
            'commented bootstrap': lambda run: run.replace('./scripts/bootstrap-from-seed.sh', '# ./scripts/bootstrap-from-seed.sh'),
            'guarded bootstrap': lambda run: run.replace('./scripts/bootstrap-from-seed.sh', 'false && ./scripts/bootstrap-from-seed.sh'),
            'function bootstrap': lambda run: run.replace('./scripts/bootstrap-from-seed.sh', 'skipped() { ./scripts/bootstrap-from-seed.sh') + '\n}',
            'early return': lambda run: 'exit 0\n' + run,
            'wrong compiler install': lambda run: run.replace('--install "$seeded"', '--install other'),
            'wrong PE file': lambda run: run.replace('--file-headers "$seeded/axiom.exe"', '--file-headers other.exe'),
            'wrong PE machine': lambda run: run.replace('IMAGE_FILE_MACHINE_', 'OTHER_MACHINE_'),
            'host compiler execution': lambda run: run.replace('"$seeded/axiom.exe" --diagnostic', 'axiom --diagnostic'),
            'wrong emission target': lambda run: run.replace('--target="$target"', '--target=linux-x86_64'),
            'wrong current source': lambda run: run.replace('core/main.ax', 'examples/hello.ax'),
            'wrong emission path': lambda run: run.replace('-o "$seeded/current.ll"', '-o other.ll'),
            'wrong compared emission': lambda run: run.replace('cmp "$seeded/current.ll"', 'cmp other.ll'),
            'wrong cross target': lambda run: run.replace('axiom-$target.ll', 'axiom-linux-x86_64.ll'),
            'comparison mentioned': lambda run: run.replace('cmp "$seeded/current.ll"', 'echo cmp "$seeded/current.ll"'),
            'bootstrap after compare': lambda run: '\n'.join(reversed(run.splitlines())),
            'extra unknown instruction': lambda run: run + '\neval "$OTHER"',
            'unclosed continuation': lambda run: run + '\n\\',
            'malformed quoting': lambda run: run + '\n"',
        }
        for name, target in (('windows', 'windows-x86_64'), ('windows-arm', 'windows-aarch64')):
            for label, mutate in controls.items():
                with self.subTest(job=name, control=label):
                    document = copy.deepcopy(DOCUMENT)
                    step = bootstrap(document['jobs'][name])
                    step['run'] = mutate(step['run'])
                    self.assertEqual(proof.native_seeds(document)[0], TARGETS - {target})

    def test_disabled_or_cyclic_ancestors(self):
        for label, changes in (
                ('disabled', {'if': 'false'}),
                ('advisory', {'continue-on-error': 'true'}),
                ('cycle', {'needs': 'windows'}),
                ('missing', {'needs': 'not-a-job'})):
            with self.subTest(control=label):
                document = copy.deepcopy(DOCUMENT)
                document['jobs']['cross'].update(changes)
                self.assertEqual(proof.native_seeds(document)[0], set())

    def test_unreadable_yaml_is_rejected(self):
        for label, text in (
                ('duplicate root key', WORKFLOW + '\njobs:\n  fake:\n    runs-on: windows-2025\n'),
                ('duplicate runner key', WORKFLOW.replace('    runs-on: windows-2025',
                 '    runs-on: windows-2025\n    runs-on: windows-11-arm', 1)),
                ('anchor', 'jobs: &jobs\n  native:\n    runs-on: windows-2025\n'),
                ('merge', 'jobs:\n  native:\n    <<: *defaults\n'),
                ('no jobs', 'name: empty\n'),
                ('multiple documents', WORKFLOW + '\n---\nname: other\n')):
            with self.subTest(control=label):
                with self.assertRaises(proof.reader.Unreadable):
                    proof.parse(text)


class IndependentInstallerTests(unittest.TestCase):
    def test_both_previews_use_the_verified_seed_output(self):
        for name in ('windows', 'windows-arm'):
            with self.subTest(job=name):
                chain = proof.installation_chain(DOCUMENT, name)
                self.assertEqual(chain['seed']['id'], 'seed')
                self.assertEqual(chain['install']['env']['AXIOM_SEEDED_COMPILER'],
                                 '${{ steps.seed.outputs.compiler }}')
                self.assertTrue(proof.after_success(chain['install']['if'], 'seed',
                                                    {'seed': 'success'}))

    def test_console_failure_does_not_skip_install_or_make_job_successful(self):
        for name in ('windows', 'windows-arm'):
            for install_status in ('success', 'failure'):
                with self.subTest(job=name, installer=install_status):
                    chain = proof.installation_chain(DOCUMENT, name)
                    outcomes = {'seed': 'success', 'runtime': 'failure'}
                    self.assertTrue(proof.after_success(chain['install']['if'], 'seed', outcomes))
                    outcomes['install'] = install_status
                    self.assertEqual(proof.after_success(chain['upload']['if'], 'install', outcomes),
                                     install_status == 'success')
                    # A later independently guarded step cannot clear an
                    # earlier failing step without continue-on-error.
                    failed = any(outcome == 'failure' and not proof.reader.continues_on_error(
                        chain[step].get('continue-on-error')) for step, outcome in outcomes.items())
                    self.assertTrue(failed)

    def test_failed_skipped_cancelled_seed_never_installs(self):
        for name in ('windows', 'windows-arm'):
            chain = proof.installation_chain(DOCUMENT, name)
            for seed_status in ('failure', 'skipped', 'cancelled'):
                with self.subTest(job=name, seed=seed_status):
                    self.assertFalse(proof.after_success(chain['install']['if'], 'seed',
                                                         {'seed': seed_status}))
            self.assertFalse(proof.after_success(chain['install']['if'], 'seed',
                                                 {'seed': 'success'}, cancelled=True))

    def test_failed_skipped_cancelled_installer_never_uploads(self):
        for name in ('windows', 'windows-arm'):
            chain = proof.installation_chain(DOCUMENT, name)
            for status in ('failure', 'skipped', 'cancelled'):
                with self.subTest(job=name, installer=status):
                    self.assertFalse(proof.after_success(chain['upload']['if'], 'install',
                                                         {'install': status}))
            self.assertFalse(proof.after_success(chain['upload']['if'], 'install',
                                                 {'install': 'success'}, cancelled=True))

    def test_chain_refusals(self):
        controls = {
            'job advisory': lambda job, chain: job.update({'continue-on-error': 'true'}),
            'seed guarded': lambda job, chain: chain['seed'].update({'if': 'false'}),
            'seed advisory': lambda job, chain: chain['seed'].update({'continue-on-error': 'true'}),
            'duplicate step id': lambda job, chain: chain['runtime'].update({'id': 'seed'}),
            'runtime advisory': lambda job, chain: chain['runtime'].update({'continue-on-error': 'true'}),
            'runtime ignored': lambda job, chain: chain['runtime'].update({'run': chain['runtime']['run'] + ' || true'}),
            'runtime conditional': lambda job, chain: chain['runtime'].update({'if': '${{ always() }}'}),
            'installer advisory': lambda job, chain: chain['install'].update({'continue-on-error': 'true'}),
            'installer missing explicit condition': lambda job, chain: chain['install'].pop('if'),
            'installer ignores seed failure': lambda job, chain: chain['install'].update({'if': '${{ !cancelled() }}'}),
            'installer implicit success condition': lambda job, chain: chain['install'].update({'if': "steps.seed.outcome == 'success'"}),
            'installer always condition': lambda job, chain: chain['install'].update({'if': "${{ always() && steps.seed.outcome == 'success' }}"}),
            'installer wrong compiler': lambda job, chain: chain['install']['env'].update({'AXIOM_SEEDED_COMPILER': '${{ steps.other.outputs.compiler }}'}),
            'installer default compiler': lambda job, chain: chain['install'].update({'run': chain['install']['run'].replace('$env:AXIOM_SEEDED_COMPILER', '$PWD/.axiom-bin/axiom.exe')}),
            'installer ignores failure': lambda job, chain: chain['install'].update({'run': chain['install']['run'] + '; exit 0'}),
            'upload advisory': lambda job, chain: chain['upload'].update({'continue-on-error': 'true'}),
            'upload default condition': lambda job, chain: chain['upload'].pop('if'),
            'upload on failed installer': lambda job, chain: chain['upload'].update({'if': "${{ !cancelled() && steps.seed.outcome == 'success' }}"}),
            'upload ignores missing files': lambda job, chain: chain['upload']['with'].update({'if-no-files-found': 'ignore'}),
        }
        for name in ('windows', 'windows-arm'):
            for label, mutate in controls.items():
                with self.subTest(job=name, control=label):
                    document = copy.deepcopy(DOCUMENT)
                    chain = proof.installation_chain(document, name)
                    mutate(document['jobs'][name], chain)
                    with self.assertRaises(ValueError):
                        proof.installation_chain(document, name)

    def test_compiler_output_is_only_after_the_exact_seed_proof(self):
        def move_before_compare(run):
            lines = run.splitlines()
            output = next(line for line in lines if "printf 'compiler=" in line)
            lines.remove(output)
            compare = next(i for i, line in enumerate(lines) if line.startswith('cmp '))
            lines.insert(compare, output)
            return '\n'.join(lines)

        controls = {
            'missing output': lambda run: '\n'.join(line for line in run.splitlines()
                                                     if "printf 'compiler=" not in line),
            'output before comparison': move_before_compare,
            'wrong compiler output': lambda run: run.replace('cygpath -m "$seeded/axiom.exe"',
                                                             'cygpath -m "$PWD/.axiom-bin/axiom.exe"'),
            'wrong output name': lambda run: run.replace('compiler=%s', 'other=%s'),
            'environment instead of output': lambda run: run.replace('$GITHUB_OUTPUT', '$GITHUB_ENV'),
            'missing PE validation': lambda run: '\n'.join(line for line in run.splitlines()
                                                           if not line.startswith('llvm-readobj ')),
            'ignored equality failure': lambda run: run.replace(
                'cmp "$seeded/current.ll" "windows-hello/selfhost/axiom-$target.ll"',
                'cmp "$seeded/current.ll" "windows-hello/selfhost/axiom-$target.ll" || true'),
        }
        for name in ('windows', 'windows-arm'):
            for label, mutate in controls.items():
                with self.subTest(job=name, control=label):
                    document = copy.deepcopy(DOCUMENT)
                    step = bootstrap(document['jobs'][name])
                    step['run'] = mutate(step['run'])
                    with self.assertRaises(ValueError):
                        proof.installation_chain(document, name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
