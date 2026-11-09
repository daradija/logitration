% The live logic: the OOP program sends its events and questions over standard input.
%   event(N, T).        -> records the event; replies ok
%   ask(Template, Goal).  -> all the answers, on one line
%   tell(Text, Goal).     -> each Text on its own line, then "end"
:- ensure_loaded(queries).
:- ensure_loaded(explain).

serve :-
    repeat,
    read_term(user_input, T, []),
    (   T == end_of_file -> halt
    ;   reply(T), flush_output, fail ).

reply(event(N, X)) :- assertz(event(N, X)), writeln(ok).
reply(ask(R, G)) :-
    catch(findall(R, G, Rs), E, Rs = error(E)),
    numbervars(Rs, 0, _), print(Rs), nl.

reply(tell(T, G)) :-
    catch(forall(G, (write(T), nl)), E, (print(error(E)), nl)),
    writeln(end).

:- initialization(serve, main).
