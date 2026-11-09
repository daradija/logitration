"""The electrical circuit of lecture 6 (Poole-98 p. 16), in object-oriented programming.

Each part is an object; the current is computed by calling the objects. What happens (assembling a part,
connecting, moving a switch, blowing or repairing) is reported to a Recorder, anchored in the constructor of the
Circuit: it is the aspect that turns the run into events, each one a Prolog term.
"""


class Recorder:
    """The aspect: notes each event, numbered, as a Prolog term."""
    def __init__(self):
        self.events = []

    def event(self, term):
        self.events.append(term)
        return len(self.events)

    def prolog(self):
        return "".join(f"event({n}, {t}).\n" for n, t in enumerate(self.events, 1))


class Part:
    """Something in the circuit through which current may (or may not) pass."""
    kind = "wire"

    def __init__(self, circuit, name):
        self.circuit = circuit
        self.name = name
        self.inputs = []            # [(part the current comes from, condition)]
        circuit.notify(f"part({name}, {self.kind})")

    def has_current(self):
        return any(condition.holds() and origin.has_current() for origin, condition in self.inputs)


class Source(Part):
    """The mains input: it always has current."""
    kind = "source"

    def has_current(self):
        return True


class Socket(Part):
    kind = "socket"


class Breakable(Part):
    """A part that can be ok or blown."""
    def __init__(self, circuit, name):
        super().__init__(circuit, name)
        self.ok = True
        circuit.notify(f"ok({name})")

    def blow(self):
        self.ok = False
        self.circuit.notify(f"blown({self.name})")

    def repair(self):
        self.ok = True
        self.circuit.notify(f"ok({self.name})")


class Fuse(Breakable):
    kind = "fuse"


class Lamp(Breakable):
    kind = "lamp"

    def is_lit(self):
        return self.ok and self.has_current()


class Switch:
    """It carries no current: it decides which connections hold. Position up or down."""
    def __init__(self, circuit, name, position):
        self.circuit = circuit
        self.name = name
        circuit.notify(f"part({name}, switch)")
        self.move(position)

    def move(self, position):
        self.position = position
        self.circuit.notify(f"{position}({self.name})")

    def up(self):
        self.move("up")

    def down(self):
        self.move("down")


# conditions of a connection: each knows whether it holds and how it is written in logic
class Always:
    def holds(self):
        return True

    def term(self):
        return "true"


class AtPosition:
    def __init__(self, switch, position):
        self.switch, self.position = switch, position

    def holds(self):
        return self.switch.position == self.position

    def term(self):
        return f"{self.position}({self.switch.name})"


class IsOk:
    def __init__(self, part):
        self.part = part

    def holds(self):
        return self.part.ok

    def term(self):
        return f"ok({self.part.name})"


class Circuit:
    def __init__(self, recorder):
        self.recorder = recorder
        self.parts = {}

    def notify(self, term):
        return self.recorder.event(term)

    def place(self, cls, name, *args):
        self.parts[name] = cls(self, name, *args)
        return self.parts[name]

    def connect(self, to, origin, condition=None):
        """Current flows from `origin` to `to` if the condition holds."""
        condition = condition or Always()
        self.parts[to].inputs.append((self.parts[origin], condition))
        self.notify(f"connects({to}, {origin}, {condition.term()})")

    def lit(self):
        return [n for n, p in self.parts.items() if isinstance(p, Lamp) and p.is_lit()]


def assembly(recorder):
    """The circuit of p. 16, in the lecture's state: i1 down, i2 and i3 up, everything ok."""
    c = Circuit(recorder)
    l1, l2 = c.place(Lamp, "l1"), c.place(Lamp, "l2")
    i1, i2, i3 = c.place(Switch, "i1", "down"), c.place(Switch, "i2", "up"), c.place(Switch, "i3", "up")
    cc1, cc2 = c.place(Fuse, "cc1"), c.place(Fuse, "cc2")
    for n in ("c0", "c1", "c2", "c3", "c4", "c5", "c6"):
        c.place(Part, n)
    c.place(Socket, "e1"), c.place(Socket, "e2"), c.place(Source, "source")
    c.connect("l1", "c0")
    c.connect("c0", "c1", AtPosition(i2, "up"))
    c.connect("c0", "c2", AtPosition(i2, "down"))
    c.connect("c1", "c3", AtPosition(i1, "up"))
    c.connect("c2", "c3", AtPosition(i1, "down"))
    c.connect("l2", "c4")
    c.connect("c4", "c3", AtPosition(i3, "up"))
    c.connect("e1", "c3")
    c.connect("c3", "c5", IsOk(cc1))
    c.connect("e2", "c6")
    c.connect("c6", "c5", IsOk(cc2))
    c.connect("c5", "source")
    return c


# what is done with the assembled circuit: (what is told, the call)
SCRIPT = [
    ("I raise i1", lambda c: c.parts["i1"].up()),
    ("cc1 blows", lambda c: c.parts["cc1"].blow()),
    ("I replace cc1", lambda c: c.parts["cc1"].repair()),
    ("I lower i2", lambda c: c.parts["i2"].down()),
    ("I lower i1", lambda c: c.parts["i1"].down()),
    ("l2 blows", lambda c: c.parts["l2"].blow()),
]


def run():
    """Assemble, follow the script and return the recorder and, per step, (event, what happened, lit lamps)."""
    recorder = Recorder()
    c = assembly(recorder)
    steps = [(len(recorder.events), "assembly", c.lit())]
    for text, call in SCRIPT:
        call(c)
        steps.append((len(recorder.events), text, c.lit()))
    return recorder, steps


if __name__ == "__main__":
    recorder, steps = run()
    print(recorder.prolog(), end="")
    for n, text, lamps in steps:
        print(f"% after event {n} ({text}): lit {lamps}")
