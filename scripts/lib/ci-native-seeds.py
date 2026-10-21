#!/usr/bin/env python3
"""Prove the native Windows seed recipe in a structurally read workflow.

The accepted recipe is straight-line Bash: install the committed seed,
verify its PE machine, execute that same compiler, and compare its current
source emission with the matching cross-built IR. Unknown shell statements
or workflow shapes do not count as evidence. Job/step display names do not
participate. Conditional ancestors are reported, as in ci-steps.py; the
native job and its proof step must themselves be unconditional.
"""

import importlib.util
from pathlib import Path
import re
import shlex
import sys

spec = importlib.util.spec_from_file_location('ci_steps', Path(__file__).with_name('ci-steps.py'))
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)

RUNNERS = {
    'windows-2025': ('windows-x86_64', 'AMD64'),
    'windows-11-arm': ('windows-aarch64', 'ARM64'),
}


class StrictReader(reader.Reader):
    """Keep the shared YAML subset, rejecting duplicate keys and aliases."""

    def mapping(self, ind):
        result = {}
        while not self.eof():
            line = self.lines[self.i]
            if reader.indent_of(line) != ind:
                if reader.indent_of(line) > ind:
                    raise reader.Unreadable(f'line {self.i + 1}: unexpected indentation')
                break
            text = line[ind:]
            if text == '-' or text.startswith('- '):
                break
            match = re.match(r'''^("[^"]*"|'[^']*'|[^\s#'\"][^:]*?):(?:\s+(.*))?$''', text)
            if not match:
                raise reader.Unreadable(f'line {self.i + 1}: not a key/value mapping')
            key = reader.unquote(match.group(1))
            if key in result or key == '<<':
                raise reader.Unreadable(f'line {self.i + 1}: duplicate or merged key {key!r}')
            raw = reader.strip_comment(match.group(2) or '')
            self.i += 1
            if re.fullmatch(r'[|>][+-]?[0-9]?', raw):
                result[key] = self.block_scalar(ind, raw[0] == '>')
            elif not raw:
                if self.eof():
                    result[key] = None
                    continue
                following = self.lines[self.i]
                rest = following[reader.indent_of(following):]
                if reader.indent_of(following) > ind or (
                        reader.indent_of(following) == ind and (rest == '-' or rest.startswith('- '))):
                    result[key] = self.node(reader.indent_of(following))
                else:
                    result[key] = None
            else:
                result[key] = self.flow(raw, ind)
        return result

    def flow(self, raw, ind):
        if raw.startswith(('&', '*')):
            raise reader.Unreadable(f'line {self.i}: YAML anchors and aliases are not supported')
        return super().flow(raw, ind)


def parse(text):
    parser = StrictReader(text)
    document = parser.node(0)
    if not parser.eof() or not isinstance(document, dict) or not isinstance(document.get('jobs'), dict):
        raise reader.Unreadable('expected one workflow with a jobs mapping')
    return document


def unconditional(item):
    condition = item.get('if')
    return condition is None or str(condition).strip() in ('true', '${{ true }}')


def dependencies(jobs, name, active=()):
    """Reject unreachable needs; retain inherited conditional applicability."""
    if name in active or name not in jobs or not isinstance(jobs[name], dict):
        raise ValueError('missing or cyclic job dependency: ' + name)
    job = jobs[name]
    if reader.literally_false(job.get('if')) or reader.continues_on_error(job.get('continue-on-error')):
        raise ValueError('disabled or advisory dependency: ' + name)
    conditions = []
    if not unconditional(job):
        conditions.append(name + ': ' + str(job['if']))
    needs = job.get('needs', [])
    if isinstance(needs, str):
        needs = [needs]
    if not isinstance(needs, list) or any(not isinstance(need, str) for need in needs):
        raise ValueError('nonliteral job dependencies: ' + name)
    for need in needs:
        conditions.extend(dependencies(jobs, need, active + (name,)))
    return sorted(set(conditions))


def shell_for(document, job, step):
    shell = None
    for item in (document, job):
        if 'env' in item:
            raise ValueError('bootstrap environment changed')
        defaults = item.get('defaults', {})
        if not isinstance(defaults, dict) or not isinstance(defaults.get('run', {}), dict):
            raise ValueError('unreadable run defaults')
        if 'working-directory' in defaults.get('run', {}):
            raise ValueError('bootstrap working directory changed')
        shell = defaults.get('run', {}).get('shell', shell)
    return step.get('shell', shell)


def commands(run):
    if not isinstance(run, str):
        raise ValueError('run must be a literal script')
    result, pending = [], ''
    for line in run.splitlines():
        if line.rstrip().endswith('\\'):
            pending += line.rstrip()[:-1] + ' '
            continue
        line = pending + line
        pending = ''
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError as error:
            raise ValueError('unreadable shell quoting') from error
        if tokens:
            result.append(tokens)
    if pending:
        raise ValueError('unfinished shell continuation')
    return result


def prove_recipe(run, target, machine):
    actual = commands(run)
    diagnostic = ['printf',
                  r'Shell machine: %s; PROCESSOR_ARCHITECTURE=%s; PROCESSOR_ARCHITEW6432=%s\n',
                  '$(uname -m)', '${PROCESSOR_ARCHITECTURE:-unset}', '${PROCESSOR_ARCHITEW6432:-unset}']
    # This optional observation cannot replace any link in the proof chain.
    if len(actual) > 2 and actual[2] == diagnostic:
        del actual[2]
    expected = [
        ['set', '-euo', 'pipefail'],
        ['target=' + target],
        ['seeded=$(cygpath -u $RUNNER_TEMP)/axiom-seeded-$target'],
        ['./scripts/bootstrap-from-seed.sh', '--install', '$seeded'],
        ['llvm-readobj', '--file-headers', '$seeded/axiom.exe', '|', 'grep', '-F',
         'Machine: IMAGE_FILE_MACHINE_' + machine + ' ('],
        ['export', 'AXIOM_STDLIB=$(cygpath -m $PWD/stdlib)'],
        ['unset', 'AXIOM_PATH'],
        ['$seeded/axiom.exe', '--diagnostic-format=ai', '--target=$target', 'emit-llvm',
         'self_host/main.ax', '-o', '$seeded/current.ll'],
        ['cmp', '$seeded/current.ll', 'windows-hello/selfhost/axiom-$target.ll'],
    ]
    if actual != expected:
        raise ValueError('not the ordered, failure-preserving native seed/PE/emission/comparison recipe')


def native_seeds(document):
    jobs, targets, notes = document['jobs'], set(), []
    for name, job in jobs.items():
        if not isinstance(job, dict) or not isinstance(job.get('runs-on'), str):
            continue
        if job['runs-on'] not in RUNNERS:
            continue
        target, machine = RUNNERS[job['runs-on']]
        try:
            if not unconditional(job) or reader.continues_on_error(job.get('continue-on-error')):
                raise ValueError('native job is guarded or advisory')
            inherited = dependencies(jobs, name)
            steps = job.get('steps')
            if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
                raise ValueError('steps must be a literal sequence of mappings')
            candidates = [step for step in steps
                          if 'bootstrap-from-seed.sh' in str(step.get('run', ''))]
            if not candidates:
                raise ValueError('no native seed recipe')
            for step in candidates:
                try:
                    if not unconditional(step) or reader.continues_on_error(step.get('continue-on-error')):
                        raise ValueError('bootstrap step is guarded or advisory')
                    if shell_for(document, job, step) != 'bash' or 'uses' in step:
                        raise ValueError('bootstrap must run directly in Bash')
                    if any(key in job or key in step for key in ('env', 'working-directory', 'container')):
                        raise ValueError('bootstrap execution context changed')
                    prove_recipe(step.get('run'), target, machine)
                except ValueError as error:
                    notes.append(f'{name}: refused {target}: {error}')
                    continue
                targets.add(target)
                for condition in inherited:
                    notes.append(f'{name}: inherited applicability: {condition}')
        except ValueError as error:
            notes.append(f'{name}: refused {target}: {error}')
    return targets, sorted(set(notes))


def main():
    try:
        document = parse(Path(sys.argv[1]).read_text())
        targets, notes = native_seeds(document)
    except (OSError, IndexError, ValueError, reader.Unreadable) as error:
        print('native seed proof: ' + str(error), file=sys.stderr)
        return 2
    for note in notes:
        print('native seed proof: ' + note, file=sys.stderr)
    for target in sorted(targets):
        print(target)
    return 0


if __name__ == '__main__':
    sys.exit(main())
