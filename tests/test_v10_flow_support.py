"""Shared fixtures for the als_flow tests: a fake `codex` and a temp project.

No tests live here. The fake codex never calls a model; it records argv, stdin,
cwd and timestamps to $FAKE_CODEX_LOG (JSONL) and is steered by env:
FAKE_CODEX_FAIL=1, FAKE_CODEX_SLEEP=SECONDS, FAKE_CODEX_INVALID=1 (non-JSON
reply with --output-schema), FAKE_CODEX_JSON=TEXT (exact reply),
FAKE_CODEX_REPLY=TEXT, FAKE_CODEX_NO_THREAD=1, FAKE_LOGIN_STATUS=TEXT,
FAKE_CODEX_USAGE=in,cached,out.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / 'bin'

FAKE_CODEX = r'''
import json, os, sys, time
argv = sys.argv[1:]
start = time.time()
if argv[:2] == ['login', 'status']:
    print(os.environ.get('FAKE_LOGIN_STATUS', 'Logged in using ChatGPT'))
    sys.exit(0)
stdin = sys.stdin.read()
VALUED = {'-c', '--config', '-m', '--model', '-o', '--output-last-message', '--output-schema', '-s',
          '--sandbox', '--cd', '-C', '-i', '--image', '-p', '--profile'}
opts, positional, i = {}, [], 0
rest = argv[1:] if argv[:1] == ['exec'] else argv
resume = rest[:1] == ['resume']
if resume:
    rest = rest[1:]
while i < len(rest):
    arg = rest[i]
    if arg in VALUED and i + 1 < len(rest):
        opts.setdefault(arg, []).append(rest[i + 1]); i += 2; continue
    if arg.startswith('-') and arg != '-':
        opts.setdefault(arg, []).append(True); i += 1; continue
    positional.append(arg); i += 1
sleep = float(os.environ.get('FAKE_CODEX_SLEEP', '0') or 0)
if sleep:
    time.sleep(sleep)
counter_file = os.environ.get('FAKE_CODEX_LOG', '') + '.n'
n = 0
if os.environ.get('FAKE_CODEX_LOG'):
    try:
        n = int(open(counter_file).read() or 0)
    except OSError:
        pass
    open(counter_file, 'w').write(str(n + 1))
session = positional[0] if resume and positional and positional[0] != '-' else 'thread-%d-%d' % (os.getpid(), n)
record = {'argv': argv, 'stdin': stdin, 'cwd': os.getcwd(), 'start': start, 'resume': resume,
          'session': session}
fail = os.environ.get('FAKE_CODEX_FAIL') == '1'
if not os.environ.get('FAKE_CODEX_NO_THREAD'):
    print(json.dumps({'type': 'thread.started', 'thread_id': session}), flush=True)
if fail:
    print(json.dumps({'type': 'error', 'message': 'fake failure'}), flush=True)
    sys.stderr.write('fake codex failed\n')
else:
    if '--output-schema' in opts:
        schema = json.load(open(opts['--output-schema'][0]))
        if os.environ.get('FAKE_CODEX_INVALID') == '1':
            reply = 'this is not json'
        elif 'FAKE_CODEX_JSON' in os.environ:
            reply = os.environ['FAKE_CODEX_JSON']
        else:
            reply = json.dumps({k: 'v-' + k for k in schema.get('required', [])})
    else:
        last = [l for l in stdin.strip().splitlines() if l.strip()]
        reply = os.environ.get('FAKE_CODEX_REPLY') or 'reply: ' + (last[-1] if last else '')
    if '-o' in opts:
        open(opts['-o'][0], 'w').write(reply)
    u = [int(x) for x in os.environ.get('FAKE_CODEX_USAGE', '100,40,10').split(',')]
    print(json.dumps({'type': 'turn.completed',
                      'usage': {'input_tokens': u[0], 'cached_input_tokens': u[1], 'output_tokens': u[2]}}))
record['end'] = time.time()
if os.environ.get('FAKE_CODEX_LOG'):
    with open(os.environ['FAKE_CODEX_LOG'], 'a') as h:
        h.write(json.dumps(record) + '\n')
sys.exit(1 if fail else 0)
'''

ROLE_READER = '''name = "reader"
description = "read-only tester role"
model = "gpt-test-luna"
model_reasoning_effort = "max"
sandbox_mode = "read-only"
developer_instructions = """
You are the READER role.
Line two of the instructions.
"""
'''

ROLE_WRITER = '''name = "writer"
description = "writer role"
model = "gpt-test-sol"
model_reasoning_effort = "high"
sandbox_mode = "workspace-write"
developer_instructions = "You are the WRITER role."
'''


class FakeCodexMixin:
    """setUp builds: a git project with roles, a fake codex on PATH, env isolation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.project = self.tmp / 'project'
        (self.project / '.codex' / 'agents').mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        (self.project / '.codex' / 'agents' / 'reader.toml').write_text(ROLE_READER)
        (self.project / '.codex' / 'agents' / 'als_writer.toml').write_text(ROLE_WRITER)
        self.codex_home = self.tmp / 'codex-home'
        (self.codex_home / 'agents').mkdir(parents=True)
        fake_bin = self.tmp / 'fakebin'
        fake_bin.mkdir()
        self.fake = fake_bin / 'codex'
        self.fake.write_text('#!%s\n%s' % (sys.executable, FAKE_CODEX))
        self.fake.chmod(0o755)
        self.log_file = self.tmp / 'codex-calls.jsonl'
        self._saved_env = dict(os.environ)
        for key in list(os.environ):
            if key.startswith(('FAKE_', 'ALS_')):
                del os.environ[key]
        os.environ.update({
            'PATH': str(fake_bin) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
            'CODEX_HOME': str(self.codex_home),
            'FAKE_CODEX_LOG': str(self.log_file),
        })
        self._saved_cwd = os.getcwd()
        os.chdir(str(self.project))

    def tearDown(self):
        os.chdir(self._saved_cwd)
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._tmp.cleanup()

    def calls(self):
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text().splitlines() if line.strip()]

    def cli(self, tool, *args, stdin=None, env=None, cwd=None):
        full_env = dict(os.environ, ALS_PYTHON=sys.executable)
        full_env.update(env or {})
        return subprocess.run([str(BIN / tool)] + list(args), input=stdin, capture_output=True, text=True,
                              env=full_env, cwd=str(cwd or self.project), timeout=120)
