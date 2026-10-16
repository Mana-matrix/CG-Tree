"""Git side of the service: one blobless bare clone per repo, one worktree per ref, per-repo locks.

The git token comes with each request and only lives in the environment of the git commands run
for it (GIT_CONFIG_* -> http.extraHeader), never on disk, in git config or on a command line.
"""
import base64, os, re, shutil, subprocess, threading, time

NAME = re.compile(r'^[A-Za-z0-9_.-]{1,100}$')
REF = re.compile(r'^(?!.*\.\.)(?!/)(?!.*//)(?!.*@\{)[A-Za-z0-9._/-]{1,200}(?<![/.])$')


class GitError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def _env(token):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_CONFIG_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='true', GCM_INTERACTIVE='never', LC_ALL='C')
    if token:
        basic = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.extraHeader', GIT_CONFIG_VALUE_0=f'Authorization: Basic {basic}')
    return env


def _classify(err):
    e = err.lower()
    if any(s in e for s in ('authentication failed', 'could not read username', 'terminal prompts disabled',
                             'repository not found', 'not found', '403', '401', 'access denied')):
        return 403
    if 'couldn\'t find remote ref' in e or 'invalid reference' in e or 'unknown revision' in e:
        return 404
    return 502


class Repos:
    def __init__(self, data, base_url='https://github.com', max_worktrees=20, fetch_every=30, timeout=600):
        self.data, self.base = data, base_url.rstrip('/')
        self.max_wt, self.fetch_every, self.timeout = max_worktrees, fetch_every, timeout
        self._locks, self._guard, self._fetched = {}, threading.Lock(), {}
        os.makedirs(os.path.join(data, 'repos'), exist_ok=True)
        os.makedirs(os.path.join(data, 'wt'), exist_ok=True)

    # ------------------------------------------------------------ helpers
    def key(self, owner, name):
        if not NAME.match(owner) or not NAME.match(name) or owner in ('.', '..') or name in ('.', '..'):
            raise GitError(422, 'bad repo name')
        return f'{owner}__{name}'.lower()

    def lock(self, key):
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _git(self, args, cwd=None, token=None, check=True):
        try:
            r = subprocess.run(['git', *args], cwd=cwd, env=_env(token), capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise GitError(504, 'git timed out')
        if check and r.returncode:
            err = (r.stderr or r.stdout).strip()
            if token: err = err.replace(token, '***')
            raise GitError(_classify(err), err.splitlines()[-1][:300] if err else 'git failed')
        return r.stdout

    def bare(self, key): return os.path.join(self.data, 'repos', key + '.git')

    def wt(self, key, ref, name):
        # the checkout's directory is named like the repo: ctx writes it into the index header (repo=<name>)
        return os.path.join(self.data, 'wt', key + '@' + ref.replace('/', '%'), name)

    # ------------------------------------------------------------ sync
    def sync(self, owner, name, ref, token, force=False):
        """Make sure <owner>/<name>@<ref> is cloned, fetched (throttled) and checked out.
        Call with the repo lock held. Returns (worktree path, ref, head sha)."""
        key = self.key(owner, name)
        if ref and not REF.match(ref): raise GitError(422, 'bad ref')
        bare = self.bare(key)
        url = f'{self.base}/{owner}/{name}.git'
        if not os.path.isdir(bare):
            tmp = bare + '.tmp'
            shutil.rmtree(tmp, ignore_errors=True)
            self._git(['clone', '--bare', '--filter=blob:none', '--quiet', url, tmp], token=token)
            os.rename(tmp, bare)
            self._fetched[key] = time.time()
        elif force or time.time() - self._fetched.get(key, 0) > self.fetch_every:
            self._git(['fetch', '--prune', '--quiet', 'origin', '+refs/heads/*:refs/heads/*'], cwd=bare, token=token)
            self._git(['remote', 'set-head', 'origin', '--auto'], cwd=bare, token=token, check=False)
            self._fetched[key] = time.time()
        if not ref:
            ref = self._git(['symbolic-ref', '--short', 'HEAD'], cwd=bare).strip()
            remote_head = self._git(['symbolic-ref', '--short', 'refs/remotes/origin/HEAD'], cwd=bare, check=False).strip()
            if remote_head.startswith('origin/'): ref = remote_head[len('origin/'):]
        try:
            head = self._git(['rev-parse', '--verify', '--quiet', f'refs/heads/{ref}^{{commit}}'], cwd=bare).strip()
        except GitError:
            raise GitError(404, f'branch {ref} not found')
        path = self.wt(key, ref, name)
        if not os.path.isdir(path):
            self._evict(key)
            self._git(['worktree', 'prune'], cwd=bare)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._git(['worktree', 'add', '--detach', '--force', path, head], cwd=bare, token=token)
        elif self._git(['rev-parse', 'HEAD'], cwd=path).strip() != head:
            # build output in .ctx is thrown away with the checkout; the ignored caches stay
            self._git(['checkout', '--force', '--detach', '--quiet', head], cwd=path, token=token)
            self._git(['clean', '-fdq', '-e', '.ctx'], cwd=path)
        os.utime(os.path.dirname(path))
        return path, ref, head

    def _evict(self, held):
        """Remove the least recently used worktrees down to max_wt - 1. `held`: the repo whose lock we hold."""
        root = os.path.join(self.data, 'wt')
        wts = sorted((os.path.getmtime(os.path.join(root, d)), d) for d in os.listdir(root))
        for _, d in wts[:max(0, len(wts) - self.max_wt + 1)]:
            key = d.split('@', 1)[0]
            own = key == held
            if not own and not self.lock(key).acquire(blocking=False): continue  # in use: leave it
            try:
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                self._git(['worktree', 'prune'], cwd=self.bare(key), check=False)
            finally:
                if not own: self.lock(key).release()

    def drop(self, owner, name):
        key = self.key(owner, name)
        root = os.path.join(self.data, 'wt')
        for d in os.listdir(root):
            if d.split('@', 1)[0] == key: shutil.rmtree(os.path.join(root, d), ignore_errors=True)
        existed = os.path.isdir(self.bare(key))
        shutil.rmtree(self.bare(key), ignore_errors=True)
        self._fetched.pop(key, None)
        return existed
