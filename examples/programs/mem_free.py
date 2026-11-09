# program mem_free (observe) v1, anchored 2026-11-08 16:24 by `logi program` (claude-cli sonnet (generated 08-11 by logitration.programmer, 1 attempt; copied from logitration/clorm/programas/mem_free.py), attempt 1)
# reason: memoria.logi:15 needs a script (observe) for «{N} has {MB} MB free»
# via: vm_stat   door: world.run / world.machines   imports: re, json
import re
import json


def _free_mb(world, machine):
    try:
        r = world.run(machine, "vm_stat")
    except RuntimeError:
        return None
    if r.rc != 0:
        return None
    text = r.out
    m = re.search(r"page size of\s+(\d+)\s+bytes", text)
    if not m:
        return None
    page = int(m.group(1))
    f = re.search(r"^Pages free:\s*(\d+)", text, re.M)
    s = re.search(r"^Pages speculative:\s*(\d+)", text, re.M)
    if not f or not s:
        return None
    pages = int(f.group(1)) + int(s.group(1))
    return (pages * page) // (1024 * 1024)


def mem_free(world, N=None, MB=None):
    """Return the list of ALL tuples (N, MB) of str that are true now and agree with the arguments that are not None. Read-only."""
    result = []
    try:
        names = list(world.machines())
    except Exception:
        return []
    if N is not None:
        names = [n for n in names if n == str(N)]
    for name in names:
        mb = _free_mb(world, name)
        if mb is None:
            continue
        mb_s = str(mb)
        if MB is not None and str(MB) != mb_s:
            continue
        t = (str(name), mb_s)
        if t not in result:
            result.append(t)
    return result
