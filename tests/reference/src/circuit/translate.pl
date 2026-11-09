% From the events of the OOP run (events.pl, from circuit.py) to the object KB of lecture 6, at a step N.
% Each fact of the KB remembers the event that put it there (from_event/2): the link between the logic and what ran.
:- ensure_loaded('../metainterp/operators').
:- dynamic (<-)/2, from_event/2, event/2.   % the events: events.pl (circuit.py) or live (server.pl)
:- ensure_loaded(laws).

% aspect(+T, ?Part, ?Aspect): event T sets one aspect of a part; the latest event of that aspect rules
aspect(up(I), I, position).
aspect(down(I), I, position).
aspect(ok(X), X, health).
aspect(blown(X), X, health).

% holds(+N, ?T, ?E): after event N, T is still true (put by E and not changed since)
holds(N, T, E) :-
    event(E, T), E =< N,
    aspect(T, P, K),
    \+ (event(E2, T2), E2 > E, E2 =< N, aspect(T2, P, K)).

% translate(+N): the object KB after event N
translate(N) :-
    retractall((_ <- _)), retractall(from_event(_, _)),
    forall((event(E, part(L, lamp)), E =< N), record(lamp(L), true, E)),
    forall((event(E, part(X, source)), E =< N), record(has_current(X), true, E)),
    forall((holds(N, T, E), T \= blown(_)), record(T, true, E)),
    forall((event(E, connects(D1, D2, C)), E =< N), record(connected(D1, D2), C, E)),
    forall(law(H, B), assertz((H <- B))).

record(H, B, E) :- assertz((H <- B)), assertz(from_event(H, E)).
