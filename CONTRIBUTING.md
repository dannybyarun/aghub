# Contributing to aghub

aghub is an evolving implementation of a coordination protocol, not a framework
with a stable extension surface. The board format, the CLI, and the hub API
change when the protocol needs them to. Small, tested, focused changes are the
easiest to review.

## Development environment

Requires Python 3.10+ and `git`. The only runtime dependency is
`PyYAML>=6.0`:

```bash
git clone <your-fork-url> aghub
cd aghub
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .

aghub --help
```

`bin/aghub` puts `src` on `PYTHONPATH` and runs `python3 -m aghub`. If you
prefer not to use the wrapper, the same thing works directly:

```bash
PYTHONPATH=src python3 -m aghub --help
```

### Windows

`bin/aghub` is a bash script, so it does not run from `cmd.exe` or PowerShell.
Use the module entry point instead:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:PYTHONPATH = "src"
python -m aghub --help
```

Eval and merge-check commands run through `cmd.exe` on Windows. The Bubblewrap
sandbox backend is POSIX-only; on Windows the fallback runs commands through
`cmd.exe` and skips the POSIX resource limits. See "Known limits" in the README
before changing sandbox behavior.

The demo scripts are also bash. Run them under WSL, Git Bash, or another POSIX
shell on Windows.

## Tests

Tests use the standard library `unittest`. There is no pytest dependency.

```bash
python3 -m unittest discover -s tests -v
```

Run one module while iterating:

```bash
python3 -m unittest tests.test_board -v
python3 -m unittest tests.test_review_policy -v
```

The Postgres integration tests are skipped unless `AGHUB_TEST_POSTGRES_DSN`
points at a disposable database and `psycopg` is installed:

```bash
python3 -m pip install 'psycopg[binary]'
AGHUB_TEST_POSTGRES_DSN='postgresql://postgres@127.0.0.1:5432/aghub_test' \
  python3 -m unittest tests.test_postgres -v
```

They use uniquely named projects and delete their own rows, but point them at a
scratch database anyway. Do not run them against a database you care about.

Tests should create their own temporary repositories and board worktrees. Never
assume the developer's current checkout is disposable.

## Demos

The demos exercise the protocol end to end. They need `git` and a POSIX shell.

```bash
./demo/run_demo.sh    # two agents, contracts, handoffs, queue, reviews, ACLs
./demo/run_hub.sh     # leases, presence, and the web board
./demo/run_swarm.sh   # concurrent board writers through one serial queue
```

Notes:

- `run_demo.sh` creates a temporary origin and three clones under `/tmp` and
  leaves them in place for inspection.
- `run_hub.sh` leaves the hub running so the printed URL stays reachable. Use
  `./demo/run_hub.sh --stop` to stop it at the end.
- `run_swarm.sh` defaults to 10 agents. `SWARM_AGENTS=20`, `SWARM_HUB=0`, and
  `SWARM_LOCK_WAIT=<seconds>` change the run. It is a load test, so expect it to
  take minutes at higher counts.

## What to contribute

Welcome without a prior issue:

- Bug fixes with a failing test that reproduces the bug.
- Test coverage for existing behavior, especially edge cases in the merge queue,
  review policy, signature verification, and codegen.
- Documentation fixes, including corrections to "Known limits" when the README
  no longer matches the code.
- Codegen improvements inside the documented OpenAPI subset.
- Windows parity fixes, as long as they do not weaken the POSIX path.

Open an issue before writing code for:

- Changes to the board layout or event format, or to anything stored on
  `aghub/board`.
- Changes to the advisory-at-write, authoritative-at-merge split between claims,
  the hub, and the merge queue.
- New runtime dependencies, new persistence backends, or changes to optional
  dependency boundaries.
- Changes to authentication, ACL roles, key handling, review policy, or the
  GitHub bridge trust model.
- Renaming or removing CLI commands, flags, environment variables, or JSON
  fields. Existing scripts and CI workflows depend on them.
- Large refactors that touch more than one subsystem.

If you are unsure, open a short issue describing the change and the failure it
fixes. That is cheaper than a large PR that has to be redirected.

## Coding conventions

These are the conventions the repository actually uses.

- **Standard library first.** `PyYAML` is the only runtime dependency. `psycopg`
  is optional and isolated to the Postgres backend. Do not add a dependency
  without discussing it in an issue first.
- **`unittest`, not pytest.** Put tests in `tests/`, use `tempfile` for
  scratch state, and prefer a real temporary git repo over mocks when the
  behavior involves git, the board, or the queue.
- **Keep the hub and the board separate.** Git on `aghub/board` is the durable
  record. The hub is a live authority for leases, presence, snapshots, quotas,
  and the board writer lease. Do not move tasks, contracts, handoffs, or merges
  into the hub. An unreachable hub must degrade to git-only operation, not block
  the board.
- **Respect the two-phase model.** Claims and path leases are advisory at write
  time and enforced again by the merge queue. Do not make a write-time check
  look authoritative when it cannot be. With a hub, live leases and the writer
  lease are authoritative for their stated scope; fencing tokens must reject
  stale writers.
- **Board transactions must be replayable.** A rejected board push replays the
  whole transaction, so each mutation has to be a pure function of the state it
  is handed. The same rule applies to `Board.batch` and `Agent.add_tasks`.
- **Keep old boards readable.** New verification should not silently
  invalidate legacy unsigned records. Report them separately, and keep
  `verify --strict` as the opt-in failure mode.
- **Determinism matters.** Codegen output for the same frozen `name@version`
  must be byte-identical. Sort unordered input before emitting it.
- **Errors are part of the interface.** Keep nonzero exit codes, keep `--json`
  output machine-readable, and keep failure traces attached to tasks.
- **Update the README when behavior changes.** At minimum, update "Known
  limits" for any new or narrowed limitation, and the Commands table for any new
  or changed flag. If a change makes a documented limit obsolete, remove the
  limit instead of leaving a stale warning.

## Commits and pull requests

- Keep commits small and focused. One logical change per commit, with an
  imperative subject such as `queue: reject stale approvals before merge`.
- Explain *why* in the commit body when the diff does not make it obvious.
- Add or update tests for behavior changes. A bug fix should include the case
  that used to fail.
- Run the full suite before opening a pull request:
  `python3 -m unittest discover -s tests -v`.
- Link the issue in the pull request description.
- Never commit secrets, tokens, private keys, `.env` files, or hub token maps.
  The PEM files under `tests/fixtures/github/` are generated test material and
  are documented as such; do not add real keys next to them.
- Do not commit generated demo directories, hub databases, `__pycache__`, or
  local `.git/aghub.local.json` state.

## Good first issues

Look for issues labeled `good first issue`. Useful starting points that do not
require holding the whole protocol in your head:

- A README or docstring correction where the text no longer matches the code.
- A missing assertion in an existing `tests/test_*.py` module.
- A codegen case inside the documented subset that generates invalid output.
- A Windows path or shell-invocation bug in the eval and merge-check fallback.
- An error message that does not say which command to run next.

If an issue looks stale or under-specified, ask in the issue before starting.
