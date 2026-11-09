"""The program asks itself the questions we used to ask Prolog.

A second aspect, Reflection, is anchored in the constructor of the Circuit instead of the Recorder: it notes the events
the same way, and also sends them to the live logic (server.pl). After the assembly and after each step of the script,
the program asks itself: which lamps are lit? (and checks it against what its objects say), why did each lamp change?,
why is the one that does not shine unlit?, what holds up the one that shines? The answers, in plain English, go to its diary.
If the objects and the logic disagree, it asks which part of which law failed and points at the method.

Usage: python3 reflection.py            (the good circuit -> diary.txt)
       python3 reflection.py careless    (l2 is a CarelessLamp, with a bug -> diary_careless.txt)
       ... own                           (with our own interpreter, prolog.py, instead of SWI-Prolog; same diary)
"""
import subprocess
import sys
from pathlib import Path

import circuit
from circuit import Lamp, Recorder, assembly, SCRIPT

HERE = Path(__file__).resolve().parent
# which method of which class each law comes from (laws.pl)
METHOD = {"is_lit": "is_lit", "has_current": "has_current"}


ENGINES = {"swipl": ["swipl", "-q", "server.pl"],                       # SWI-Prolog
           "own": [sys.executable, "prolog.py", "server.pl"]}            # our own interpreter (prolog.py)


class Logic:
    """The live logic: server.pl in a Prolog (SWI or our own), which receives events and questions."""
    def __init__(self, engine="swipl"):
        self.p = subprocess.Popen(ENGINES[engine], cwd=HERE, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, bufsize=1)

    def send(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def event(self, n, term):
        self.send(f"event({n}, {term}).")
        assert self.p.stdout.readline().strip() == "ok"

    def ask(self, template, goal):
        self.send(f"ask({template}, {goal}).")
        return self.p.stdout.readline().strip()

    def tell(self, goal):
        self.send(f"tell(T, {goal}).")
        lines = []
        while (line := self.p.stdout.readline().rstrip("\n")) != "end":
            lines.append(line)
        return lines

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


class Reflection(Recorder):
    """The aspect that thinks: it records like the Recorder and also asks the logic about what happens."""
    def __init__(self, logic):
        super().__init__()
        self.logic = logic
        self.diary = []
        self.before = None      # (event, lamps) of the last step thought about

    def event(self, term):
        n = super().event(term)
        self.logic.event(n, term)
        return n

    def say(self, text):
        self.diary.append(text)

    def think(self, what, circuit):
        n = len(self.events)
        self.say(f"\n== After '{what}' (event {n})")
        mine = circuit.lit()
        logic = self.logic.ask("L", f"(lit({n}, Ls), member(L, Ls))")
        logic = [] if logic == "[]" else logic.strip("[]").split(",")
        self.say(f"Which lamps are lit? My objects: {mine or 'none'}. The logic: {logic or 'none'}.")
        for l in sorted(set(mine) ^ set(logic)):
            self.say(f"MISMATCH on {l}: my objects say {'lit' if l in mine else 'unlit'} "
                     f"and the logic says {'lit' if l in logic else 'unlit'}.")
            for t in self.logic.tell(f"unlit({n}, {l}, T)"):
                self.say(f"  The logic explains it: {t}")
            part = circuit.parts[l]
            self.say(f"  So the is_lit method of {type(part).__name__} does not obey the law "
                     f"is_lit(L) <- lamp(L) & ok(L) & has_current(L): it needs reviewing.")
        if self.before:
            n0, lamps0 = self.before
            for l in sorted(set(lamps0) ^ set(logic)):
                for t in self.logic.tell(f"change({n0}, {n}, {l}, T)"):
                    self.say(f"Why did {l} change? {t}")
        if not self.before:                     # after the assembly: for each lamp, why it shines or why not
            for l in ("l1", "l2"):
                for t in self.logic.tell(f"{'holds_up' if l in logic else 'unlit'}({n}, {l}, T)"):
                    self.say(f"{'What holds up ' + l if l in logic else 'Why is ' + l + ' unlit'}? {t}")
        self.before = (n, logic)


class CarelessLamp(Lamp):
    """A lamp with a bug: it forgets to check whether it is ok."""
    def is_lit(self):
        return self.has_current()


def run(careless=False, engine="swipl"):
    logic = Logic(engine)
    reflection = Reflection(logic)
    if careless:
        original = circuit.Lamp
        circuit.Lamp = CarelessLamp            # lamps l1 and l2 are assembled with the buggy class
    try:
        c = assembly(reflection)
    finally:
        if careless:
            circuit.Lamp = original
    reflection.think("assembly", c)
    for text, call in SCRIPT:
        call(c)
        reflection.think(text, c)
    logic.close()
    return reflection.diary


if __name__ == "__main__":
    careless = "careless" in sys.argv[1:]
    diary = run(careless, "own" if "own" in sys.argv[1:] else "swipl")
    name = "diary_careless.txt" if careless else "diary.txt"
    (HERE / name).write_text("\n".join(diary).lstrip() + "\n")
    print("\n".join(diary))
