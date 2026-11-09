# program installed (observe) v1, anchored 2026-11-07 15:25 by `logi program` (claude-sonnet-5-5, attempt 1)
# reason: modelos.logi:31 needs a script (observe) for «{X} is installed on {M}»
# via: ollama   door: world.run / world.machines   imports: re, json
def _models(world, machine):
    try:
        r = world.run(machine, "ollama", "list")
    except RuntimeError:
        return []
    if r.rc != 0:
        return []
    names = []
    for line in r.out.splitlines():
        line = line.strip()
        if not line:
            continue
        tok = line.split()[0]
        if tok.upper() == "NAME":
            continue
        if tok.endswith(":latest"):
            tok = tok[:-len(":latest")]
        if tok not in names:
            names.append(tok)
    return names


def installed(world, M=None, X=None):
    """Return the list of ALL tuples (M, X) of str that are true now and agree with the arguments that are not None."""
    if M is not None:
        machines = [M] if M in world.machines() else []
    else:
        machines = list(world.machines())
    want = X
    if want is not None and want.endswith(":latest"):
        want = want[:-len(":latest")]
    result = []
    for m in machines:
        for name in _models(world, m):
            if want is None or name == want:
                t = (m, name if X is None else X)
                if t not in result:
                    result.append(t)
    return result
