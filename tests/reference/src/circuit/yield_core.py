"""The core of prolog.py in a few lines: unification with a trail, and resolution with yield.

Terms: an atom is a string; a structure, a tuple (name, arg1, arg2, ...); a variable, a Var.
A body is a list of goals. No reader, no cut, no arithmetic: just the two ingredients.
    python3 yield_core.py
"""
# ---- core: from here to "end of core"
class Var:
    def __init__(self, name): self.name, self.ref = name, None

trail = []                                   # the bound variables, in order

def deref(t):
    while isinstance(t, Var) and t.ref is not None: t = t.ref
    return t

def undo(mark):                              # backtrack: release what was bound since the mark
    while len(trail) > mark: trail.pop().ref = None

def unify(a, b):                             # make two terms equal by binding variables
    a, b = deref(a), deref(b)
    if a is b: return True
    if isinstance(a, Var): a.ref = b; trail.append(a); return True
    if isinstance(b, Var): b.ref = a; trail.append(b); return True
    if isinstance(a, tuple) and isinstance(b, tuple) and len(a) == len(b):
        return all(unify(x, y) for x, y in zip(a, b))
    return a == b

def rename(t, vs):                           # each use of a clause, with fresh variables
    if isinstance(t, Var): return vs.setdefault(t, Var(t.name))
    if isinstance(t, tuple): return tuple(rename(x, vs) for x in t)
    if isinstance(t, list): return [rename(x, vs) for x in t]
    return t

def solve(goals, kb):                        # a generator: each yield is a solution
    if not goals: yield; return
    first, rest = goals[0], goals[1:]
    for head, body in kb:                    # the loop is the choice point
        mark, vs = len(trail), {}
        if unify(rename(head, vs), first):
            yield from solve(rename(body, vs) + rest, kb)
        undo(mark)                           # asking for another solution = undo and try the next one
# ---- end of core

def show(t):
    t = deref(t)
    if isinstance(t, Var): return '_' + t.name
    if isinstance(t, tuple): return t[0] + '(' + ','.join(show(x) for x in t[1:]) + ')'
    return t

if __name__ == '__main__':
    D, D1, L = Var('D'), Var('D1'), Var('L')
    fact = lambda *t: (t, [])
    # the circuit after the assembly (event 36), the facts that translate.pl gives; the law, that of laws.pl
    KB = [fact('lamp', 'l1'), fact('lamp', 'l2'), fact('ok', 'l1'), fact('ok', 'l2'),
          fact('down', 'i1'), fact('up', 'i2'), fact('up', 'i3'), fact('ok', 'cc1'),
          (('connected', 'l1', 'c0'), []), (('connected', 'c0', 'c1'), [('up', 'i2')]),
          (('connected', 'c0', 'c2'), [('down', 'i2')]), (('connected', 'c1', 'c3'), [('up', 'i1')]),
          (('connected', 'c2', 'c3'), [('down', 'i1')]), (('connected', 'l2', 'c4'), []),
          (('connected', 'c4', 'c3'), [('up', 'i3')]), (('connected', 'e1', 'c3'), []),
          (('connected', 'c3', 'c5'), [('ok', 'cc1')]),
          (('connected', 'c5', 'source'), []), fact('has_current', 'source'),
          (('has_current', D), [('connected', D, D1), ('has_current', D1)]),
          (('is_lit', L), [('lamp', L), ('ok', L), ('has_current', L)])]
    X = Var('X')
    print('?- is_lit(X).')
    solutions = solve([('is_lit', X)], KB)
    for n, _ in enumerate(solutions, 1):      # each turn of the for asks for the next one: it backtracks
        print(f'  solution {n}: X = {show(X)}   (trail: {len(trail)} bindings)')
    print(f'  no more. Trail after undoing: {len(trail)} bindings; X = {show(X)}')
    print('?- connected(Y, c3).')
    Y = Var('Y')
    for _ in solve([('connected', Y, 'c3')], KB):
        print(f'  Y = {show(Y)}')
    print('  (c1 does not appear: it asked for up(i1), fails and backtracks)')
    core = open(__file__).read().split('# ---- core')[1].split('# ---- end of core')[0]
    print('lines of the core:', sum(1 for s in core.splitlines()[1:] if s.strip()))
