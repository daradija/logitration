% A desk lamp is on when it has power, its switch is on and its bulb is not broken.
on(L) :- lamp(L), powered(L), switch(L, on), \+ bulb(L, broken).

powered(L) :- plugged_into(L, S), \+ power(S, off).

% before loading, ask for what is missing
missing(switch(L, _)) :- lamp(L), \+ switch(L, _).
missing(plugged_into(L, _)) :- lamp(L), \+ plugged_into(L, _).
