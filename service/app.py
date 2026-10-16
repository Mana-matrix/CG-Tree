"""ctx service: the CTX1 index of GitHub repos over HTTP.

Holds blobless clones and one worktree per branch, keeps them fetched and the index built, and
answers the read commands of ctx.py (root, q, show, db). It never calls an LLM: the caller does,
and uses these routes as the model's tools.

Env: CTX_API_KEY (required), CTX_DATA (/data), CTX_GIT_BASE (https://github.com),
     CTX_MAX_WORKTREES (20), CTX_FETCH_EVERY (30 s), CTX_PY (ctx.py next to the service)
"""
import hmac, os, subprocess, sys, time
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .repos import GitError, Repos

VERSION = '1'
HERE = os.path.dirname(os.path.abspath(__file__))
CTX_PY = os.environ.get('CTX_PY') or os.path.join(os.path.dirname(HERE), 'ctx.py')
API_KEY = os.environ.get('CTX_API_KEY', '')
LOCK_WAIT = 120

repos = Repos(
    os.environ.get('CTX_DATA', '/data'),
    os.environ.get('CTX_GIT_BASE', 'https://github.com'),
    int(os.environ.get('CTX_MAX_WORKTREES', '20')),
    int(os.environ.get('CTX_FETCH_EVERY', '30')),
)
app = FastAPI(title='ctx service', version=VERSION, docs_url=None, redoc_url=None)


@app.exception_handler(HTTPException)
async def _http_error(_: Request, e: HTTPException):
    return JSONResponse({'error': e.detail, 'code': e.status_code}, status_code=e.status_code)


@app.exception_handler(GitError)
async def _git_error(_: Request, e: GitError):
    return JSONResponse({'error': e.msg, 'code': e.code}, status_code=e.code)


def auth(authorization: Annotated[str | None, Header()] = None):
    if not API_KEY: raise HTTPException(503, 'CTX_API_KEY is not set')
    got = (authorization or '').removeprefix('Bearer ').strip()
    if not hmac.compare_digest(got.encode(), API_KEY.encode()): raise HTTPException(401, 'bad service key')


GitToken = Annotated[str | None, Header(alias='X-Git-Token')]


class Ref(BaseModel):
    ref: str | None = Field(None, max_length=200, description='branch; default = the repo default branch')
    refresh: bool = Field(False, description='fetch now instead of at most every CTX_FETCH_EVERY seconds')


class Query(Ref):
    text: str = Field(min_length=1, max_length=4000)
    budget: int = Field(3000, ge=300, le=20000)


class Show(Ref):
    ids: list[Annotated[str, Field(pattern=r'^[fm][0-9a-f]{5,6}$')]] = Field(min_length=1, max_length=30)


class Db(Ref):
    tables: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=30)


def _ctx(path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_CONFIG_', 'CTX_API'))}
    # no rename detection: in a blobless clone it would fetch blobs for the whole history
    env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='diff.renames', GIT_CONFIG_VALUE_0='false', GIT_TERMINAL_PROMPT='0')
    r = subprocess.run([sys.executable, CTX_PY, '--repo', path, *args], cwd=path, env=env, capture_output=True, text=True, timeout=600)
    if r.returncode: raise HTTPException(500, f'ctx failed: {(r.stderr or r.stdout).strip()[-300:]}')
    return r.stdout


def _run(owner, name, body: Ref, token, *args, root=False):
    t0 = time.time()
    key = repos.key(owner, name)
    lk = repos.lock(key)
    if not lk.acquire(timeout=LOCK_WAIT): raise HTTPException(409, 'repo is busy, try again')
    try:
        path, ref, head = repos.sync(owner, name, body.ref, token, force=body.refresh)
        if root:
            _ctx(path, 'build', '-q')
            text = open(os.path.join(path, '.ctx', 'root.ct'), encoding='utf8').read()
        else:
            text = _ctx(path, *args)
        indexed = bool(subprocess.run(['git', 'ls-files', '.ctx/root.ct'], cwd=path, capture_output=True, text=True).stdout.strip())
        changed = subprocess.run(['git', 'status', '--porcelain', '--', '.ctx'], cwd=path, capture_output=True, text=True).stdout
        # stale: the committed index is behind the branch (rebuilt here; changed files lack their purposes)
        stale = indexed and any(l[3:].startswith(('.ctx/n/', '.ctx/root.ct', '.ctx/db.ct')) for l in changed.splitlines())
    finally:
        lk.release()
    return {'text': text, 'ref': ref, 'head': head, 'indexed': indexed, 'stale': stale,
            'tokens': len(text) // 4, 'ms': int((time.time() - t0) * 1000)}


@app.get('/health')
def health():
    return {'ok': True, 'version': VERSION}


@app.post('/repos/{owner}/{name}/root', dependencies=[Depends(auth)])
def root(owner: str, name: str, body: Ref, token: GitToken = None):
    return _run(owner, name, body, token, root=True)


@app.post('/repos/{owner}/{name}/q', dependencies=[Depends(auth)])
def q(owner: str, name: str, body: Query, token: GitToken = None):
    return _run(owner, name, body, token, 'q', body.text, '-b', str(body.budget))


@app.post('/repos/{owner}/{name}/show', dependencies=[Depends(auth)])
def show(owner: str, name: str, body: Show, token: GitToken = None):
    return _run(owner, name, body, token, 'show', *body.ids)


@app.post('/repos/{owner}/{name}/db', dependencies=[Depends(auth)])
def db(owner: str, name: str, body: Db, token: GitToken = None):
    return _run(owner, name, body, token, 'db', *body.tables)


@app.post('/repos/{owner}/{name}/refresh', dependencies=[Depends(auth)])
def refresh(owner: str, name: str, body: Ref, token: GitToken = None):
    r = _run(owner, name, Ref(ref=body.ref, refresh=True), token, root=True)
    r.pop('text')
    return r


@app.delete('/repos/{owner}/{name}', dependencies=[Depends(auth)])
def drop(owner: str, name: str):
    key = repos.key(owner, name)
    lk = repos.lock(key)
    if not lk.acquire(timeout=LOCK_WAIT): raise HTTPException(409, 'repo is busy, try again')
    try:
        return {'dropped': repos.drop(owner, name)}
    finally:
        lk.release()
