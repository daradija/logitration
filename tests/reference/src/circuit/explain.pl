% The answers of queries.pl, said in plain English (for the program's diary: the corpus).

ev(E-X, T) :- format(atom(T), 'event ~w (~w)', [E, X]).
event_list(Es, S) :- maplist(ev, Es, Ts), atomic_list_concat(Ts, ', ', S).

% change(+N1, +N2, +L, -T): why L changed between N1 and N2
change(N1, N2, L, T) :- why(N1, N2, L, turned_on(Es)), event_list(Es, S), format(atom(T), '~w turned on because of ~w.', [L, S]).
change(N1, N2, L, T) :- why(N1, N2, L, turned_off(Es)), event_list(Es, S), format(atom(T), '~w turned off because of ~w.', [L, S]).

% unlit(+N, +L, -T): why L is not lit
unlit(N, L, T) :-
    why_not(N, L, fails(P, Cs)),
    maplist(missing, Cs, Ts), atomic_list_concat(Ts, '; ', S),
    (   P = has_current(_) -> format(atom(T), '~w is unlit because no current reaches it: ~w.', [L, S])
    ;   format(atom(T), '~w is unlit: ~w.', [L, S]) ).
missing(H-(E-X), T) :- !, format(atom(T), 'it would need ~w, but event ~w left ~w', [H, E, X]).
missing(H-no_event, T) :- format(atom(T), 'it would need ~w', [H]).

% holds_up(+N, +L, -T): which events hold L up as lit
holds_up(N, L, T) :- supporting(N, L, Es), length(Es, K), event_list(Es, S), format(atom(T), '~w is lit; ~w events hold it up: ~w.', [L, K, S]).
