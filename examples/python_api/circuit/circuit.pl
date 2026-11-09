% An electrical circuit: lamps, switches, plugs, fuses and wires.
% The client states the parts, what feeds what, and what happens; the rules decide which lamps are lit.

lit(L) :- lamp(L), \+ state(L, blown), current(L).

current(X) :- source(X).
current(X) :- feeds(X, Y), current(Y).
current(X) :- feeds(X, Y, P, V), in(P, V), current(Y).

in(P, ok) :- \+ state(P, blown).
in(P, V) :- V \== ok, state(P, V).

part(X) :- source(X) ; lamp(X) ; fuse(X) ; switch(X) ; plug(X) ; wire(X) ; socket(X).

% what to ask the client before loading the description
missing(state(S, _)) :- (switch(S) ; plug(S)), \+ state(S, _).
missing(feeds(L, _)) :- lamp(L), \+ feeds(L, _), \+ feeds(L, _, _, _).
missing(T) :- (feeds(A, B) ; feeds(A, B, _, _) ; feeds(_, _, A, _), B = A), member(X, [A, B]), \+ part(X),
              format(atom(T), 'What is ~w? It is mentioned but not described.', [X]).
