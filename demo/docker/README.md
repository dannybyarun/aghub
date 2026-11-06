# Docker demo: three agents, one board

Each agent runs in its own container and its own clone. They never message each
other. They claim disjoint lanes on one `aghub` board, commit, submit, and a
separate container drains the serial merge queue into trunk.

```bash
cd demo/docker
docker compose up --build
```

Then open the board at <http://localhost:8788>. The hub stays up after the merge
so you can look at it; stop everything with:

```bash
docker compose down -v
```

## What to watch

- `seed` creates the bare origin and three lanes: `lanes/a/**`, `lanes/b/**`,
  `lanes/c/**`.
- `agent-a`, `agent-b`, `agent-c` each claim one lane. A claim on an overlapping
  path is rejected; these three do not overlap.
- `merger` runs `aghub queue run` once all three have submitted. The project
  check (`test -f README.md`) runs on every merge.
- `docker compose logs merger` should end with all three lane files on
  `origin/main`.

The hub is the live authority for claims and the writer lease. Git remains the
durable record: the board lives on the `aghub/board` orphan branch and the merge
history lives on `main`. Stop the hub and the record survives.

To use a different host port:

```bash
AGHUB_DEMO_PORT=9000 docker compose up --build
```
