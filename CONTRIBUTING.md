# Contributing to logitration

## Run the tests
```
python3 -m venv .venv && . .venv/bin/activate && pip install .
for t in test_library test_ideas test_design test_program test_types; do python tests/$t.py || break; done
```
No network, no model, no dependencies. The five suites must end with 0 failures.

## License of your contribution (CLA)
Logitration is offered under the Business Source License 1.1 ([LICENSE](LICENSE)) and under commercial licenses. To be able to do both,
the Licensor (David Ragel) needs the right to license every contribution under any terms. The agreement, in short
([CLA_DRAFT.txt](CLA_DRAFT.txt) has the full text):

* you keep the copyright of your contribution;
* you give the Licensor a perpetual, worldwide, non-exclusive, royalty-free, irrevocable license to use, copy, modify, publish, distribute
  and sublicense it under any terms, including commercial licenses and the BSL with its Change License (AGPL 3);
* you give a patent license for patents your contribution necessarily infringes;
* you promise the work is yours (or you may submit it) and that your employer, if it has rights in it, allows it;
* the Licensor may accept or reject any contribution; no payment is owed.

A pull request is accepted only after the CLA is signed. How to sign: not defined yet (write to contact@logitration.com).

## Style
Standard library only. Python 3.9+. Keep every change covered by one of the five suites.
