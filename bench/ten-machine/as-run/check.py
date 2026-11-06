"""Merge check for the multi-machine swarm trial.

Every candidate merge is compiled before it reaches trunk. This is deliberately
a real check: a merge that cannot be imported is rejected by the queue.
"""

import pathlib
import py_compile
import sys

root = pathlib.Path(__file__).resolve().parent
files = sorted(root.rglob("*.py"))
if not files:
    print("swarm check: no modules found")
    sys.exit(1)

bad = []
for path in files:
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        bad.append(f"{path.relative_to(root)}: {exc.msg}")

if bad:
    print("swarm check failed:")
    for item in bad:
        print(f"  {item}")
    sys.exit(1)

print(f"swarm check ok: {len(files)} modules compile")
