# CTX1 — Repository Context Protocol

A committed, machine-oriented index of a repository, so an LLM agent can load
**only the parts of a codebase that matter for a question** instead of reading source.
Two layers share one id space:

- **c-tree**: structure. repo → module → file → symbol, plus edges.
- **g-tree**: history. Git facts attached to the same file ids. Derived from `.git`
  on each machine, never committed (a commit cannot contain its own history).

Design rules:

1. **Tokens, not bytes.** Plain ASCII, short keys, real identifiers. No binary, no
   base64, no invented symbols; they tokenize badly and the model reads them worse.
2. **Lazy by level.** `root.ct` is always read. Everything else is read on demand.
   An agent should never need to read the whole `.ctx/`.
3. **Deterministic.** Same source in, same bytes out. No timestamps. This keeps diffs
   small and makes `check` possible.
4. **Cache-stable root.** `root.ct` changes only when structure changes (modules, exports,
   deps, facts), never on a body edit or a new commit. It can sit in a cached prompt prefix.
5. **Hash-addressed.** Every file carries its git blob hash, so a stale index is
   detectable and rebuilds are incremental.

## Layout

```
.ctx/                                                           committed?
  SPEC.md          this file                                    yes
  ctx.py           builder/reader (self-contained tool)         yes
  root.ct          repo header + one entry per module           yes   [always loaded]
  idx.tsv          code term -> file ids                        yes   [search]
  n/<mod>.ct       c-tree: file blocks of one module            yes   [on demand]
  db.ct            schema: one line per table                   yes   [on demand, and inside q output]
  purposes.tsv     file purposes keyed by blob hash             yes   (LLM layer)
  mods.tsv         module purposes keyed by structure hash      yes   (LLM layer)
  hist.ct          per-module history digest, upto=<sha>        yes   (LLM layer) [in q/show as h:]
  OVERVIEW.md      human overview of the repo                   yes   (LLM layer)
  .sync/           sync batches in flight                       no
  g/<mod>.gt       g-tree: history blocks of one module         no    [on demand]
  g/root.gt        per-module churn: @<mod> c=<n>/<n90d> last=  no
  g/idx.tsv        history term (ticket refs) -> file ids       no
  .cache.json      parse + stat cache                           no
  .git.cache       git log cached by HEAD                       no
```

## Ids

- `m` + 5 hex chars: module (sha1 of module path)
- `f` + 6 hex chars: file (sha1 of file path)

Ids are path-derived. A file keeps its id across edits; a rename gives it a new id.

## Modules

A module is a directory. Directories with fewer than `max(4, files/40)` files (counted
recursively) merge into their parent, so a repo has about 40 modules at most and `root.ct` stays
around 1–2k tokens whatever the repo size. `.` is the repo root module.

## Line grammar

```
<block>  := "@" <id> SP <fields...> NL { <line> NL }
<line>   := <key> ":" SP <value>
```

A block starts at a line beginning with `@` and ends at the next `@` or EOF.
Unknown keys must be ignored (forward compatible). Lists are space-separated.
Values are truncated with `…`.

### root.ct

```
CTX1 repo=<name> files=<n> mods=<n> [tables=<n>]
stack: <dependency names>
lang: <ext>:<count> ...
@<mod> <path> f=<files>
  p: <purpose>
  k: <top exported names>
  d: <module ids this module imports>   u: <count of modules importing it>
  e: <facts summary>
```

No counters that move on ordinary edits or commits (line counts, commit counts, dates) belong
in root. They go to `n/` (L=, h=) or `g/root.gt`.

### n/<mod>.ct (c-tree)

```
@<mod> mod <path> h=<hash8>
@<file> <path> L=<lines> h=<blob8>
p: <purpose>             from purposes.tsv, else doc comment. "p~:" = stale (file changed since written)
x: <symbol signature>    one per exported symbol (non-exported ones are omitted)
i: <file ids> | ext:<pkgs>   imports (internal resolved | external packages)
u: <file ids> [+N]       used-by: who imports this file (capped at 8)
e: <facts>               env:A,B  db+:t (defines)  db:t (reads)  dbw:t (writes)  route:METHOD /path
```

A table that a file both reads and writes is listed under `dbw:` only.

Symbol forms: `name(params): ret`, `class Name{publicMethods}`, `name=callee()`,
`iface Name`, `type Name`, `enum Name`, `{a,b} from ./x` (re-export), `default …`.

### g/<mod>.gt (g-tree)

```
@<file> n=<commits> last=<YYYY-MM-DD> by=<author>:<n>,...
cc: <file>:<ratio> ...   co-change: share of this file's commits that also touched <file>
t: <ticket refs>         #123, ABC-45 found in commit subjects
w: <date> <subject>      last 3 commit subjects: the "why"
```

Co-change is the most useful signal for ticket writing. It exposes coupling that
imports don't show, such as a schema file and the migration that always ships with it.

### db.ct (schema)

```
# table(col* = pk, col→table = fk) <defined-in  w: writer file ids
<table>(<col>[*][→<table>] ...) <<path> [w:<file ids>[+N]]
```

Sources, merged into the final schema:

| Source | What is read |
|---|---|
| Drizzle `pgTable/mysqlTable/sqliteTable` | column db names, `.primaryKey()`, `.references(() => x.id)` |
| Prisma `model` | scalar fields (`@map`), `@id`/`@@id`, `@@map`, relation `fields:` → FK. Virtual relation fields are skipped |
| SQL `.sql` files | `CREATE TABLE` (inline and table-level PK/FK), then `ALTER TABLE` add/drop/rename column, add FK, rename table, `DROP TABLE`, `RENAME TABLE`, applied in path order (migration order) |
| SQLAlchemy, SQLModel (`table=True`), Django | `Column`/`mapped_column`/`Field`/`*Field`, `primary_key=True`, `ForeignKey` (Django FK becomes `<name>_id`) |

ORM definitions override SQL migrations for the same table. Schema prefixes (`public.`) are dropped.

Usage (reads/writes) comes from Drizzle `db.select().from(t)`/`insert(t)`/`update(t)`/`delete(t)`/joins/`db.query.t.find*`,
Prisma-style `x.model.findMany/create/…`, Supabase `.from('t').select/insert/…`, Django `Model.objects.*`,
SQLAlchemy `query(Model)`/`select(Model)`, and SQL inside string literals (`FROM/JOIN` = read,
`INSERT INTO/UPDATE/DELETE FROM` = write). A name counts only if it resolves to a known table
(case, underscores and plural insensitive, plus ORM model/variable aliases), which removes false positives.

`q` appends a `db:` section: tables named by the request (table or column names), tables the
matched files read or write, and one foreign-key hop from those. That's what makes absence
answerable ("`user` has no email column"). `ctx.py db [table...]` prints the same lines.

### idx.tsv

`<term>\t<id>,<id>,...` Terms are lowercased and crudely stemmed. They come from path
segments, exported and imported symbol names split on camelCase and snake_case, env names,
tables and routes. Ticket refs from history live in `g/idx.tsv`; readers merge both.

### LLM layer (`ctx.py sync`)

The builder itself is deterministic and never calls a model. Meaning is added by `sync`, a separate
pass whose output is committed (it costs money, so teammates and CI shouldn't pay to regenerate it):

```
purposes.tsv   <blob8>\t<path>\t<purpose>\t<g|h>        g = generated, h = human (setp); human wins on the same hash
mods.tsv       <module path>\t<struct8>\t<purpose>       struct8 = hash of the module's paths + export/fact lines
hist.ct        @<mod> <path> upto=<sha12> n=<commits>    then lines: YYYY-MM <milestone/decision>, at most 8
OVERVIEW.md    <!-- ... src=<hash8> -->  markdown for humans
```

Phases run in order, because each one consumes the previous one:

| Phase | Pending when | Input to the model |
|---|---|---|
| files | code file (≥3 lines) with no purpose for its current blob hash | compacted source (imports stripped, ≤4k chars), `x:`/`e:` lines, old purpose as a hint |
| mods | struct8 changed (purpose), or ≥3 new commits since `upto` (digest) | file purposes, existing digest, new commits (subject + body). Commits spanning >3 modules count as repo-wide and go to `.` only; above 150 commits the low-signal ones are dropped |
| overview | src hash changed: module purposes, or a module's newest history month | root.ct, hist.ct, db.ct, churn |

Staleness: a changed file shows `p~:` (old purpose) until the next sync. Root shows module purposes
from `mods.tsv`, which only change with structure, so root stays cache-stable through syncs that
only add file purposes or digest lines. If `upto` is no longer in history (rebase), that module's
digest is rebuilt.

Protocol (any runner works): `sync plan` writes self-contained prompts to `.sync/<phase>-NNN.md` plus a
manifest. A runner writes `<phase>-NNN.out`. `sync apply` validates every line against the manifest and
the *current* blob hashes (lines for files changed meanwhile are rejected), merges, and rebuilds.

Runners:
- `sync run [-j 8] [--max N] [--model haiku] [--api]`: headless. Uses the `claude` CLI (subscription,
  run outside the repo, with thinking and prompt caching off: about 6x cheaper per batch, measured) or
  the Messages API with `ANTHROPIC_API_KEY`.
- `/ctx-sync` skill: runs `sync run`, and falls back to Haiku subagents (plan → parallel Agents → apply)
  when no CLI is available. The subagent path costs about 2–3x more.
- `init --sync`: init, then `sync run`.

Measured with Haiku:

| Repo | Files | Full sync | Incremental (3 commits, 1 file) |
|---|---|---|---|
| saas-starter | 51 | $0.13, ~1 min | $0.03 |
| umami | 1,652 | ~$2.3 extrapolated ($3.40 before compaction), ~2.5 min at -j 8 | — |

## Agent reading procedure

1. Read `root.ct`. It orients you in about 1–2k tokens for a mid-size repo.
2. Map the request to terms. Run `ctx.py q "<request>"` (auto-refreshes the index first).
3. Read only the matching file blocks from `n/`, plus their `g/` blocks (`ctx.py show <id>`).
4. Follow `u:` (impact), `cc:` (hidden coupling) and `i:` (dependencies) one hop.
5. Open real source **only** for the few files whose blocks don't answer the question.

## Change request → ticket gap check

For each affected node, answer each point from the index. Anything left unanswered
becomes a question to the client or an explicit assumption on the ticket:

| # | Check | Index source |
|---|---|---|
| 1 | Data model change? Field exists? | `db:` section of `q` / `ctx.py db`, `e: db+: db: dbw:` |
| 2 | API or route contract change? | `e: route:` |
| 3 | Auth, roles, permissions? | auth module `u:` |
| 4 | UI surfaces affected? | `u:` into app/pages |
| 5 | Migration / backfill? | `cc:` with migration files |
| 6 | Config, env, infra? | `e: env:` |
| 7 | Third-party side effects? | `i: ext:` |
| 8 | Tests to add or change? | `cc:` with test files |
| 9 | Business rules not in code? | nothing in index → **ask client** |

Every ticket lists the file ids it touches. Future commits referencing that ticket
then appear in `t:`, which closes the loop between tickets and history.

## Keeping it fresh

| When | What | Cost |
|---|---|---|
| Any read (`q`, `show`, `todo`, `session`) | incremental refresh of the working tree | ~0.1–0.2s: stat cache, parse cache, git log cached by HEAD |
| `git commit` | pre-commit: `build -q --tracked && git add .ctx` | same |
| CI | `ctx.py check`: rebuild tracked files, diff committed parts, exit 1 if stale | full parse |

`check` ignores `g/`, so a shallow CI checkout is fine.

## Claude Code integration (`ctx.py init`)

`init` copies `ctx.py` and `SPEC.md` into `.ctx/` and installs:

- `.claude/settings.json`: a `SessionStart` hook running `ctx.py session`. It refreshes the
  index and prints `root.ct` inside `<repo-context>`, which Claude Code adds to context.
  It also adds the permission `Bash(python3 .ctx/ctx.py:*)`, so lookups need no prompt, and
  Read/Write/Edit on `.ctx/.sync/**` for the subagent fallback.
- `CLAUDE.md`: a short block between `<!-- ctx:start -->` and `<!-- ctx:end -->` with the reading
  order and the rule to check `u:` and `cc:` before editing. Re-running init replaces the block.
- `.claude/skills/ticket/SKILL.md`: `/ticket <change request>` → gap check → tickets and client questions.
- `.claude/skills/ctx-sync/SKILL.md`: `/ctx-sync` fills or refreshes the LLM layer. `session` prints a one-line
  hint after `<repo-context>` when ≥5 files or ≥3 modules lack it. The hint sits after the stable root, so it
  doesn't break the cached prefix.
- The git pre-commit hook.

Options and environments:

- `init --strict` adds a `PreToolUse` hook (`ctx.py gate`, matcher `Grep|Glob|Bash`). The first search-like
  call of a session (Grep, Glob, or grep/rg/find in Bash) is blocked once with a message to run `ctx.py q`,
  unless ctx was already used. It never blocks twice. Session marks live in `.ctx/.sess/` (gitignored).
- Cloud sessions (claude.ai/code, `claude --cloud`) read the repo's `CLAUDE.md`, `.claude/skills/` and
  `.claude/settings.json` hooks in single-repo sessions. When `CLAUDE_CODE_REMOTE=true`, `session` installs
  `tree-sitter-language-pack` from PyPI if it's missing and deepens a shallow clone (up to 2000 commits),
  so the g-tree has history. Multi-repo cloud sessions (projects) don't load repo hooks. Put
  `pip install tree-sitter-language-pack` in the environment's setup script there, which is cached.

No MCP server is used: tool schemas would cost context on every turn, while a hook, a skill and
bash cost nothing until they are used.
