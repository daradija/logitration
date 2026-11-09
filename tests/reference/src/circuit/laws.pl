% The classes of circuit.py, transpiled to rules: what does not change with the events.
:- ensure_loaded('../metainterp/operators').

% Part.has_current: some input whose condition holds and whose origin has current
law(has_current(D), connected(D, D1) & has_current(D1)).
% Lamp.is_lit: it is a lamp, it is ok and it has current
law(is_lit(L), lamp(L) & ok(L) & has_current(L)).
