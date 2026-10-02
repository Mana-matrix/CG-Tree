"""End-to-end tests against local bare repos served over file:// (no network)."""
import importlib, os, subprocess, sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEY = {'Authorization': 'Bearer test-key'}


def git(*a, cwd):
    subprocess.run(['git', *a], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@x'})


def write(base, files):
    for p, s in files.items():
        os.makedirs(os.path.dirname(os.path.join(base, p)) or base, exist_ok=True)
        open(os.path.join(base, p), 'w').write(s)


FILES = {
    'package.json': '{"name": "shop", "private": true}',
    'packages/db/package.json': '{"name": "@shop/db", "exports": {".": "./src/index.ts"}}',
    'packages/db/src/index.ts': "import { pgTable, text } from 'drizzle-orm/pg-core';\n"
                                "export const invoices = pgTable('invoices', { id: text('id').primaryKey(), total: text('total') });\n",
    'apps/web/tsconfig.json': '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}',
    'apps/web/src/billing.ts': "import { invoices } from '@shop/db';\n/** Invoice totals and tax. */\nexport function invoiceTotal(id: string) { return invoices; }\n",
    'apps/web/src/page.tsx': "import { invoiceTotal } from '@/billing';\nexport default function Page() { return invoiceTotal('1'); }\n",
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    remotes = tmp_path / 'remotes'
    for name, indexed in (('shop', True), ('plain', False)):
        src = tmp_path / 'src' / name
        src.mkdir(parents=True)
        git('init', '-q', '-b', 'main', cwd=src)
        write(src, FILES)
        if indexed:
            subprocess.run([sys.executable, os.path.join(ROOT, 'ctx.py'), '--repo', str(src), 'build', '-q'], check=True, capture_output=True)
        git('add', '-A', cwd=src)
        git('commit', '-qm', 'first', cwd=src)
        git('branch', 'dev', cwd=src)
        bare = remotes / 'acme' / f'{name}.git'
        git('clone', '-q', '--bare', str(src), str(bare), cwd=tmp_path)
        git('config', 'uploadpack.allowFilter', 'true', cwd=bare)
        git('config', 'uploadpack.allowAnySHA1InWant', 'true', cwd=bare)
    monkeypatch.setenv('CTX_API_KEY', 'test-key')
    monkeypatch.setenv('CTX_DATA', str(tmp_path / 'data'))
    monkeypatch.setenv('CTX_GIT_BASE', f'file://{remotes}')
    monkeypatch.setenv('CTX_FETCH_EVERY', '3600')
    import service.app as app_mod
    importlib.reload(app_mod)
    return TestClient(app_mod.app), tmp_path


def test_auth(env):
    c, _ = env
    assert c.get('/health').json()['ok']
    r = c.post('/repos/acme/shop/q', json={'text': 'invoice'})
    assert r.status_code == 401 and r.json()['code'] == 401
    assert c.post('/repos/acme/shop/q', json={'text': 'invoice'}, headers={'Authorization': 'Bearer nope'}).status_code == 401


def test_root_q_show_db(env):
    c, _ = env
    r = c.post('/repos/acme/shop/root', json={}, headers=KEY).json()
    assert r['ref'] == 'main' and r['indexed'] and not r['stale'] and r['text'].startswith('CTX1 repo=')
    r = c.post('/repos/acme/shop/q', json={'text': 'invoice total', 'budget': 1000}, headers=KEY).json()
    assert 'apps/web/src/billing.ts' in r['text']
    fid = next(l.split()[0][1:] for l in r['text'].splitlines() if l.startswith('@f') and 'billing.ts' in l)
    s = c.post('/repos/acme/shop/show', json={'ids': [fid]}, headers=KEY).json()['text']
    assert 'invoiceTotal' in s and '@shop/db' not in s  # the workspace package resolves to a file
    d = c.post('/repos/acme/shop/db', json={'tables': ['invoices']}, headers=KEY).json()['text']
    assert d.startswith('invoices(')


def test_branch_refresh_and_stale(env):
    c, tmp = env
    assert c.post('/repos/acme/shop/root', json={'ref': 'dev'}, headers=KEY).json()['ref'] == 'dev'
    assert c.post('/repos/acme/shop/q', json={'ref': 'nope', 'text': 'x'}, headers=KEY).status_code == 404
    src = tmp / 'src' / 'shop'
    write(src, {'apps/web/src/refund.ts': 'export function refundInvoice() { return 1; }\n'})
    git('add', '-A', cwd=src)
    git('commit', '-qm', 'refunds', cwd=src)
    git('push', '-q', str(tmp / 'remotes' / 'acme' / 'shop.git'), 'main', cwd=src)
    # throttled: still the old head until refresh
    assert 'refund' not in c.post('/repos/acme/shop/q', json={'text': 'refund invoice'}, headers=KEY).json()['text']
    r = c.post('/repos/acme/shop/refresh', json={}, headers=KEY).json()
    assert 'text' not in r and r['stale']  # index committed before refund.ts
    assert 'refund.ts' in c.post('/repos/acme/shop/q', json={'text': 'refund invoice'}, headers=KEY).json()['text']


def test_unindexed_repo(env):
    c, _ = env
    r = c.post('/repos/acme/plain/q', json={'text': 'invoice'}, headers=KEY).json()
    assert not r['indexed'] and not r['stale'] and 'billing.ts' in r['text']


def test_bad_input_and_errors(env):
    c, _ = env
    assert c.post('/repos/acme/..%2Fx/q', json={'text': 'x'}, headers=KEY).status_code in (404, 422)
    assert c.post('/repos/acme/shop/q', json={'text': 'x', 'ref': '../main'}, headers=KEY).status_code == 422
    assert c.post('/repos/acme/shop/show', json={'ids': ['nope']}, headers=KEY).status_code == 422
    r = c.post('/repos/acme/missing/q', json={'text': 'x'}, headers={**KEY, 'X-Git-Token': 'sekrit123'})
    assert r.status_code in (403, 502) and 'sekrit123' not in r.text


def test_drop(env):
    c, _ = env
    c.post('/repos/acme/shop/root', json={}, headers=KEY)
    assert c.delete('/repos/acme/shop', headers=KEY).json() == {'dropped': True}
    assert c.post('/repos/acme/shop/root', json={}, headers=KEY).status_code == 200


def test_evicts_old_worktrees(env, monkeypatch):
    c, tmp = env
    import service.app as app_mod
    monkeypatch.setattr(app_mod.repos, 'max_wt', 1)
    c.post('/repos/acme/shop/root', json={'ref': 'main'}, headers=KEY)
    c.post('/repos/acme/shop/root', json={'ref': 'dev'}, headers=KEY)
    assert sorted(os.listdir(tmp / 'data' / 'wt')) == ['acme__shop@dev']
