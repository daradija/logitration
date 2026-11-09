% Meta-interpreter with a proof tree (lecture 6, p. 35).
:- ensure_loaded(operators).

% prove_with_tree(+G,?T) holds if T is a proof tree of the goal G
prove_with_tree(true,true).
prove_with_tree((A & B),(TA & TB)) :-
    prove_with_tree(A,TA),
    prove_with_tree(B,TB).
prove_with_tree(G,proof(G,TB)) :-
    (G <- B),
    prove_with_tree(B,TB).
