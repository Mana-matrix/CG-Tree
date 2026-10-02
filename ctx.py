#!/usr/bin/env python3
"""ctx — build and query a CTX1 repository context index (see SPEC.md).

  ctx.py init [--strict] [--sync]  install into repo: .ctx/, Claude Code hooks, /ticket + /ctx-sync skills,
                                 CLAUDE.md block, pre-commit. --strict: search gate. --sync: also run the LLM layer
  ctx.py build [-q] [--tracked]  build/refresh .ctx/ (incremental)
  ctx.py check                   exit 1 if committed .ctx/ is stale (CI)
  ctx.py session                 refresh + print root for Claude Code SessionStart hook
  ctx.py gate                    PreToolUse hook (init --strict): nudge to `q` before the first grep/glob
  ctx.py q "<request>" [-b N]    emit a minimal context slice for a request (N = token budget)
  ctx.py show <id> [<id>...]     print c-tree + g-tree blocks for ids
  ctx.py db [table...]           schema: columns (* pk, → fk), defining file, writer file ids
  ctx.py todo [-n N]             list files lacking a purpose line
  ctx.py setp <path> "<text>"    write a purpose for a file (human-written, wins over generated)
  ctx.py sync [status|plan|apply|run]  LLM layer: file purposes, module purposes + history digests, OVERVIEW.md

Requires: git, pip install tree-sitter-language-pack
"""
import argparse, collections, hashlib, json, math, os, re, shutil, subprocess, sys, tempfile

VERSION = "CTX1"
PARSE_REV = hashlib.sha1(open(__file__, "rb").read()).hexdigest()[:8]  # any tool change invalidates the parse cache
CODE_EXT = {'.ts': 'typescript', '.tsx': 'tsx', '.js': 'javascript', '.jsx': 'javascript',
            '.mjs': 'javascript', '.cjs': 'javascript', '.mts': 'typescript', '.cts': 'typescript',
            '.py': 'python'}
SKIP_RE = re.compile(
    r'(^|/)(\.claude|node_modules|dist|build|out|\.next|\.turbo|coverage|vendor|__pycache__|\.ctx|\.git)/'
    r'|(^|/)(pnpm-lock\.yaml|package-lock\.json|yarn\.lock|bun\.lockb|poetry\.lock|uv\.lock|LICENSE[^/]*)$'
    r'|\.(png|jpe?g|gif|ico|svg|webp|avif|woff2?|ttf|otf|eot|pdf|zip|gz|mp4|mp3|wav|map)$'
    r'|\.min\.(js|css)$|(^|/)migrations/meta/')
MAX_BYTES = 400_000
GITIGNORE = '# derived locally: history is rebuilt from .git, caches are per-machine\ng/\n.cache.json\n.git.cache\n.sess/\n.sync/\n'
MIN_MOD_FILES = 4
TARGET_MODS = 40  # root budget: module min size scales so root stays ~1-2k tokens
UB_CAP = 8

# ---------------------------------------------------------------- utils

def sh(args, cwd, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, errors='replace')
    if check and r.returncode != 0:
        raise SystemExit(f"error: {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout

def sha(s, n): return hashlib.sha1(s.encode()).hexdigest()[:n]

def blob_hash(b): return hashlib.sha1(b'blob %d\0' % len(b) + b).hexdigest()

def squash(s, lim=110):
    s = re.sub(r'\s+', ' ', s).strip()
    return s if len(s) <= lim else s[:lim - 1] + '…'

def stem(w): return w[:-1] if len(w) > 4 and w.endswith('s') and not w.endswith('ss') else w

def terms(s):
    out = set()
    for w in re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+', s):
        w = w.lower()
        if len(w) >= 2: out.add(stem(w))
    return out

def load_jsonish(path):
    """tsconfig-style JSON: comments + trailing commas."""
    try: s = open(path, encoding='utf8').read()
    except OSError: return {}
    out, i, n, q = [], 0, len(s), None
    while i < n:
        c = s[i]
        if q:
            out.append(c)
            if c == '\\' and i + 1 < n: out.append(s[i + 1]); i += 1
            elif c == q: q = None
        elif c == '"': q = c; out.append(c)
        elif s.startswith('//', i): i = s.find('\n', i); i = n if i < 0 else i; continue
        elif s.startswith('/*', i): i = s.find('*/', i + 2); i = n if i < 0 else i + 2; continue
        else: out.append(c)
        i += 1
    try: return json.loads(re.sub(r',(\s*[}\]])', r'\1', ''.join(out)))
    except ValueError: return {}

# ---------------------------------------------------------------- parsing

_parsers = {}
def parser(lang):
    if lang not in _parsers:
        from tree_sitter_language_pack import get_parser
        _parsers[lang] = get_parser(lang)
    return _parsers[lang]

def T(n): return n.text.decode('utf8', 'replace') if n is not None else ''
def unq(s): return s.strip().strip('\'"`')
def field(n, f): return n.child_by_field_name(f) if n is not None else None

JUNK_DOC = re.compile(r'copyright|license|eslint|prettier|@ts-|use (client|server)|biome|istanbul|^\s*$', re.I)

def doc_from_comment(text):
    text = re.sub(r'^/\*\*?|\*/$', '', text.strip())
    lines = [re.sub(r'^\s*(\*|//|#)\s?', '', l).strip() for l in text.splitlines()]
    text = ' '.join(l for l in lines if l and not l.startswith('@'))
    if not text or JUNK_DOC.search(text): return ''
    return squash(re.split(r'(?<=[.!?])\s', text)[0], 100)

FN_TYPES = ('function_declaration', 'generator_function_declaration', 'function_expression',
            'function', 'arrow_function', 'generator_function')
CLASS_TYPES = ('class_declaration', 'abstract_class_declaration', 'class')

def ts_fn_sig(name, fn):
    ps = field(fn, 'parameters')
    if ps is None:
        p1 = field(fn, 'parameter'); ps_s = f"({T(p1)})"
    else: ps_s = T(ps)
    return squash(f"{name}{ps_s}{T(field(fn, 'return_type'))}")

def ts_class_sig(name, d):
    ms, body = [], field(d, 'body')
    for m in (body.named_children if body is not None else []):
        if m.type in ('method_definition', 'abstract_method_signature', 'public_field_definition'):
            mn = T(field(m, 'name'))
            acc = [T(c) for c in m.children if c.type == 'accessibility_modifier']
            if mn and not mn.startswith('#') and not (acc and acc[0] in ('private', 'protected')):
                ms.append(mn)
    return squash(f"class {name}{{{','.join(ms)}}}", 160)

def ts_decl(d):
    ty, name = d.type, T(field(d, 'name'))
    if ty in FN_TYPES: return [ts_fn_sig(name, d)]
    if ty in CLASS_TYPES: return [ts_class_sig(name, d)]
    if ty in ('lexical_declaration', 'variable_declaration'):
        out = []
        for v in d.named_children:
            if v.type != 'variable_declarator': continue
            vn, val = T(field(v, 'name')), field(v, 'value')
            while val is not None and val.type in ('as_expression', 'satisfies_expression', 'parenthesized_expression'):
                val = val.named_children[0] if val.named_children else None
            if val is None: out.append(vn)
            elif val.type in FN_TYPES: out.append(ts_fn_sig(vn, val))
            elif val.type == 'call_expression':
                fn = T(field(val, 'function'))
                fn = fn if len(fn) <= 40 else fn.split('(')[0][:40]
                out.append(squash(f"{vn}={fn}()", 90))
            else: out.append(vn)
        return out
    if ty == 'interface_declaration': return ['iface ' + name]
    if ty == 'type_alias_declaration': return ['type ' + name]
    if ty == 'enum_declaration': return ['enum ' + name]
    return []

def ts_parse(b, lang):
    root = parser(lang).parse(b).root_node
    xs, imps, local, doc, exported, names = [], [], [], '', False, []
    for i, n in enumerate(root.named_children):
        if i == 0 and n.type == 'comment': doc = doc_from_comment(T(n))
        if n.type == 'comment' and not doc and i < 3: doc = doc_from_comment(T(n))
        if n.type == 'import_statement':
            s = field(n, 'source')
            if s is not None: imps.append(unq(T(s)))
            for c in n.named_children:
                if c.type == 'import_clause':
                    for m in re.findall(r'[A-Za-z_$][\w$]*', T(c)):
                        if m not in ('type', 'as', 'from'): names.append(m)
        elif n.type == 'export_statement':
            exported = True
            s = field(n, 'source')
            if s is not None: imps.append(unq(T(s)))
            dflt = any(c.type == 'default' for c in n.children)
            pre = 'default ' if dflt else ''
            d = field(n, 'declaration')
            if d is not None:
                xs += [pre + x for x in ts_decl(d)]; continue
            v = field(n, 'value')
            if v is not None:
                if v.type in FN_TYPES: xs.append(pre + ts_fn_sig(T(field(v, 'name')) or 'fn', v))
                elif v.type in CLASS_TYPES: xs.append(pre + ts_class_sig(T(field(v, 'name')), v))
                elif v.type == 'call_expression':
                    xs.append(pre + squash(T(field(v, 'function')), 40) + '()')
                else: xs.append(pre + squash(T(v), 40))
            for c in n.named_children:
                if c.type == 'export_clause':
                    en = [T(field(sp, 'alias') or field(sp, 'name')) for sp in c.named_children
                             if sp.type == 'export_specifier']
                    if en:
                        xs.append(squash('{' + ','.join(en) + '}' + (f" from {unq(T(s))}" if s is not None else ''), 140))
                elif c.type == 'namespace_export' or (s is not None and not xs):
                    xs.append(f"* from {unq(T(s))}" if s is not None else '*')
        elif n.type in FN_TYPES + CLASS_TYPES:
            local += ts_decl(n)
    if not exported: xs = local
    return xs, imps, doc, names

def py_parse(b):
    root = parser('python').parse(b).root_node
    xs, imps, doc, names = [], [], '', []
    for i, n in enumerate(root.named_children):
        if i == 0 and n.type == 'expression_statement' and n.named_children and n.named_children[0].type == 'string':
            doc = doc_from_comment(T(n.named_children[0]).strip('\'"rbuf'))
        d = field(n, 'definition') if n.type == 'decorated_definition' else n
        if d is None: continue
        nm = T(field(d, 'name'))
        if d.type == 'function_definition' and not nm.startswith('_'):
            rt = field(d, 'return_type')
            xs.append(squash(f"{nm}{T(field(d, 'parameters'))}" + (f" -> {T(rt)}" if rt is not None else '')))
        elif d.type == 'class_definition' and not nm.startswith('_'):
            body = field(d, 'body'); ms = []
            for m in (body.named_children if body is not None else []):
                m = field(m, 'definition') if m.type == 'decorated_definition' else m
                if m is not None and m.type == 'function_definition':
                    mn = T(field(m, 'name'))
                    if not mn.startswith('_') or mn == '__init__': ms.append(mn)
            xs.append(squash(f"class {nm}{{{','.join(ms)}}}", 160))
        elif n.type == 'import_statement':
            for c in n.named_children:
                imps.append(T(field(c, 'name')) if c.type == 'aliased_import' else T(c))
        elif n.type == 'import_from_statement':
            imps.append(T(field(n, 'module_name')))
            names += [T(c) for c in n.named_children[1:] if c.type == 'dotted_name']
    return xs, imps, doc, names

# facts: regexes over raw text
RX_ENV = re.compile(r'process\.env\.([A-Z][A-Z0-9_]+)|process\.env\[[\'"]([A-Z][A-Z0-9_]+)'
                    r'|import\.meta\.env\.([A-Z][A-Z0-9_]+)|os\.environ(?:\.get)?[\[(]\s*[\'"]([A-Z][A-Z0-9_]+)'
                    r'|os\.getenv\(\s*[\'"]([A-Z][A-Z0-9_]+)')
RX_ROUTE = re.compile(r'\b(?:app|router|server|api|\w*[Rr]outer)\.(get|post|put|patch|delete)\(\s*[\'"](/[^\'"]*)'
                      r'|@\w+\.(get|post|put|patch|delete|route)\(\s*[\'"](/[^\'"]*)')
HTTP = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')

def next_route(path, xs):
    m = re.search(r'(?:^|/)(app|pages)/(.*?)(?:/)?(page|route|index)?\.(?:[mc]?[jt]sx?)$', path)
    if not m: return []
    kind, rest, leaf = m.group(1), m.group(2), m.group(3)
    if kind == 'app' and leaf not in ('page', 'route'): return []
    segs = [s for s in rest.split('/') if s and not (s.startswith('(') and s.endswith(')')) and not s.startswith('@')]
    if kind == 'pages' and not leaf: segs = segs  # pages/foo.tsx -> /foo
    if kind == 'pages' and segs and segs[0] in ('_app', '_document', '_error'): return []
    url = '/' + '/'.join(segs)
    if leaf == 'route' or (kind == 'pages' and segs[:1] == ['api']):
        ms = sorted({x.split('(')[0] for x in xs if x.split('(')[0].split('=')[0] in HTTP} |
                    {x.split('=')[0] for x in xs if x.split('=')[0] in HTTP})
        return [f"{','.join(ms) or 'ANY'} {url}"]
    return [f"PAGE {url}"]


# ---------------------------------------------------------------- db schema + usage
# dbdef ops (per file, cached):  ['orm', table, cols, aliases] | ['create', table, cols] | ['add', table, col]
#   | ['drop', table, colname] | ['rename', table, old, new] | ['droptable', table] | ['renametable', old, new]
# col = [name, pk(bool), ref(str|None)]   ref is a raw name (table, model or var), resolved at build.
# dbuse (per file): [[raw_name, 'r'|'w'], ...]  resolved against known tables at build (unknown names dropped).

def match_brace(t, i):
    close = {'(': ')', '{': '}', '[': ']'}; stack, q, j = [], None, i
    while j < len(t):
        c = t[j]
        if q:
            if c == '\\': j += 2; continue
            if c == q: q = None
        elif c in '\'"`': q = c
        elif c in close: stack.append(close[c])
        elif c in ')}]':
            if stack and c == stack[-1]: stack.pop()
            if not stack: return j
        j += 1
    return -1

def split_top(t, sep=','):
    out, cur, depth, q, j = [], [], 0, None, 0
    while j < len(t):
        c = t[j]
        if q:
            cur.append(c)
            if c == '\\' and j + 1 < len(t): cur.append(t[j + 1]); j += 1
            elif c == q: q = None
        elif c in '\'"`': q = c; cur.append(c)
        elif c in '([{': depth += 1; cur.append(c)
        elif c in ')]}': depth -= 1; cur.append(c)
        elif c == sep and depth == 0: out.append(''.join(cur)); cur = []
        else: cur.append(c)
        j += 1
    if ''.join(cur).strip(): out.append(''.join(cur))
    return out

def first_ident(s):
    s = s.strip()
    m = re.match(r'"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|(\S+)', s)
    return next(g for g in m.groups() if g) if m else ''

def tname(s):
    s = s.strip().strip(';')
    s = re.sub(r'[`"\[\]]', '', s)
    return s.split('.')[-1]

def strip_line_comments(t, mark='//'):
    return re.sub(r'(^|[\s;,{(])' + re.escape(mark) + r'[^\n]*', r'\1', t)

RX_DZ = re.compile(r'(?:(?:const|let|var)\s+(\w+)\s*=\s*)?\b(?:\w+\.)?(?:pg|mysql|sqlite)Table\(\s*[\'"`](\w+)[\'"`]\s*,')

def drizzle_defs(text):
    t, ops = strip_line_comments(text), []
    for m in RX_DZ.finditer(t):
        j = t.find('{', m.end())
        if j < 0 or j - m.end() > 60: continue
        k = match_brace(t, j)
        if k < 0: continue
        cols = []
        for part in split_top(t[j + 1:k]):
            mm = re.match(r'\s*[\'"]?(\w+)[\'"]?\s*:\s*\w+\(\s*(?:[\'"`](\w+)[\'"`])?', part)
            if not mm: continue
            ref = re.search(r'references\(\s*\(\)\s*(?::\s*[\w.]+\s*)?=>\s*(\w+)\.', part)
            cols.append([mm.group(2) or mm.group(1), '.primaryKey(' in part, ref.group(1) if ref else None])
        ops.append(['orm', m.group(2), cols, [a for a in (m.group(1),) if a]])
    return ops

def prisma_defs(text):
    t = strip_line_comments(text)
    models = set(re.findall(r'^\s*model\s+(\w+)\s*\{', t, re.M))
    ops = []
    for m in re.finditer(r'^\s*model\s+(\w+)\s*\{', t, re.M):
        k = match_brace(t, m.end() - 1)
        body = t[m.end():k]
        mp = re.search(r'@@map\(\s*"(\w+)"', body)
        table, cols, fks = (mp.group(1) if mp else m.group(1)), [], {}
        cpk = re.search(r'@@id\(\s*(?:fields:\s*)?\[([^\]]*)\]', body)
        cpk = {x.strip() for x in cpk.group(1).split(',')} if cpk else set()
        for line in body.split('\n'):
            tok = line.split()
            if len(tok) < 2 or tok[0].startswith('@'): continue
            base = tok[1].rstrip('?').replace('[]', '')
            if base in models:
                rf = re.search(r'fields:\s*\[([^\]]*)\]', line)
                for fld in (rf.group(1).split(',') if rf else []): fks[fld.strip()] = base
                continue
            cm = re.search(r'@map\(\s*"(\w+)"', line)
            cols.append([tok[0], '@id' in line or tok[0] in cpk, None, cm.group(1) if cm else None])
        out = []
        for name, pk, _, dbn in cols: out.append([dbn or name, pk, fks.get(name)])
        ops.append(['orm', table, out, [m.group(1)]])
    return ops

SQL_SKIP = ('primary', 'constraint', 'foreign', 'unique', 'index', 'key', 'check', 'exclude', 'like', 'period',
            'fulltext', 'spatial', 'projection', 'engine', 'order', 'partition', 'settings', 'ttl')

def sql_defs(text):
    t = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    t = re.sub(r'--[^\n]*', '', t)
    ops = []
    for stmt in split_top(t, ';'):
        st = stmt.strip()
        m = re.match(r'create\s+(?:or\s+replace\s+)?(?:(?:global\s+|local\s+)?(?:unlogged|temporary|temp)\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w."`\[\]]+)\s*(?:on\s+cluster\s+\S+\s*)?\(', st, re.I)
        if m:
            j = m.end() - 1; k = match_brace(st, j)
            cols, tpk, tfk = [], set(), {}
            for part in split_top(st[j + 1:k]):
                p_ = part.strip(); w = p_.split(None, 1)[0].lower().strip('`"[]') if p_ else ''
                if not w: continue
                if w in SQL_SKIP:
                    pk = re.match(r'(?:constraint\s+\S+\s+)?primary\s+key\s*\(([^)]*)\)', p_, re.I)
                    if pk: tpk |= {tname(x) for x in pk.group(1).split(',')}
                    fk = re.search(r'foreign\s+key\s*\(([^)]*)\)\s*references\s+([\w."`\[\]]+)', p_, re.I)
                    if fk:
                        for x in fk.group(1).split(','): tfk[tname(x)] = tname(fk.group(2))
                    continue
                ref = re.search(r'\breferences\s+([\w."`\[\]]+)', p_, re.I)
                cols.append([first_ident(p_), bool(re.search(r'primary\s+key', p_, re.I)), tname(ref.group(1)) if ref else None])
            for c in cols:
                if c[0] in tpk: c[1] = True
                if c[0] in tfk and not c[2]: c[2] = tfk[c[0]]
            ops.append(['create', tname(m.group(1)), cols]); continue
        m = re.match(r'alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?([\w."`\[\]]+)\s*(?:on\s+cluster\s+\S+\s*)?(.*)', st, re.I | re.S)
        if m:
            tb = tname(m.group(1))
            for act in split_top(m.group(2)):
                a = act.strip()
                am = re.match(r'add\s+(?:column\s+)?(?:if\s+not\s+exists\s+)?("[^"]+"|`[^`]+`|[\w\[\]]+)(.*)', a, re.I | re.S)
                if am and am.group(1).lower() not in ('constraint', 'primary', 'foreign', 'unique', 'index', 'check', 'projection'):
                    ref = re.search(r'\breferences\s+([\w."`\[\]]+)', am.group(2), re.I)
                    ops.append(['add', tb, [first_ident(am.group(1)), bool(re.search(r'primary\s+key', a, re.I)), tname(ref.group(1)) if ref else None]]); continue
                fk = re.search(r'foreign\s+key\s*\(([^)]*)\)\s*references\s+([\w."`\[\]]+)', a, re.I)
                if fk:
                    for x in fk.group(1).split(','): ops.append(['fk', tb, tname(x), tname(fk.group(2))])
                    continue
                dm = re.match(r'drop\s+(?:column\s+)?(?:if\s+exists\s+)?([\w"`\[\]]+)', a, re.I)
                if dm and dm.group(1).lower() not in ('constraint', 'index', 'primary', 'projection'):
                    ops.append(['drop', tb, tname(dm.group(1))]); continue
                rm = re.match(r'rename\s+(?:column\s+)?([\w"`\[\]]+)\s+to\s+([\w."`\[\]]+)', a, re.I)
                if rm:
                    if rm.group(1).lower() == 'to': ops.append(['renametable', tb, tname(rm.group(2))])
                    else: ops.append(['rename', tb, tname(rm.group(1)), tname(rm.group(2))])
                    continue
                rt = re.match(r'rename\s+to\s+([\w."`\[\]]+)', a, re.I)
                if rt: ops.append(['renametable', tb, tname(rt.group(1))])
            continue
        m = re.match(r'drop\s+table\s+(?:if\s+exists\s+)?([\w."`\[\]]+)', st, re.I)
        if m: ops.append(['droptable', tname(m.group(1))]); continue
        m = re.match(r'rename\s+table\s+([\w."`\[\]]+)\s+to\s+([\w."`\[\]]+)', st, re.I)
        if m: ops.append(['renametable', tname(m.group(1)), tname(m.group(2))])
    return ops

PY_ORM_BASE = re.compile(r'\b(Base|Model|DeclarativeBase|SQLModel|db\.Model|models\.Model)\b')
def py_orm_defs(b):
    ops = []
    root = parser('python').parse(b).root_node
    for n in root.named_children:
        d = field(n, 'definition') if n.type == 'decorated_definition' else n
        if d is None or d.type != 'class_definition': continue
        sup = T(field(d, 'superclasses'))
        if not PY_ORM_BASE.search(sup) or ('SQLModel' in sup and 'table=True' not in sup): continue
        cname, table, cols = T(field(d, 'name')), None, []
        body = field(d, 'body')
        django = 'models.Model' in sup
        for stn in (body.named_children if body is not None else []):
            line = T(stn)
            tm = re.match(r'__tablename__\s*=\s*[\'"](\w+)', line)
            if tm: table = tm.group(1); continue
            fm = re.match(r'(\w+)\s*(?::\s*[^=]+?)?=\s*(?:\w+\.)*(Column|mapped_column|Field|\w*Field|ForeignKey|OneToOneField)\(', line)
            if not fm or fm.group(1).startswith('_'): continue
            name, kind, ref = fm.group(1), fm.group(2), None
            if kind in ('ForeignKey', 'OneToOneField') and django:
                rr = re.search(r'\(\s*[\'"]?([\w.]+)', line[line.index('('):])
                ref = rr.group(1).split('.')[-1] if rr else None; name += '_id'
            else:
                rr = re.search(r'ForeignKey\(\s*[\'"]([\w]+)\.', line)
                ref = rr.group(1) if rr else None
            if 'ManyToMany' in line: continue
            cols.append([name, 'primary_key=True' in line, ref])
        if not cols: continue
        ops.append(['orm', table or cname.lower(), cols, [cname]])
    return ops

# usage (reads/writes)
RX_DZ_USE = re.compile(r'\.(select|from|insert|update|delete|innerJoin|leftJoin|rightJoin|fullJoin)\(\s*(\w+)\s*[,)]')
RX_DZ_QUERY = re.compile(r'\.query\.(\w+)\.find')
RX_ORM_CALL = re.compile(r'\.(\w+)\.(findMany|findFirst|findUnique|findFirstOrThrow|findUniqueOrThrow|count|aggregate|groupBy'
                         r'|create|createMany|createManyAndReturn|update|updateMany|upsert|delete|deleteMany)\(')
RX_SB = re.compile(r'\.from\(\s*[\'"`](\w+)[\'"`]\s*\)\s*\.\s*(select|insert|update|upsert|delete)')
RX_DJ = re.compile(r'\b([A-Z]\w+)\.objects\.(\w+)')
RX_SA = re.compile(r'\b(?:query|select)\(\s*([A-Z]\w+)\b')
RX_SQL_W = re.compile(r'\b(?:insert\s+into|update|delete\s+from|merge\s+into|truncate(?:\s+table)?)\s+([\w."`]+)', re.I)
RX_SQL_R = re.compile(r'\b(?:from|join)\s+([\w."`]+)', re.I)
RX_STR = re.compile(r'`(?:[^`\\]|\\.)*`|"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')
SQL_HINT = re.compile(r'\b(select|insert|update|delete|merge|truncate)\b', re.I)
WRITE_M = {'insert', 'update', 'delete', 'create', 'createMany', 'createManyAndReturn', 'updateMany', 'upsert', 'deleteMany'}
DJ_W = {'create', 'update', 'delete', 'bulk_create', 'bulk_update', 'get_or_create', 'update_or_create'}

def db_usage(text):
    use = set()
    for m, n in RX_DZ_USE.findall(text): use.add((n, 'w' if m in WRITE_M else 'r'))
    for n in RX_DZ_QUERY.findall(text): use.add((n, 'r'))
    for n, m in RX_ORM_CALL.findall(text): use.add((n, 'w' if m in WRITE_M else 'r'))
    for n, m in RX_SB.findall(text): use.add((n, 'r' if m == 'select' else 'w'))
    for n, m in RX_DJ.findall(text): use.add((n, 'w' if m in DJ_W else 'r'))
    for n in RX_SA.findall(text): use.add((n, 'r'))
    for sm in RX_STR.finditer(text):
        s_ = sm.group(0)
        if len(s_) < 12 or not SQL_HINT.search(s_): continue
        for n in RX_SQL_W.findall(s_): use.add((tname(n), 'w'))
        for n in RX_SQL_R.findall(s_): use.add((tname(n), 'r'))
    return sorted([list(x) for x in use])

def db_parse(path, text, b, lang):
    ext = os.path.splitext(path)[1].lower()
    defs = []
    try:
        if ext == '.prisma': defs = prisma_defs(text)
        elif ext == '.sql': defs = sql_defs(text)
        elif lang == 'python': defs = py_orm_defs(b)
        elif lang and re.search(r'(?:pg|mysql|sqlite)Table\(', text): defs = drizzle_defs(text)
    except (RecursionError, IndexError, ValueError): defs = []
    use = db_usage(text) if lang else []
    return defs, use

def norm_keys(s):
    k = s.lower().replace('_', '')
    out = {k}
    if k.endswith('ies'): out.add(k[:-3] + 'y')
    elif k.endswith('s') and not k.endswith('ss'): out.add(k[:-1])
    else: out.add(k + 's')
    return out

def build_schema(files, info):
    """Merge per-file defs. ORM definitions win over SQL migrations; SQL ops apply in path order."""
    orm, sql, src, alias = {}, {}, {}, {}
    for f in files:
        for op in info[f].get('dbdef', []):
            k = op[0]
            if k == 'orm':
                orm[op[1]] = [list(c) for c in op[2]]; src[op[1]] = f
                for a in op[3] + [op[1]]:
                    for n in norm_keys(a): alias.setdefault(n, op[1])
            elif k == 'create': sql[op[1]] = [list(c) for c in op[2]]; src.setdefault(op[1], f)
            elif k == 'droptable': sql.pop(op[1], None)
            elif k == 'renametable':
                if op[1] in sql: sql[op[2]] = sql.pop(op[1]); src[op[2]] = src.get(op[1], f)
            elif op[1] in sql:
                cols = sql[op[1]]
                if k == 'add' and op[2][0] not in [c[0] for c in cols]: cols.append(list(op[2]))
                elif k == 'drop': sql[op[1]] = [c for c in cols if c[0] != op[2]]
                elif k == 'rename':
                    for c in cols:
                        if c[0] == op[2]: c[0] = op[3]
                elif k == 'fk':
                    for c in cols:
                        if c[0] == op[2]: c[2] = op[3]
    schema = dict(sql); schema.update(orm)
    for t_ in schema:
        for n in norm_keys(t_): alias.setdefault(n, t_)
    def resolve(name):
        for n in norm_keys(name):
            if n in alias: return alias[n]
        return None
    for t_, cols in schema.items():
        for c in cols:
            if c[2]: c[2] = resolve(c[2]) or c[2]
    return schema, src, resolve

def parse_file(path, b):
    ext = os.path.splitext(path)[1].lower()
    lang = CODE_EXT.get(ext)
    text = b.decode('utf8', 'replace')
    xs, imps, doc, names = [], [], '', []
    if lang == 'python': xs, imps, doc, names = py_parse(b)
    elif lang: xs, imps, doc, names = ts_parse(b, lang)
    if lang and lang != 'python':
        imps += re.findall(r'(?:require|import)\(\s*[\'"]([^\'"]+)[\'"]\s*\)', text)
    env = sorted({g for m in RX_ENV.findall(text) for g in m if g})
    dbdef, dbuse = db_parse(path, text, b, lang)
    routes = sorted({f"{(m[0] or m[2]).upper().replace('ROUTE', 'ANY')} {m[1] or m[3]}" for m in RX_ROUTE.findall(text)})
    routes += next_route(path, xs)
    return {'lang': lang or ext.lstrip('.'), 'L': text.count('\n') + (0 if text.endswith('\n') else 1),
            'x': xs, 'imp': list(dict.fromkeys(imps)), 'impn': sorted(set(names)), 'doc': doc, 'env': env,
            'dbdef': dbdef, 'dbuse': dbuse, 'routes': routes}

# ---------------------------------------------------------------- resolution

NODE_BUILTIN = set('assert buffer child_process cluster crypto dgram dns events fs http http2 https net os path '
                   'perf_hooks process querystring readline stream string_decoder timers tls tty url util v8 vm '
                   'worker_threads zlib async_hooks module'.split())

class Resolver:
    def __init__(self, repo, files):
        self.files = set(files)
        tc = load_jsonish(os.path.join(repo, 'tsconfig.json')) or load_jsonish(os.path.join(repo, 'jsconfig.json'))
        co = tc.get('compilerOptions', {}) if isinstance(tc, dict) else {}
        self.base = (co.get('baseUrl') or '').strip('./')
        self.alias = []
        for k, v in (co.get('paths') or {}).items():
            if not v: continue
            tgt = os.path.normpath(os.path.join(self.base, v[0].rstrip('*'))).replace('\\', '/')
            tgt = '' if tgt == '.' else tgt + '/'
            self.alias.append((k.rstrip('*'), tgt, k.endswith('*')))
        self.alias.sort(key=lambda a: -len(a[0]))

    def _try(self, base):
        base = os.path.normpath(base).replace('\\', '/')
        for suf in ('', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '/index.ts', '/index.tsx', '/index.js', '/index.jsx'):
            if base + suf in self.files: return base + suf
        if re.search(r'\.[mc]?js$', base):
            stem_ = re.sub(r'\.[mc]?js$', '', base)
            for suf in ('.ts', '.tsx', '.mts'):
                if stem_ + suf in self.files: return stem_ + suf
        return None

    def ts(self, spec, fpath):
        if spec.startswith('.'):
            return self._try(os.path.join(os.path.dirname(fpath), spec)), None
        for pre, tgt, wild in self.alias:
            if (wild and spec.startswith(pre)) or spec == pre:
                return self._try(tgt + spec[len(pre):] if wild else tgt.rstrip('/')), None
        if self.base or self.base == '':
            hit = self._try(os.path.join(self.base, spec)) if self.base else None
            if hit: return hit, None
        if spec.startswith(('@/', '~/', '#')): return None, None
        spec = spec.removeprefix('node:')
        pkg = '/'.join(spec.split('/')[:2]) if spec.startswith('@') else spec.split('/')[0]
        return None, (None if pkg in NODE_BUILTIN else pkg)

    def py(self, spec, fpath):
        if spec.startswith('.'):
            lvl = len(spec) - len(spec.lstrip('.'))
            d = os.path.dirname(fpath)
            for _ in range(lvl - 1): d = os.path.dirname(d)
            parts = [p for p in spec.lstrip('.').split('.') if p]
            base = os.path.join(d, *parts) if parts else d
            for c in (base + '.py', base + '/__init__.py'):
                c = os.path.normpath(c)
                if c in self.files: return c, None
            return None, None
        parts = spec.split('.')
        for root in ('', 'src/', 'app/', 'lib/'):
            for k in range(len(parts), 0, -1):
                base = root + '/'.join(parts[:k])
                for c in (base + '.py', base + '/__init__.py'):
                    if c in self.files: return c, None
        top = parts[0]
        return None, (None if top in getattr(sys, 'stdlib_module_names', ()) else top)

# ---------------------------------------------------------------- git

def git_log(repo, cache_path, max_commits=5000):
    head = sh(['git', 'rev-parse', 'HEAD'], repo, check=False).strip()
    try:
        with open(cache_path, encoding='utf8') as fh:
            if head and fh.readline().rstrip('\n') == head: return fh.read()
    except OSError: pass
    out = sh(['git', 'log', '--no-merges', '--date=short', '-n', str(max_commits),
              '--format=%x1e%ad%x1f%an%x1f%s', '--name-only'], repo, check=False)
    try: open(cache_path, 'w', encoding='utf8').write(head + '\n' + out)
    except OSError: pass
    return out

def git_history(repo, fileset, cache_path):
    out = git_log(repo, cache_path)
    per = collections.defaultdict(lambda: {'n': 0, 'n90': 0, 'last': '', 'by': collections.Counter(), 'w': [], 't': set()})
    pair, commits = collections.Counter(), []
    for rec in out.split('\x1e')[1:]:
        head, _, body = rec.partition('\n')
        try: date, author, subj = head.split('\x1f', 2)
        except ValueError: continue
        fs = sorted({f for f in body.split('\n') if f in fileset})
        if fs: commits.append((date, author, subj, fs))
    if not commits: return {}, {}
    newest = max(c[0] for c in commits)
    y, m, d = map(int, newest.split('-'))
    cut = f"{y if m > 3 else y - 1:04d}-{(m - 3 - 1) % 12 + 1:02d}-{d:02d}"
    tix_re = re.compile(r'(?<![\w/])#\d+\b|\b[A-Z][A-Z0-9]{1,9}-\d+\b')
    for date, author, subj, fs in commits:  # newest first
        tix = set(tix_re.findall(subj))
        for f in fs:
            p = per[f]; p['n'] += 1; p['by'][author] += 1; p['t'] |= tix
            if date >= cut: p['n90'] += 1
            if not p['last']: p['last'] = date
            if len(p['w']) < 3: p['w'].append(f"{date} {squash(subj, 90)}")
        if 2 <= len(fs) <= 20:
            for a in fs:
                for b in fs:
                    if a != b: pair[(a, b)] += 1
    cc = collections.defaultdict(list)
    for (a, b), k in pair.items():
        r = k / per[a]['n']
        if k >= 2 and r >= 0.3: cc[a].append((r, b))
    return per, {a: sorted(v, key=lambda t: (-t[0], t[1]))[:4] for a, v in cc.items()}

# ---------------------------------------------------------------- build

def module_of(path, rec_count, minf):
    d = os.path.dirname(path)
    while d and rec_count[d] < minf: d = os.path.dirname(d)
    return d or '.'

def stack_of(repo):
    deps = []
    pj = load_jsonish(os.path.join(repo, 'package.json'))
    if isinstance(pj, dict): deps += sorted(pj.get('dependencies', {}))
    for rq in ('requirements.txt',):
        try:
            for l in open(os.path.join(repo, rq)):
                m = re.match(r'\s*([A-Za-z0-9_.\-]+)', l)
                if m and not l.lstrip().startswith('#'): deps.append(m.group(1).lower())
        except OSError: pass
    try:
        py = open(os.path.join(repo, 'pyproject.toml')).read()
        m = re.search(r'dependencies\s*=\s*\[(.*?)\]', py, re.S)
        if m: deps += [re.match(r'[A-Za-z0-9_.\-]+', x).group(0).lower() for x in re.findall(r'"([^"]+)"', m.group(1))]
    except OSError: pass
    return list(dict.fromkeys(deps))[:200]

def load_purposes(ctxdir):
    by_hash, by_path = {}, {}
    try:
        for l in open(os.path.join(ctxdir, 'purposes.tsv'), encoding='utf8'):
            parts = l.rstrip('\n').split('\t')
            if len(parts) >= 3: by_hash[parts[0]] = parts[2]; by_path[parts[1]] = parts[2]
    except OSError: pass
    return by_hash, by_path

def load_mods(ctxdir):
    """mods.tsv: <module path>\t<struct8>\t<purpose>"""
    out = {}
    try:
        for l in open(os.path.join(ctxdir, 'mods.tsv'), encoding='utf8'):
            parts = l.rstrip('\n').split('\t')
            if len(parts) == 3: out[parts[0]] = (parts[1], parts[2])
    except OSError: pass
    return out

def list_files(repo, tracked=False):
    out = sh(['git', 'ls-files', '-z'] + ([] if tracked else ['-co', '--exclude-standard']), repo)
    fs = []
    for f in out.split('\0'):
        if not f or SKIP_RE.search(f): continue
        p = os.path.join(repo, f)
        if os.path.isfile(p) and os.path.getsize(p) <= MAX_BYTES: fs.append(f)
    return sorted(fs)

def build(repo, out, quiet=False, cache_dir=None, tracked=False):
    cache_dir = cache_dir or out
    os.makedirs(os.path.join(out, 'n'), exist_ok=True); os.makedirs(os.path.join(out, 'g'), exist_ok=True)
    cpath = os.path.join(cache_dir, '.cache.json')
    try:
        cache = json.load(open(cpath))
        if cache.get('rev') != PARSE_REV: cache = {}
    except (OSError, ValueError, AttributeError): cache = {}
    pcache, scache = cache.get('parse', {}), cache.get('stat', {})
    newp, news, reparsed = {}, {}, 0

    files = list_files(repo, tracked)
    info, hashes = {}, {}
    for f in files:
        fp = os.path.join(repo, f)
        st = os.stat(fp); sig = [st.st_mtime_ns, st.st_size]
        old = scache.get(f)
        if old and old[:2] == sig and old[2] in pcache and old[2].endswith(':' + f):
            key = old[2]  # unchanged on disk: skip read + hash
            h = key.split(':')[0]
        else:
            b = open(fp, 'rb').read()
            if b'\0' in b[:4096]: continue  # binary
            h = blob_hash(b); key = h + ':' + f
            if key not in pcache: pcache[key] = parse_file(f, b); reparsed += 1
        hashes[f] = h; info[f] = pcache[key]; newp[key] = info[f]; news[f] = sig + [key]
    files = [f for f in files if f in info]
    fid = {f: 'f' + sha(f, 6) for f in files}

    # modules
    rec = collections.Counter()
    for f in files:
        d = os.path.dirname(f)
        while d: rec[d] += 1; d = os.path.dirname(d)
    minf = max(MIN_MOD_FILES, round(len(files) / TARGET_MODS))
    mod = {f: module_of(f, rec, minf) for f in files}
    mid = {m: 'm' + sha(m, 5) for m in set(mod.values())}
    byid = {v: k for k, v in fid.items()}

    # imports + used-by
    R = Resolver(repo, files)
    imp_int, imp_ext = {}, {}
    for f in files:
        ints, exts = set(), set()
        for spec in info[f]['imp']:
            r, e = (R.py if info[f]['lang'] == 'python' else R.ts)(spec, f)
            if r and r != f: ints.add(r)
            elif e: exts.add(e)
        imp_int[f], imp_ext[f] = sorted(ints), sorted(exts)
    used_by = collections.defaultdict(set)
    for f, ints in imp_int.items():
        for g in ints: used_by[g].add(f)

    schema, schema_src, resolve = build_schema(files, info)
    dbr, dbw = collections.defaultdict(set), collections.defaultdict(set)
    for f in files:
        for raw, k in info[f].get('dbuse', []):
            t_ = resolve(raw)
            if t_: (dbw if k == 'w' else dbr)[f].add(t_)

    def facts(f):
        i, e = info[f], []
        defs = sorted({op[1] for op in i.get('dbdef', []) if op[0] in ('orm', 'create')})
        w = sorted(dbw[f] - set(defs)); r = sorted(dbr[f] - set(defs) - dbw[f])
        if i['env']: e.append('env:' + ','.join(i['env']))
        if defs: e.append('db+:' + ','.join(defs))
        if r: e.append('db:' + ','.join(r))
        if w: e.append('dbw:' + ','.join(w))
        e += ['route:' + r for r in i['routes']]
        return e

    per, cc = git_history(repo, set(files), os.path.join(cache_dir, '.git.cache'))
    pur_h, pur_p = load_purposes(out if os.path.exists(os.path.join(out, 'purposes.tsv')) else cache_dir)
    modp = load_mods(out if os.path.exists(os.path.join(out, 'mods.tsv')) else cache_dir)

    # ---- write n/ and g/
    mods = collections.defaultdict(list)
    for f in files: mods[mod[f]].append(f)
    written = set()
    root_mods, groot = [], []
    for m in sorted(mods):
        fs = sorted(mods[m]); M = mid[m]
        mh = sha(''.join(hashes[f] for f in fs), 8)
        lines = [f"@{M} mod {m} h={mh}"]
        glines = []
        mfacts, mkeys, mdeps, mn, mn90, mlast = collections.Counter(), [], set(), 0, 0, ''
        for f in fs:
            i = info[f]
            lines.append(f"@{fid[f]} {f} L={i['L']} h={hashes[f][:8]}")
            if hashes[f][:8] in pur_h: lines.append('p: ' + pur_h[hashes[f][:8]])
            elif f in pur_p: lines.append('p~: ' + pur_p[f])
            elif i['doc']: lines.append('p: ' + i['doc'])
            triv = collections.defaultdict(list)
            for x in i['x']:
                k = x.split(' ', 1)[0]
                if k in ('type', 'iface', 'enum') and ' ' in x: triv[k].append(x.split(' ', 1)[1])
                else: lines.append('x: ' + x)
            lines += [f"x: {k} {','.join(v)}" for k, v in triv.items()]
            ii = ' '.join(fid[g] for g in imp_int[f])
            ie = ('ext:' + ','.join(imp_ext[f])) if imp_ext[f] else ''
            if ii or ie: lines.append('i: ' + ' | '.join(x for x in (ii, ie) if x))
            ub = sorted(fid[g] for g in used_by[f])
            if ub: lines.append('u: ' + ' '.join(ub[:UB_CAP]) + (f" +{len(ub) - UB_CAP}" if len(ub) > UB_CAP else ''))
            fx = facts(f)
            if fx: lines.append('e: ' + ' '.join(fx))
            for x in fx:
                k, v = x.split(':', 1)
                for vv in ([v] if k == 'route' else v.split(',')): mfacts[k + ':' + vv] += 1
            for x in i['x']:
                if x.startswith('{'): mkeys += x[1:].split('}')[0].split(',')
                elif not x.startswith(('*', 'type ', 'iface ', 'enum ')):
                    mkeys.append(re.split(r'[({=<]', x.removeprefix('default ').removeprefix('class '))[0])
            mdeps |= {mod[g] for g in imp_int[f] if mod[g] != m}
            if f in per:
                p = per[f]; mn += p['n']; mn90 += p['n90']; mlast = max(mlast, p['last'])
                g = [f"@{fid[f]} n={p['n']} last={p['last']} by=" + ','.join(f"{a.replace(' ', '_')}:{k}" for a, k in p['by'].most_common(3))]
                if f in cc: g.append('cc: ' + ' '.join(f"{fid[b]}:{r:.1f}".replace(':0.', ':.') for r, b in cc[f]))
                if p['t']: g.append('t: ' + ' '.join(sorted(p['t'])[:8]))
                g += ['w: ' + w for w in p['w']]
                glines += g
        open(os.path.join(out, 'n', M + '.ct'), 'w').write('\n'.join(lines) + '\n')
        written.add('n/' + M + '.ct')
        if glines:
            open(os.path.join(out, 'g', M + '.gt'), 'w').write('\n'.join(glines) + '\n')
            written.add('g/' + M + '.gt')
        mused = sum(1 for m2 in mods if m2 != m and any(mod[g] == m for f2 in mods[m2] for g in imp_int[f2]))
        mp = modp.get(m, ('', ''))[1]
        if not mp:
            rd = next((f for f in fs if os.path.dirname(f) == ('' if m == '.' else m) and f.lower().endswith('readme.md')), None)
            if rd:
                txt = [l.strip() for l in open(os.path.join(repo, rd), encoding='utf8', errors='replace')]
                txt = [l for l in txt if len(l) > 20 and not l.startswith(('#', '!', '[', '<', '|', '`', '-', '*', '>'))]
                mp = squash(txt[0], 100) if txt else ''
        keys = list(dict.fromkeys(k for k in mkeys if k))
        fsum = collections.Counter()
        for k in mfacts: fsum[k.split(':')[0]] += 1
        ent = [f"@{M} {m} f={len(fs)}"]
        if mn: groot.append(f"@{M} c={mn}/{mn90} last={mlast}")
        if mp: ent.append('  p: ' + mp)
        if keys: ent.append('  k: ' + squash(' '.join(keys), 120))
        dl = ' '.join(sorted(mid[d] for d in mdeps))
        if dl or mused: ent.append('  d: ' + (dl or '-') + f"   u: {mused}")
        ef = []
        tbls = sorted({k.split(':', 1)[1] for k in mfacts if k.startswith(('db:', 'db+:', 'dbw:'))})
        if tbls: ef.append('db:' + ','.join(tbls[:10]) + ('…' if len(tbls) > 10 else ''))
        envs = sorted({k.split(':', 1)[1] for k in mfacts if k.startswith('env:')})
        if envs: ef.append('env:' + ','.join(envs[:8]) + ('…' if len(envs) > 8 else ''))
        if fsum['route']: ef.append(f"routes:{fsum['route']}")
        if ef: ent.append('  e: ' + ' '.join(ef))
        root_mods.append('\n'.join(ent))

    if groot:
        open(os.path.join(out, 'g', 'root.gt'), 'w').write('\n'.join(groot) + '\n'); written.add('g/root.gt')
    # stale node files
    for sub in ('n', 'g'):
        for fn in os.listdir(os.path.join(out, sub)):
            if f"{sub}/{fn}" not in written and fn != 'idx.tsv': os.remove(os.path.join(out, sub, fn))

    # ---- root
    langs = collections.Counter(os.path.splitext(f)[1].lstrip('.') or 'none' for f in files)
    name = os.path.basename(os.path.abspath(repo))
    head = [f"{VERSION} repo={name} files={len(files)} mods={len(mods)}" + (f" tables={len(schema)}" if schema else '')]
    dbl = []
    writers = collections.defaultdict(set)
    for f in files:
        for t_ in dbw[f]: writers[t_].add(fid[f])
    for t_ in sorted(schema):
        cs = ' '.join((f'"{c[0]}"' if ' ' in c[0] else c[0]) + ('*' if c[1] else '') + (f"→{c[2]}" if c[2] else '') for c in schema[t_])
        wl = sorted(writers[t_])
        dbl.append(f"{t_}({cs}) <{schema_src.get(t_, '?')}" + (f" w:{','.join(wl[:6])}" + (f"+{len(wl) - 6}" if len(wl) > 6 else '') if wl else ''))
    dbp = os.path.join(out, 'db.ct')
    if dbl: open(dbp, 'w').write('# table(col* = pk, col→table = fk) <defined-in  w: writer file ids\n' + '\n'.join(dbl) + '\n')
    elif os.path.exists(dbp): os.remove(dbp)
    st = stack_of(repo)
    if st: head.append('stack: ' + ' '.join(st))
    head.append('lang: ' + ' '.join(f"{k}:{v}" for k, v in sorted(langs.items(), key=lambda t: (-t[1], t[0]))[:10]))
    open(os.path.join(out, 'root.ct'), 'w').write('\n'.join(head + root_mods) + '\n')

    # ---- idx
    idx = collections.defaultdict(set)
    internal_names = set()
    for f in files:
        for x in info[f]['x']:
            internal_names |= set(re.findall(r'[A-Za-z_]\w*', re.split(r'[(=]', x.removeprefix('default ').removeprefix('class '))[0]))
    for f in files:
        i, F = info[f], fid[f]
        src = [f] + i['x'] + i['env'] + i['routes']
        src += [nm for nm in info[f].get('impn', []) if nm in internal_names and len(nm) > 3]
        src += sorted(dbr[f] | dbw[f])
        for op in i.get('dbdef', []):
            if op[0] in ('orm', 'create'): src += [op[1]] + [c[0] for c in op[2]]
        for s in src:
            for t_ in terms(s): idx[t_].add(F)
    open(os.path.join(out, 'idx.tsv'), 'w').write(''.join(f"{t_}\t{','.join(sorted(v))}\n" for t_, v in sorted(idx.items())))
    gidx = collections.defaultdict(set)
    for f in files:
        for t_ in (per[f]['t'] if f in per else ()): gidx[t_.lower()].add(fid[f])
    open(os.path.join(out, 'g', 'idx.tsv'), 'w').write(''.join(f"{t_}\t{','.join(sorted(v))}\n" for t_, v in sorted(gidx.items())))

    # ---- housekeeping
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(here, 'SPEC.md')) and os.path.realpath(out) != os.path.realpath(here):
        shutil.copyfile(os.path.join(here, 'SPEC.md'), os.path.join(out, 'SPEC.md'))
    open(os.path.join(out, '.gitignore'), 'w').write(GITIGNORE)
    json.dump({'rev': PARSE_REV, 'parse': newp, 'stat': news}, open(cpath, 'w'))
    if not quiet:
        tot = sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, fns in os.walk(out) for fn in fns if fn.endswith(('.ct', '.gt', '.tsv')))
        raw = sum(os.path.getsize(os.path.join(repo, f)) for f in files)
        rootsz = os.path.getsize(os.path.join(out, 'root.ct'))
        print(f"ctx: {len(files)} files, {len(mods)} modules, reparsed {reparsed}. "
              f"root ~{rootsz // 4} tok | full index ~{tot // 4} tok | raw source ~{raw // 4} tok", file=sys.stderr)
    return out

# ---------------------------------------------------------------- read side

def find_repo(start='.'):
    return sh(['git', 'rev-parse', '--show-toplevel'], start).strip()

def blocks(path):
    out, cur = {}, None
    try:
        for l in open(path, encoding='utf8'):
            if l.startswith('@'):
                cur = l[1:].split(' ', 1)[0]; out[cur] = [l.rstrip('\n')]
            elif cur: out[cur].append(l.rstrip('\n'))
    except OSError: pass
    return out

def load_db(ctx):
    out = {}
    fp = os.path.join(ctx, 'db.ct')
    if os.path.exists(fp):
        for l in open(fp, encoding='utf8'):
            if l.startswith('#') or '(' not in l: continue
            out[l.split('(', 1)[0]] = l.rstrip('\n')
    return out

def load_index(ctx):
    root = open(os.path.join(ctx, 'root.ct'), encoding='utf8').read().split('\n')
    header = [l for l in root if l and not l.startswith(('@', '  '))]
    rootb = {}
    cur = None
    for l in root:
        if l.startswith('@'): cur = l[1:].split(' ', 1)[0]; rootb[cur] = [l]
        elif l.startswith('  ') and cur: rootb[cur].append(l)
    n, g, f2m = {}, {}, {}
    for fn in os.listdir(os.path.join(ctx, 'n')):
        M = fn[:-3]
        for k, v in blocks(os.path.join(ctx, 'n', fn)).items():
            if k != M: n[k] = v; f2m[k] = M
    gd = os.path.join(ctx, 'g')
    for fn in (os.listdir(gd) if os.path.isdir(gd) else []):
        if fn.endswith('.gt'): g.update(blocks(os.path.join(gd, fn)))
    idx = collections.defaultdict(list)
    for fp in (os.path.join(ctx, 'idx.tsv'), os.path.join(gd, 'idx.tsv')):
        if not os.path.exists(fp): continue
        for l in open(fp, encoding='utf8'):
            t_, ids = l.rstrip('\n').split('\t'); idx[t_] += ids.split(',')
    return header, rootb, n, g, f2m, idx

STOP = set('the and for with that this from into when then than have has are was were will would should could '
           'can not but all any our your their they them client wants want need needs please also like make add '
           'new each every after before should able using use get set via page user'.split())

def query(ctx, repo, text, budget):
    header, rootb, n, g, f2m, idx = load_index(ctx)
    N = max(1, len(n))
    qt = {stem(w.lower()) for w in re.findall(r'[A-Za-z]{3,}|#\d+|[A-Z]+-\d+', text)} - STOP
    score = collections.Counter()
    for q in qt:
        for t_, ids in idx.items():
            if t_ == q: w = 1.0
            elif len(q) >= 4 and len(t_) >= 4 and (t_.startswith(q) or q.startswith(t_)): w = 0.5
            else: continue
            idf = math.log(1 + N / len(ids))
            for i in ids: score[i] += w * idf
    edges = collections.defaultdict(set)
    for i, blk in n.items():
        for l in blk:
            if l.startswith(('i: ', 'u: ')):
                for x in re.findall(r'\bf[0-9a-f]{6}\b', l): edges[i].add(x); edges[x].add(i)
    base = dict(score)
    for i, sc in base.items():
        for j in edges[i]: score[j] += 0.25 * sc
    if not score:
        print('\n'.join(header + [l for b in rootb.values() for l in b])); return
    best = score.most_common(1)[0][1]
    hits = [i for i, s in score.most_common(20) if s >= 0.35 * best][:12]
    out = list(header)
    shown_m = []
    for i in hits:
        if f2m[i] not in shown_m: shown_m.append(f2m[i])
    hist = load_hist(ctx)
    for k_, M in enumerate(shown_m):
        out += rootb.get(M, [])
        if k_ < 3 and hist.get(M, {}).get('lines'): out += ['  h: ' + l for l in hist[M]['lines']]
    out.append('--')
    lim = budget * 4
    used = set()
    for i in hits:
        blk = n[i] + g.get(i, [])
        if len('\n'.join(out + blk)) > lim and used: break
        out += blk; used.add(i)
    near = collections.Counter()
    for i in used:
        for l in n[i] + g.get(i, []):
            if l.startswith(('u: ', 'i: ')):
                for x in re.findall(r'\bf[0-9a-f]{6}\b', l): near[x] += 1
            if l.startswith('cc: '):
                for x, r in re.findall(r'\b(f[0-9a-f]{6}):(\.\d|1\.0)', l): near[x] += 2 + float(r)
    near_l = [f"{x} {n[x][0].split(' ')[1]}" for x, _ in near.most_common(10) if x not in used and x in n]
    if near_l: out += ['--', 'near: ' + ' | '.join(near_l)]
    db = load_db(ctx)
    if db:
        want = []
        for i in used:
            for l in n[i]:
                if l.startswith('e: '):
                    for fx in l[3:].split():
                        if fx.startswith(('db:', 'db+:', 'dbw:')): want += fx.split(':', 1)[1].split(',')
        direct = [t_ for t_ in db if norm_keys(t_) & qt
                  or any(norm_keys(c) & qt for c in re.findall(r'[\s(](\w+)', db[t_].split(') <')[0]))]
        want = [t_ for t_ in dict.fromkeys(direct + want) if t_ in db]
        fks = [r for t_ in want for r in re.findall(r'→(\w+)', db[t_].split(') <')[0])]  # one FK hop
        want = [t_ for t_ in dict.fromkeys(want + fks) if t_ in db][:12]
        if want: out += ['--', 'db:'] + [db[t_] for t_ in want]
    s = '\n'.join(out)
    print(s)
    touched = sum(os.path.getsize(os.path.join(repo, n[i][0].split(' ')[1])) for i in used
                  if os.path.exists(os.path.join(repo, n[i][0].split(' ')[1])))
    print(f"[ctx q] emitted ~{len(s) // 4} tok | raw of {len(used)} matched files ~{touched // 4} tok | "
          f"terms: {' '.join(sorted(qt))}", file=sys.stderr)


# ---------------------------------------------------------------- sync: LLM layer (purposes, module digests, overview)
#
# Phases, run in order (each needs the previous one):
#   files     one-line purpose per source file              -> purposes.tsv  (keyed by blob hash)
#   mods      purpose per module + history digest           -> mods.tsv (keyed by structure hash), hist.ct (upto=<sha>)
#   overview  human-readable repo overview                  -> OVERVIEW.md   (keyed by hash of mods.tsv + hist.ct)
# `sync plan` writes self-contained prompt batches to .ctx/.sync/. Any runner (Claude Code subagents,
# `claude -p`, the API) writes <batch>.out next to them, then `sync apply` validates and merges.

SYNC_EXT = set(CODE_EXT) | {'.sql', '.prisma', '.sh', '.go', '.rs', '.rb', '.php', '.java', '.kt', '.cs', '.vue', '.svelte'}
SYNC_NAMES = re.compile(r'(^|/)(Dockerfile[^/]*|docker-compose[^/]*\.ya?ml|\.github/workflows/[^/]+\.ya?ml|Makefile)$')
BATCH_CHARS, FILE_CHARS, BATCH_FILES = 120000, 4000, 50  # fewer, larger calls: per-call overhead is ~4.5k tokens
HIST_MIN_NEW, HIST_MAX_COMMITS = 3, 150
NOISE = re.compile(r'\b(bump|deps?|dependenc|typo|format|lint|prettier|merge|wip|chore|readme|changelog|version|release)\b', re.I)
SIGNAL = re.compile(r'\b(add|feat|feature|migrat|refactor|remove|rewrite|replace|switch|introduc|implement|support|deprecat|breaking|security|fix)\w*', re.I)
NBLK = re.compile(r'^@(f\w+) (.+) L=(\d+) h=(\w+)$')

def sync_dir(ctx): return os.path.join(ctx, '.sync')

RX_IMPORT_LINE = re.compile(r'^\s*(import\s[^;]*?from\s+[\'"][^\'"]+[\'"];?|import\s+[\'"][^\'"]+[\'"];?'
                            r'|from\s+\S+\s+import\s+[^\n]+|import\s+[\w., ]+|(const|let|var)\s+\w+\s*=\s*require\([^)]*\);?)\s*$', re.M | re.S)
def compact_source(src):
    """imports are already in the index (i:); whitespace costs tokens"""
    src = RX_IMPORT_LINE.sub('', src)
    src = re.sub(r'[ \t]+', ' ', src)
    return re.sub(r'\n\s*\n+', '\n', src).strip()

def read_mod_files(ctx):
    """-> {mod_id: (mod_path, [(fid, path, L, blob8, block_lines)])}, struct8 per module."""
    mods, struct = {}, {}
    nd = os.path.join(ctx, 'n')
    for fn in sorted(os.listdir(nd)):
        lines = open(os.path.join(nd, fn), encoding='utf8').read().split('\n')
        M, mpath = fn[:-3], lines[0].split(' ')[2]
        files, cur, sig = [], None, []
        for l in lines[1:]:
            m = NBLK.match(l)
            if m:
                cur = [m.group(1), m.group(2), int(m.group(3)), m.group(4), []]; files.append(cur); sig.append('@' + m.group(2))
            elif cur is not None and l:
                cur[4].append(l)
                if not l.startswith(('p: ', 'p~: ')): sig.append(l)
        mods[M] = (mpath, files); struct[M] = sha('\n'.join(sig), 8)
    return mods, struct

def load_hist(ctx):
    """hist.ct -> {mod_id: {'path','upto','n','lines'}}"""
    out, cur = {}, None
    try:
        for l in open(os.path.join(ctx, 'hist.ct'), encoding='utf8'):
            l = l.rstrip('\n')
            m = re.match(r'^@(m\w+) (\S+) upto=(\w*) n=(\d+)$', l)
            if m: cur = out[m.group(1)] = {'path': m.group(2), 'upto': m.group(3), 'n': int(m.group(4)), 'lines': []}
            elif cur is not None and l: cur['lines'].append(l)
    except OSError: pass
    return out

def save_hist(ctx, hist):
    body = [f"# per-module history digest (LLM-written from commits up to upto=<sha>). Format: YYYY-MM event"]
    for M in sorted(hist, key=lambda k: hist[k]['path']):
        h = hist[M]; body.append(f"@{M} {h['path']} upto={h['upto']} n={h['n']}"); body += h['lines']
    open(os.path.join(ctx, 'hist.ct'), 'w', encoding='utf8').write('\n'.join(body) + '\n')

def git_commits(repo, limit=3000):
    out = sh(['git', 'log', '--no-merges', '--date=short', '-n', str(limit),
              '--format=%x1e%H%x1f%ad%x1f%s%x1f%b%x1f', '--name-only'], repo, check=False)
    cs = []
    for rec in out.split('\x1e')[1:]:
        parts = rec.split('\x1f')
        if len(parts) < 5: continue
        body = squash(parts[3], 160)
        cs.append({'sha': parts[0], 'date': parts[1], 'subj': squash(parts[2], 110), 'body': body,
                   'files': {f for f in parts[4].split('\n') if f}})
    return cs  # newest first

def overview_src(ctx):
    """changes when module purposes change, or a module gets history in a new month (not on every digest line)"""
    h = hashlib.sha1()
    p_ = os.path.join(ctx, 'mods.tsv')
    if os.path.exists(p_): h.update(open(p_, 'rb').read())
    for M, hh in sorted(load_hist(ctx).items()):
        h.update(f"{M}:{max((l[:7] for l in hh['lines']), default='')}".encode())
    return h.hexdigest()[:8]

def sync_pending(repo, ctx, with_hist=True):
    mods, struct = read_mod_files(ctx)
    pur_h, pur_p = load_purposes(ctx)
    modp = load_mods(ctx)
    files = []
    for M, (mpath, fs) in mods.items():
        for fid_, path, L, b8, blk in fs:
            if L < 3 or b8 in pur_h: continue
            if os.path.splitext(path)[1].lower() in SYNC_EXT or SYNC_NAMES.search(path):
                files.append((M, fid_, path, b8, blk, pur_p.get(path)))
    mod_p = [M for M in mods if modp.get(mods[M][0], ('',))[0] != struct[M]]
    mod_h, commits = {}, []
    if with_hist:
        hist = load_hist(ctx)
        commits = git_commits(repo)
        head = commits[0]['sha'][:12] if commits else ''
        f2mod = {x[1]: M for M, (_, fs) in mods.items() for x in fs}
        root_m = next((M for M, (mp_, _) in mods.items() if mp_ == '.'), None)
        for c in commits:  # commits spanning many modules are repo-wide: they belong to the root digest only
            c['mods'] = {f2mod[f] for f in c['files'] if f in f2mod}
        for M, (mpath, fs) in mods.items():
            paths = {x[1] for x in fs}
            rel = lambda c: (c['files'] & paths and len(c['mods']) <= 3) or (M == root_m and len(c['mods']) > 3)
            new = []
            upto = hist.get(M, {}).get('upto', '')
            for c in commits:
                if upto and c['sha'].startswith(upto): break
                if rel(c): new.append(c)
            else:
                if upto and M in hist: new = [c for c in commits if rel(c)]; hist[M]['lines'] = []  # upto vanished (rebase): redo
            if (M not in hist and new) or len(new) >= HIST_MIN_NEW:
                mod_h[M] = new
        mod_h = {M: v for M, v in mod_h.items()}
    ov = None
    if not files and not mod_p and not mod_h:
        op = os.path.join(ctx, 'OVERVIEW.md')
        cur = open(op, encoding='utf8').readline() if os.path.exists(op) else ''
        if f"src={overview_src(ctx)}" not in cur: ov = True
    return {'mods': mods, 'struct': struct, 'files': files, 'mod_p': mod_p, 'mod_h': mod_h,
            'overview': ov, 'head': commits[0]['sha'][:12] if commits else ''}

def repo_header(ctx):
    root = open(os.path.join(ctx, 'root.ct'), encoding='utf8').read().split('\n')
    name = re.search(r'repo=(\S+)', root[0]).group(1)
    stack = next((l[7:] for l in root if l.startswith('stack: ')), '')
    return name, squash(stack, 400)

FILE_PROMPT = """You write one-line purposes for source files. They go into a compact repo index that AI coding agents read
instead of the source, so they must be precise and information-dense.

Repo: {repo}
Stack: {stack}

For EACH file below output exactly one line, nothing else, no code fences:
<id>\\t<purpose>
Rules:
- max 90 characters, plain ASCII
- say what it does and its role, plus key side effects (DB writes, external APIs, emails, auth checks)
  good: "Payment webhook: verifies signature, marks invoices paid in invoices table, emails receipt"
  bad:  "This file contains the webhook route"
- don't repeat the file path or the exported names from the x: line, don't start with "This file"
- if an old purpose is given, keep it if still correct, otherwise fix it
"""

MOD_PROMPT = """You maintain module summaries and history digests for a compact repo index that AI coding agents read.

Repo: {repo}
Stack: {stack}

For EACH module below output only these lines, nothing else, no code fences:
M <id>\\t<purpose>                 only if the module says "purpose: requested". max 100 chars: what the module is for
H <id>\\t<YYYY-MM> <event>         only if the module says "history: requested". 0-8 lines, oldest first, max 100 chars each

History digest = the few milestones and decisions that explain why the module looks the way it does today:
features added, data model changes, migrations, rewrites, switches of library/provider, removals, notable fixes,
ticket refs (#123). Merge the existing digest with the new commits into at most 8 lines. Drop trivia (dependency
bumps, formatting, typos, merges). Use concrete names (tables, providers, endpoints). Never invent anything that
is not supported by the commits or existing digest.
"""

OV_PROMPT = """Write OVERVIEW.md for the repository "{repo}": a concise orientation document for a developer or client
who has never seen the code. Use ONLY the index below; don't invent features. Markdown, max ~120 lines, sections:

1. What it is (2-3 sentences)
2. Architecture: modules and how they depend on each other (use module paths)
3. Data model: main tables and relations (only if a schema is given)
4. External services and config (stack, env vars)
5. How it evolved: the key milestones from the history digests, chronologically
6. Hotspots: most-changed modules (from churn), and what that suggests
7. Gaps and risks you can see from the index (e.g. no tests module, no email lib), phrased as things to verify

Output the markdown only.

<index>
{index}
</index>
"""

def _new_batch(d, phase, k, text, manifest):
    base = os.path.join(d, f"{phase}-{k:03d}")
    open(base + '.md', 'w', encoding='utf8').write(text)
    json.dump(manifest, open(base + '.json', 'w'))
    return base + '.md'

def sync_plan(repo, ctx, max_files=0, quiet=False):
    refresh(repo, ctx)
    st = sync_pending(repo, ctx)
    d = sync_dir(ctx)
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    repo_name, stack = repo_header(ctx)
    batches = []
    if st['files']:
        items = st['files'][:max_files] if max_files else st['files']
        cur, cur_len, man = [], 0, {}
        def flush():
            if cur:
                txt = FILE_PROMPT.format(repo=repo_name, stack=stack) + '\n' + '\n'.join(cur)
                batches.append(_new_batch(d, 'files', len(batches) + 1, txt, {'phase': 'files', 'items': dict(man)}))
        for M, fid_, path, b8, blk, old in items:
            try: src = compact_source(open(os.path.join(repo, path), encoding='utf8', errors='replace').read())
            except OSError: continue
            if len(src) > FILE_CHARS: src = src[:FILE_CHARS] + f"\n... [truncated, {len(src)} chars total]"
            meta = '\n'.join(l for l in blk if l.startswith(('x: ', 'e: ')))
            sec = f"### id={len(man) + 1} {path}\n" + (f"old purpose: {old}\n" if old else '') + (meta + '\n' if meta else '') + f"```\n{src}\n```\n"
            if cur and (cur_len + len(sec) > BATCH_CHARS or len(man) >= BATCH_FILES):
                flush(); cur, cur_len, man = [], 0, {}
                sec = re.sub(r'^### id=\d+', '### id=1', sec)
            man[str(len(man) + 1)] = [path, b8]
            cur.append(sec); cur_len += len(sec)
        flush()
        phase = 'files'
    elif st['mod_p'] or st['mod_h']:
        pur_h, _ = load_purposes(ctx)
        hist = load_hist(ctx)
        cur, cur_len, man = [], 0, {}
        def flush():
            if cur:
                txt = MOD_PROMPT.format(repo=repo_name, stack=stack) + '\n' + '\n'.join(cur)
                batches.append(_new_batch(d, 'mods', len(batches) + 1, txt, {'phase': 'mods', 'head': st['head'], 'items': dict(man)}))
        for M in sorted(set(st['mod_p']) | set(st['mod_h']), key=lambda k: st['mods'][k][0]):
            mpath, fs = st['mods'][M]
            want_p, want_h = M in st['mod_p'], M in st['mod_h']
            fl = []
            for fid_, path, L, b8, blk in fs[:60]:
                fl.append(f"- {path} ({L} lines): {pur_h.get(b8, '')}".rstrip(': '))
            if len(fs) > 60: fl.append(f"- ... {len(fs) - 60} more files")
            sec = [f"### id={{ID}} module {mpath}" + (" (root: also gets repo-wide commits)" if mpath == '.' else ''), f"purpose: {'requested' if want_p else 'not requested'}",
                   f"history: {'requested' if want_h else 'not requested'}", 'files:'] + fl
            if want_h:
                new = st['mod_h'][M]
                if len(new) > HIST_MAX_COMMITS:
                    def score(c):
                        t_ = c['subj'] + ' ' + c['body']
                        return (2 if SIGNAL.search(t_) else 0) + (1 if c['body'] else 0) + (1 if re.search(r'#\d+', t_) else 0) - (3 if NOISE.search(c['subj']) else 0)
                        # stable: newest first among equal score
                    keep = sorted(range(len(new)), key=lambda i: (-score(new[i]), i))[:HIST_MAX_COMMITS]
                    omitted = len(new) - HIST_MAX_COMMITS
                    new = [new[i] for i in sorted(keep)]
                else: omitted = 0
                old = hist.get(M, {}).get('lines', [])
                sec += ['existing digest:'] + (old or ['(none)'])
                sec += [f"new commits ({len(new)}, oldest first" + (f"; {omitted} low-signal commits omitted" if omitted else '') + '):']
                sec += [f"{c['date']} {c['subj']}" + (f" | {c['body']}" if c['body'] else '') for c in reversed(new)]
            txt_ = '\n'.join(sec) + '\n'
            if cur and cur_len + len(txt_) > BATCH_CHARS:
                flush(); cur, cur_len, man = [], 0, {}
            k = str(len(man) + 1)
            man[k] = {'mod': M, 'path': mpath, 'struct': st['struct'][M], 'p': want_p, 'h': want_h,
                      'n': len(st['mod_h'].get(M, [])) + hist.get(M, {}).get('n', 0)}
            cur.append(txt_.replace('{ID}', k)); cur_len += len(txt_)
        flush()
        phase = 'mods'
    elif st['overview']:
        parts = [open(os.path.join(ctx, 'root.ct'), encoding='utf8').read()]
        for fn in ('hist.ct', 'db.ct'):
            p_ = os.path.join(ctx, fn)
            if os.path.exists(p_): parts.append(open(p_, encoding='utf8').read())
        gr = os.path.join(ctx, 'g', 'root.gt')
        if os.path.exists(gr): parts.append('# churn per module: c=<commits>/<last 90 days>\n' + open(gr, encoding='utf8').read())
        txt = OV_PROMPT.format(repo=repo_name, index='\n'.join(parts)[:120000])
        batches.append(_new_batch(d, 'overview', 1, txt, {'phase': 'overview', 'src': overview_src(ctx)}))
        phase = 'overview'
    else: phase = None
    tok = sum(os.path.getsize(b) for b in batches) // 3
    if not quiet:
        if not batches: print('ctx sync: nothing pending')
        else:
            print(f"ctx sync plan: phase={phase} batches={len(batches)} ~{tok // 1000}k input tokens"
                  + (f" ({len(st['files'])} files)" if phase == 'files' else '')
                  + (f" ({len(set(st['mod_p']) | set(st['mod_h']))} modules)" if phase == 'mods' else ''))
            for b in batches: print(f"  {os.path.relpath(b, repo)}  ->  {os.path.relpath(b[:-3] + '.out', repo)}")
    return phase, batches

def clean_line(s, lim):
    s = re.sub(r'\s+', ' ', s.replace('\t', ' ')).strip().strip('"').strip()
    s = re.sub(r'^[-*]\s+', '', s)
    return squash(s, lim) if s else ''

def sync_apply(repo, ctx, quiet=False):
    d = sync_dir(ctx)
    if not os.path.isdir(d): return 0, 0
    refresh(repo, ctx)
    mods, struct = read_mod_files(ctx)
    cur_hash = {path: b8 for _, fs in mods.values() for _, path, _, b8, _ in fs}
    ok = bad = 0
    pf = os.path.join(ctx, 'purposes.tsv')
    rows = {}
    if os.path.exists(pf):
        for l in open(pf, encoding='utf8'):
            parts = l.rstrip('\n').split('\t')
            if len(parts) >= 3: rows[parts[1]] = parts
    modp = load_mods(ctx); hist = load_hist(ctx)
    for fn in sorted(os.listdir(d)):
        if not fn.endswith('.json'): continue
        base = os.path.join(d, fn[:-5])
        if not os.path.exists(base + '.out'): continue
        man = json.load(open(base + '.json'))
        out = open(base + '.out', encoding='utf8', errors='replace').read()
        out = re.sub(r'^```\w*\s*$', '', out, flags=re.M)
        if man['phase'] == 'files':
            for l in out.split('\n'):
                m = re.match(r'^\s*(?:id=)?(\d+)\s*[\t:|]\s*(.+)$', l) or re.match(r'^\s*(?:id=)?(\d+)\s{2,}(.+)$', l)
                if not m or m.group(1) not in man['items']: continue
                path, b8 = man['items'][m.group(1)]
                txt = clean_line(m.group(2), 100)
                if not txt or cur_hash.get(path) != b8:
                    bad += 1; continue
                if rows.get(path, [None, None, None, ''])[3:4] == ['h'] and rows[path][0] == b8: continue  # human wins
                rows[path] = [b8, path, txt, 'g']; ok += 1
        elif man['phase'] == 'mods':
            got_p, got_h = {}, collections.defaultdict(list)
            for l in out.split('\n'):
                m = re.match(r'^\s*([MH])\s+(\d+)\s*[\t:|]\s*(.+)$', l)
                if not m or m.group(2) not in man['items']: continue
                if m.group(1) == 'M': got_p[m.group(2)] = clean_line(m.group(3), 110)
                else:
                    t_ = re.sub(r'^(\d{4}-\d{2})-\d{2}\b', r'\1', clean_line(m.group(3), 110))
                    if re.match(r'^\d{4}-\d{2}\b', t_): got_h[m.group(2)].append(t_)
            for k, it in man['items'].items():
                M = it['mod']
                if M not in struct: continue
                if it['p']:
                    if got_p.get(k) and struct[M] == it['struct']:
                        modp[it['path']] = (it['struct'], got_p[k]); ok += 1
                    else: bad += 1
                if it['h']:
                    lines = sorted(got_h.get(k, []), key=lambda x: x[:7])[:8] or hist.get(M, {}).get('lines', [])
                    hist[M] = {'path': it['path'], 'upto': man['head'], 'n': it['n'], 'lines': lines}; ok += 1
        elif man['phase'] == 'overview':
            md = out.strip()
            if len(md) > 200:
                open(os.path.join(ctx, 'OVERVIEW.md'), 'w', encoding='utf8').write(
                    f"<!-- generated by ctx sync from the .ctx index; src={man['src']}. Regenerate: ctx.py sync -->\n{md}\n")
                ok += 1
            else: bad += 1
        for ext in ('.md', '.json', '.out'):
            try: os.remove(base + ext)
            except OSError: pass
    open(pf, 'w', encoding='utf8').write(''.join('\t'.join(r) + '\n' for _, r in sorted(rows.items())))
    alive = {mpath for mpath, _ in mods.values()}
    open(os.path.join(ctx, 'mods.tsv'), 'w', encoding='utf8').write(
        ''.join(f"{k}\t{v[0]}\t{v[1]}\n" for k, v in sorted(modp.items()) if k in alive))
    save_hist(ctx, {M: h for M, h in hist.items() if M in struct})
    build(repo, ctx, quiet=True)
    if not quiet: print(f"ctx sync apply: accepted {ok}, rejected {bad}")
    return ok, bad

def run_batch(prompt, model, use_api):
    """-> (text, cost_usd or None, usage dict)"""
    if use_api:
        import urllib.request
        mid = {'haiku': 'claude-haiku-4-5', 'sonnet': 'claude-sonnet-4-5'}.get(model, model)
        req = urllib.request.Request('https://api.anthropic.com/v1/messages', method='POST',
            data=json.dumps({'model': mid, 'max_tokens': 8000, 'messages': [{'role': 'user', 'content': prompt}]}).encode(),
            headers={'x-api-key': os.environ['ANTHROPIC_API_KEY'], 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
        r = json.load(urllib.request.urlopen(req, timeout=600))
        return ''.join(b.get('text', '') for b in r.get('content', [])), None, r.get('usage', {})
    with tempfile.TemporaryDirectory() as tmp:  # outside the repo: no project hooks/CLAUDE.md in the nested run
        r = subprocess.run(['claude', '-p', '--model', model, '--output-format', 'json', '--tools', '', '--max-turns', '1'],
                           input=prompt, capture_output=True, text=True, cwd=tmp, timeout=900,
                           # one-shot unique prompts: thinking and the 1h cache write only add cost (~6x measured)
                           env={**os.environ, 'MAX_THINKING_TOKENS': '0', 'DISABLE_PROMPT_CACHING': '1'})
    try: j = json.loads(r.stdout[r.stdout.find('{'):])
    except ValueError: raise RuntimeError((r.stderr or r.stdout)[:300])
    if j.get('is_error'): raise RuntimeError(str(j.get('result'))[:300])
    return j.get('result', ''), j.get('total_cost_usd'), j.get('usage', {})

def sync_run(repo, ctx, model='haiku', jobs=6, max_files=0, use_api=None, rounds=6):
    import concurrent.futures as cf
    if use_api is None: use_api = bool(os.environ.get('ANTHROPIC_API_KEY')) and not shutil.which('claude')
    if not use_api and not shutil.which('claude'):
        raise SystemExit('ctx sync run: needs the `claude` CLI (logged in) or ANTHROPIC_API_KEY (use --api)')
    cost, intok, outtok = 0.0, 0, 0
    for _ in range(rounds):
        phase, batches = sync_plan(repo, ctx, max_files)
        if not batches: break
        def one(b):
            text, c, u = run_batch(open(b, encoding='utf8').read(), model, use_api)
            open(b[:-3] + '.out', 'w', encoding='utf8').write(text)
            return c, u
        with cf.ThreadPoolExecutor(jobs) as ex:
            for fut, b in [(ex.submit(one, b), b) for b in batches]:
                try:
                    c, u = fut.result(); cost += c or 0
                    intok += u.get('input_tokens', 0) + u.get('cache_creation_input_tokens', 0) + u.get('cache_read_input_tokens', 0)
                    outtok += u.get('output_tokens', 0)
                except Exception as e: print(f"  {os.path.basename(b)} failed: {str(e)[:200]}", file=sys.stderr)
        ok, bad = sync_apply(repo, ctx)
        if ok == 0: print('ctx sync run: no progress, stopping', file=sys.stderr); break
        if max_files and phase == 'files': break
    print(f"ctx sync run: done. tokens in ~{intok // 1000}k out ~{outtok // 1000}k" + (f", cost ${cost:.3f}" if cost else ''))

def sync_status(repo, ctx):
    refresh(repo, ctx)
    st = sync_pending(repo, ctx)
    n_files = sum(len(fs) for _, fs in st['mods'].values())
    print(f"ctx sync status: files needing purpose {len(st['files'])}/{n_files} | modules needing purpose {len(st['mod_p'])}"
          f" | modules needing history digest {len(st['mod_h'])} | overview {'stale' if st['overview'] else ('blocked' if (st['files'] or st['mod_p'] or st['mod_h']) else 'fresh')}")
    est = sum(min(os.path.getsize(os.path.join(repo, x[2])), FILE_CHARS) + 300 for x in st['files'] if os.path.exists(os.path.join(repo, x[2])))
    est += sum(2500 + 150 * len(v) for v in st['mod_h'].values()) + 1500 * len(st['mod_p'])
    if est: print(f"  estimated input ~{est // 3 // 1000 + 1}k tokens for files+modules (+ prompt overhead per batch)")
    return st

def sync_hint(ctx):
    """cheap pending summary for the SessionStart output (no git)"""
    try:
        mods, struct = read_mod_files(ctx)
        pur_h, _ = load_purposes(ctx); modp = load_mods(ctx)
        nf = sum(1 for _, fs in mods.values() for _, path, L, b8, _ in fs
                 if L >= 3 and b8 not in pur_h and (os.path.splitext(path)[1].lower() in SYNC_EXT or SYNC_NAMES.search(path)))
        nm = sum(1 for M in mods if modp.get(mods[M][0], ('',))[0] != struct[M])
        return (nf, nm)
    except Exception: return (0, 0)

# ---------------------------------------------------------------- Claude Code integration

CMD = 'python3 "$CLAUDE_PROJECT_DIR/.ctx/ctx.py"'
MARK_A, MARK_B = '<!-- ctx:start -->', '<!-- ctx:end -->'

CLAUDE_MD = MARK_A + """
## Repo context index (.ctx, CTX1)
A compact map of this repo is injected at session start as <repo-context>. Use it before exploring:
1. `python3 .ctx/ctx.py q "<task in plain words>"`: the relevant files with exports (x), imports (i),
   used-by (u), facts (e: env/db/routes) and history (n, last, cc = co-changed files, w = recent commits).
2. `python3 .ctx/ctx.py show <id>...` expands ids (f…= file, m…= module).
   `python3 .ctx/ctx.py db [table...]` shows the schema: columns (* = pk, → = fk), where each table is defined, and which files write to it.
3. Open source only for files you will change, or whose block doesn't answer the question.
Before editing a file, check `u:` (callers to update) and `cc:` (files that historically change with it:
migrations, tests, config). Prefer these commands over reading .ctx/* directly; they auto-refresh.
`h:` lines under a module are its history digest (milestones/decisions). `.ctx/OVERVIEW.md` is a human overview.
When you learn what a file without a `p:` line does, record it: `python3 .ctx/ctx.py setp <path> "<one line>"`.
Format: .ctx/SPEC.md
""" + MARK_B

TICKET_SKILL = """---
name: ticket
description: Turn a client change request into implementation tickets plus open questions for the client, using the .ctx repo index. Use when given a change request, feature request, or bug report to scope.
allowed-tools: Bash(python3 .ctx/ctx.py:*), Read, Grep, Glob
---
# /ticket: change request to tickets

Change request: $ARGUMENTS
(If empty, use the change request from the conversation.)

Cost is driven by the number of turns, not by output size. Aim for 3 tool calls in total.

## 1. Locate (1 call)
Chain everything into ONE Bash call: `python3 .ctx/ctx.py q "<change request in domain terms>"`, plus
`show <id>` for obvious `cc:` partners. The root in <repo-context> already lists the dependencies (`stack:`),
so the absence of a library (mail, pdf, queue…) is already answered there. Don't grep for it.

## 2. Verify (1 call)
ONE batched call: the Grep/sed of the 2-6 files that will change, only the relevant parts.
Skip it if the index blocks already answer the question.

## 3. Gap check (no tool calls) (every item gets: answered-from-code / assumption / question)
1. Data model: tables read (`e: db:`), written (`dbw:`), defined (`db+:`). The `db:` section of `q` output
   (or `ctx.py db <table>`) lists the real columns. Does the needed field exist? New columns, migration, backfill?
2. API/routes: new or changed endpoints (`e: route:`)? Breaking for existing callers (`u:`)?
3. Auth/roles: who may do this? Existing role model?
4. UI: which pages/components (`u:` into app/pages)?
5. Config/infra: new env vars, secrets, services, cron, queues (`e: env:`, `i: ext:`)?
6. Third parties: new provider or library (e.g. mail, PDF, payments)? Webhooks/events?
7. Coupling: files that always change together (`cc:`). Tests to add or update?
8. Business rules the code cannot tell you: limits, wording, edge cases, who receives what, when.
Never guess on 8. Make it a client question.

## 4. Output
### Summary
One paragraph: what changes, where, rough size (S/M/L per ticket).

### Questions for the client
Numbered, answerable in one line each, with a proposed default so the client can just say "ok".

### Tickets
For each ticket (split by deployable unit; order by dependency):
```
T<n>: <imperative title>                                  size: S|M|L   depends: T<k>
Why: <one line tied to the change request>
Touches: <path>, ...
Do:
- <concrete step>
Acceptance:
- [ ] <testable criterion>
Risks: <from u:/cc:/history, or "none known">
Assumptions: <defaults taken for open questions>
```
Put the ticket id in commit subjects later: the g-tree picks it up (`t:`) and links history to the files.
"""

SYNC_SKILL = """---
name: ctx-sync
description: Generate or refresh the LLM layer of the .ctx repo index (one-line file purposes, module summaries, history digests from git, OVERVIEW.md) with a cheap Haiku pass. Use after ctx init, when the session start reports the layer is incomplete, or when asked to sync, refresh or overview the codebase.
allowed-tools: Bash(python3 .ctx/ctx.py:*), Bash(command -v claude), Agent
---
# /ctx-sync: build the LLM layer of the repo index

You only orchestrate. Never read source files or batch prompts yourself.

1. Run `python3 .ctx/ctx.py sync status`.
   - Nothing pending: say "index LLM layer is up to date" and stop.
   - Estimated input over ~1000k tokens (about $2+ with Haiku): tell the user the estimate and ask before
     continuing, unless they already said to go ahead ($ARGUMENTS containing "yes" counts).
2. Main path (cheapest, ~3x cheaper than subagents): if `command -v claude` succeeds or ANTHROPIC_API_KEY is set, run
   `python3 .ctx/ctx.py sync run -j 8` with the Bash timeout set to 600000. With more than ~1500 files pending,
   run `python3 .ctx/ctx.py sync run -j 8 --max 1200` repeatedly until status shows no files pending, then once more
   without --max (modules + overview). If it fails, use the fallback.
3. Fallback (no CLI available, or the main path failed): repeat up to 6 rounds:
   a. `python3 .ctx/ctx.py sync plan` prints `<batch>.md  ->  <batch>.out` pairs, or "nothing pending" (go to 4).
   b. Spawn ONE subagent per batch, ALL in a single message so they run in parallel:
      subagent_type "general-purpose", model "haiku", description "ctx sync <batch name>", prompt:
      "Read the file <batch>.md with the Read tool. Follow its instructions exactly to produce the output
       (lines, or markdown for an overview batch). Write ONLY that output to <batch>.out with the Write tool.
       Reply with just: done."
   c. `python3 .ctx/ctx.py sync apply`.
4. Report in 2-3 lines: counts, cost if printed, and that `.ctx/OVERVIEW.md` has the human overview.
   These files are meant to be committed: .ctx/purposes.tsv, mods.tsv, hist.ct, OVERVIEW.md.
"""

def _merge_settings(path, strict=False):
    try: st = json.load(open(path))
    except (OSError, ValueError): st = {}
    hooks = st.setdefault('hooks', {})
    ss = hooks.setdefault('SessionStart', [])
    want = CMD + ' session'
    if not any(h.get('command') == want for e in ss for h in e.get('hooks', [])):
        ss.append({'hooks': [{'type': 'command', 'command': want, 'timeout': 120}]})
    if strict:
        pt = hooks.setdefault('PreToolUse', [])
        want_g = CMD + ' gate'
        if not any(h.get('command') == want_g for e in pt for h in e.get('hooks', [])):
            pt.append({'matcher': 'Grep|Glob|Bash', 'hooks': [{'type': 'command', 'command': want_g, 'timeout': 10}]})
    allow = st.setdefault('permissions', {}).setdefault('allow', [])
    for rule in ('Bash(python3 .ctx/ctx.py:*)', 'Read(.ctx/.sync/**)', 'Write(.ctx/.sync/**)', 'Edit(.ctx/.sync/**)'):
        if rule not in allow: allow.append(rule)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(st, open(path, 'w'), indent=2); open(path, 'a').write('\n')

def init(repo, strict=False):
    ctx = os.path.join(repo, '.ctx'); os.makedirs(ctx, exist_ok=True)
    here = os.path.dirname(os.path.abspath(__file__)); done = []
    if os.path.realpath(here) != os.path.realpath(ctx):
        shutil.copyfile(os.path.abspath(__file__), os.path.join(ctx, 'ctx.py'))
        if os.path.exists(os.path.join(here, 'SPEC.md')): shutil.copyfile(os.path.join(here, 'SPEC.md'), os.path.join(ctx, 'SPEC.md'))
        done.append('.ctx/ctx.py + SPEC.md')
    _merge_settings(os.path.join(repo, '.claude', 'settings.json'), strict)
    done.append('.claude/settings.json (SessionStart hook, permission' + (', PreToolUse gate' if strict else '') + ')')
    sk = os.path.join(repo, '.claude', 'skills', 'ticket'); os.makedirs(sk, exist_ok=True)
    open(os.path.join(sk, 'SKILL.md'), 'w').write(TICKET_SKILL); done.append('.claude/skills/ticket/SKILL.md')
    sk2 = os.path.join(repo, '.claude', 'skills', 'ctx-sync'); os.makedirs(sk2, exist_ok=True)
    open(os.path.join(sk2, 'SKILL.md'), 'w').write(SYNC_SKILL); done.append('.claude/skills/ctx-sync/SKILL.md')
    cm = os.path.join(repo, 'CLAUDE.md')
    cur = open(cm, encoding='utf8').read() if os.path.exists(cm) else ''
    if MARK_A in cur and MARK_B in cur:
        cur = cur[:cur.index(MARK_A)] + CLAUDE_MD + cur[cur.index(MARK_B) + len(MARK_B):]
    else: cur = (cur.rstrip() + '\n\n' if cur.strip() else '') + CLAUDE_MD + '\n'
    open(cm, 'w', encoding='utf8').write(cur); done.append('CLAUDE.md block')
    hp = sh(['git', 'rev-parse', '--git-path', 'hooks/pre-commit'], repo).strip()
    hp = hp if os.path.isabs(hp) else os.path.join(repo, hp)
    line = 'python3 .ctx/ctx.py build -q --tracked && git add .ctx'
    body = open(hp).read() if os.path.exists(hp) else '#!/bin/sh\n'
    if 'ctx.py build' not in body:
        os.makedirs(os.path.dirname(hp), exist_ok=True)
        open(hp, 'w').write(body.rstrip('\n') + '\n# ctx: keep the committed index in sync\n' + line + '\n')
        os.chmod(hp, 0o755); done.append('git pre-commit hook')
    ign = [p_ for p_ in ('CLAUDE.md', '.claude/settings.json', '.claude/skills/ticket/SKILL.md')
           if subprocess.run(['git', 'check-ignore', '-q', p_], cwd=repo).returncode == 0]
    if ign: print(f"ctx init: WARNING gitignored, teammates won't get them: {', '.join(ign)}", file=sys.stderr)
    build(repo, ctx)
    print('ctx init: ' + '; '.join(done), file=sys.stderr)
    print('LLM layer (purposes, module digests, OVERVIEW.md): run /ctx-sync in Claude Code, or `python3 .ctx/ctx.py sync run`', file=sys.stderr)
    print('CI (GitHub Actions), after actions/checkout:\n'
          '  pip install tree-sitter-language-pack && python3 .ctx/ctx.py check', file=sys.stderr)

def cloud_bootstrap(repo):
    """Cloud sessions (claude.ai/code): fresh VM, deps not installed, clone may be shallow."""
    if os.environ.get('CLAUDE_CODE_REMOTE') != 'true': return
    try: import tree_sitter_language_pack  # noqa
    except ImportError:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '--break-system-packages',
                        'tree-sitter-language-pack'], capture_output=True, timeout=300)
    if sh(['git', 'rev-parse', '--is-shallow-repository'], repo, check=False).strip() == 'true':
        subprocess.run(['git', 'fetch', '-q', '--deepen=2000'], cwd=repo, capture_output=True, timeout=120)

def session(repo, ctx):
    try: cloud_bootstrap(repo)
    except Exception as e: print(f"[ctx] cloud bootstrap: {str(e)[:120]}")
    try: build(repo, ctx, quiet=True)
    except (Exception, SystemExit) as e:
        print(f"[ctx] index refresh failed ({str(e)[:200]}); showing last built index.")
    rp = os.path.join(ctx, 'root.ct')
    if not os.path.exists(rp): return
    print('<repo-context spec=".ctx/SPEC.md" usage="python3 .ctx/ctx.py q \\"<task>\\" | show <id>">')
    print(open(rp, encoding='utf8').read().rstrip('\n'))
    print('</repo-context>')
    nf, nm = sync_hint(ctx)
    if nf >= 5 or nm >= 3:
        print(f"[ctx] LLM layer incomplete: {nf} files without purpose, {nm} modules without summary. "
              f"Suggest /ctx-sync to the user (cheap Haiku pass) if they work here regularly; don't run it unasked.")

GATE_MSG = ("This repo has a context index. Before searching, run: python3 .ctx/ctx.py q \"<task in plain words>\" "
            "(one call gives files, exports, callers, facts and history). Then grep/read only what it points to. "
            "This reminder shows once per session.")
SEARCH_BASH = re.compile(r'(^|[;&|(]\s*)(grep|rg|ag|find|fd|ack|git grep|tree)\b')

def gate(repo, ctx):
    """PreToolUse hook. Deny the FIRST search-like call of a session if ctx wasn't used yet. Never blocks twice."""
    try: ev = json.load(sys.stdin)
    except ValueError: return
    sid = re.sub(r'[^\w-]', '', str(ev.get('session_id', 'x')))[:80] or 'x'
    tool, inp = ev.get('tool_name', ''), ev.get('tool_input') or {}
    d = os.path.join(ctx, '.sess'); os.makedirs(d, exist_ok=True)
    mark = os.path.join(d, sid)
    if os.path.exists(mark): return
    cmd = inp.get('command', '') if tool == 'Bash' else ''
    if 'ctx.py' in cmd:
        open(mark, 'w').write('used'); return
    if tool in ('Grep', 'Glob') or (tool == 'Bash' and SEARCH_BASH.search(cmd)):
        open(mark, 'w').write('nudged')
        for fn in os.listdir(d):  # prune old session marks
            fp = os.path.join(d, fn)
            if os.path.getmtime(fp) < os.path.getmtime(mark) - 7 * 86400: os.remove(fp)
        print(GATE_MSG, file=sys.stderr); sys.exit(2)

def refresh(repo, ctx):
    try: build(repo, ctx, quiet=True)
    except (Exception, SystemExit) as e: print(f"[ctx] refresh failed: {str(e)[:200]}", file=sys.stderr)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', default=None)
    sp = ap.add_subparsers(dest='cmd', required=True)
    b = sp.add_parser('build'); b.add_argument('-q', action='store_true'); b.add_argument('--tracked', action='store_true')
    sp.add_parser('check'); sp.add_parser('session'); sp.add_parser('gate')
    dbp_ = sp.add_parser('db'); dbp_.add_argument('tables', nargs='*')
    it = sp.add_parser('init'); it.add_argument('--strict', action='store_true', help='add PreToolUse gate')
    q = sp.add_parser('q'); q.add_argument('text'); q.add_argument('-b', type=int, default=3000)
    s = sp.add_parser('show'); s.add_argument('ids', nargs='+')
    t = sp.add_parser('todo'); t.add_argument('-n', type=int, default=50)
    p = sp.add_parser('setp'); p.add_argument('path'); p.add_argument('text')
    sy = sp.add_parser('sync', help='LLM layer: purposes, module digests, OVERVIEW.md')
    sy.add_argument('action', nargs='?', default='status', choices=['status', 'plan', 'apply', 'run'])
    sy.add_argument('--max', type=int, default=0, help='max files per run (files phase)')
    sy.add_argument('-j', type=int, default=6); sy.add_argument('--model', default='haiku')
    sy.add_argument('--api', action='store_true', help='use ANTHROPIC_API_KEY instead of the claude CLI')
    it.add_argument('--sync', action='store_true', help='also run `sync run` (LLM layer) after init')
    a = ap.parse_args()
    repo = a.repo or find_repo()
    ctx = os.path.join(repo, '.ctx')

    if a.cmd in ('q', 'show', 'todo', 'db'): refresh(repo, ctx)
    if a.cmd == 'build': build(repo, ctx, a.q, tracked=a.tracked)
    elif a.cmd == 'init':
        init(repo, a.strict)
        if a.sync: sync_run(repo, ctx)
    elif a.cmd == 'sync':
        if a.action == 'status': sync_status(repo, ctx)
        elif a.action == 'plan': sync_plan(repo, ctx, a.max)
        elif a.action == 'apply': sync_apply(repo, ctx)
        else: sync_run(repo, ctx, a.model, a.j, a.max, True if a.api else None)
    elif a.cmd == 'gate': gate(repo, ctx)
    elif a.cmd == 'db':
        db = load_db(ctx)
        sel = [t_ for t_ in db if not a.tables or any(norm_keys(t_) & norm_keys(x) for x in a.tables)]
        print('\n'.join(db[t_] for t_ in sel) if sel else '(no tables' + (' matching' if a.tables else '') + ')')
    elif a.cmd == 'session': session(repo, ctx)
    elif a.cmd == 'check':
        with tempfile.TemporaryDirectory() as tmp:
            for fn in ('purposes.tsv', 'mods.tsv'):
                if os.path.exists(os.path.join(ctx, fn)): shutil.copy(os.path.join(ctx, fn), tmp)
            build(repo, tmp, quiet=True, tracked=True)
            r = subprocess.run(['diff', '-rq', '-x', '.cache.json', '-x', '.git.cache', '-x', 'g', '-x', 'SPEC.md', '-x', 'ctx.py',
                                '-x', 'hist.ct', '-x', 'OVERVIEW.md', '-x', '.sync', '-x', '.sess', ctx, tmp],
                               capture_output=True, text=True)
            if r.returncode: print('ctx: stale\n' + r.stdout, file=sys.stderr); sys.exit(1)
            print('ctx: fresh', file=sys.stderr)
    elif a.cmd == 'q': query(ctx, repo, a.text, a.b)
    elif a.cmd == 'show':
        _, rootb, n, g, _, _ = load_index(ctx)
        hist = load_hist(ctx)
        for i in a.ids: print('\n'.join((rootb.get(i) or n.get(i, [f'@{i} ?'])) + ['  h: ' + l for l in hist.get(i, {}).get('lines', [])] + g.get(i, [])))
    elif a.cmd == 'todo':
        _, _, n, _, _, _ = load_index(ctx)
        k = 0
        for i, blk in sorted(n.items(), key=lambda t: t[1][0].split(' ')[1]):
            if not any(l.startswith('p: ') for l in blk):
                h = re.search(r'h=(\w+)', blk[0]).group(1)
                print(f"{h}\t{blk[0].split(' ')[1]}"); k += 1
                if k >= a.n: break
    elif a.cmd == 'setp':
        path = os.path.relpath(os.path.abspath(a.path), repo) if os.path.exists(a.path) else a.path
        h = blob_hash(open(os.path.join(repo, path), 'rb').read())[:8]
        pf = os.path.join(ctx, 'purposes.tsv')
        rows = [l for l in (open(pf).read().splitlines() if os.path.exists(pf) else []) if l.split('\t')[1:2] != [path]]
        rows.append(f"{h}\t{path}\t{squash(a.text, 100)}\th")
        open(pf, 'w').write('\n'.join(sorted(rows, key=lambda r: r.split('\t')[1])) + '\n')

if __name__ == '__main__':
    main()
