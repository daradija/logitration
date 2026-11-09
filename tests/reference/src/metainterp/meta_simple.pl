% Simple meta-interpreter (lecture 6, p. 11).
:- ensure_loaded(operators).

% prove(+G) holds if the goal G can be proved from the object KB
prove(true).
prove((A & B)) :-
    prove(A),
    prove(B).
prove(A) :-
    (A <- B),
    prove(B).
