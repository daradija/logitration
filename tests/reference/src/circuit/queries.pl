% Questions to the running program, with the lecture-6 meta-interpreters (untouched) over the translated KB.
:- ensure_loaded(translate).
:- ensure_loaded('../metainterp/meta_simple').
:- ensure_loaded('../metainterp/meta_with_tree').

% lit(+N, -Ls): the lamps that are lit after event N
lit(N, Ls) :-
    translate(N),
    findall(L, prove(is_lit(L)), L0), sort(L0, Ls).

% how(+N, +L, -A): the proof tree that L is lit
how(N, L, A) :-
    translate(N),
    prove_with_tree(is_lit(L), A), !.

% supports(+A, -Es): the tree nodes that an event put there, with their event (E-Fact)
supports(A, Es) :- findall(E-X, (node(A, X), from_event(X, E)), E0), sort(E0, Es).
node(proof(X, _), X).
node(proof(_, B), X) :- node(B, X).
node((A & _), X) :- node(A, X).
node((_ & B), X) :- node(B, X).

% supporting(+N, +L, -Es): which events support L being lit after N
supporting(N, L, Es) :- how(N, L, A), supports(A, Es).

% why(+N1, +N2, +L, -R): why L changed between N1 and N2
%   turned_on(Es): the supports of the proof at N2 that did not exist at N1, with their event
%   turned_off(Es): the supports of the proof at N1 that no longer hold at N2, with the event that changed them
why(N1, N2, L, turned_on(Es)) :-
    \+ how(N1, L, _), supporting(N2, L, All),
    findall(E-X, (member(E-X, All), E > N1), Es).
why(N1, N2, L, turned_off(Es)) :-
    supporting(N1, L, Before), \+ how(N2, L, _),
    findall(E-T, (member(_-X, Before), aspect(X, P, K),
                  event(E, T), E > N1, E =< N2, aspect(T, P, K)), E0),
    sort(E0, Es).

% why_not(+N, +L, -F): why L is NOT lit after N. F = fails(Part, Causes): the first part of the law
%   that cannot be proved and, for each missing fact, the event that left the part that way (Fact-(E-Term)).
%   If the failing part is having current, the causes are the cuts on the way to the source.
why_not(N, L, fails(P, Causes)) :-
    translate(N), \+ prove(is_lit(L)),
    law(is_lit(L), Body), part_of(Body, P), \+ prove(P), !,
    (   P = has_current(X) -> findall(C, cut(X, C), Cs0), sort(Cs0, Missing)
    ;   Missing = [P] ),
    findall(H-Cause, (member(H, Missing), cause(N, H, Cause)), Causes).
part_of((A & B), P) :- !, ( P = A ; part_of(B, P) ).
part_of(P, P).
% cut(+X, -C): a condition that does not hold on some path from X to the source
cut(X, C) :-
    (connected(X, Y) <- Cond),
    (   \+ prove(Cond) -> C = Cond
    ;   \+ prove(has_current(Y)), cut(Y, C) ).
% cause(+N, +H, -E-T): the last event, up to N, that fixed the aspect of the part in H (H is not true because of it)
cause(N, H, E-T) :-
    aspect(H, X, K), event(E, T), E =< N, aspect(T, X, K),
    \+ (event(E2, T2), E2 > E, E2 =< N, aspect(T2, X, K)), !.
cause(_, _, no_event).
