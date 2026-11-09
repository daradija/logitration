% Events of the OOP run (written by `python3 circuit.py` from circuit.py).
event(1, part(l1, lamp)).
event(2, ok(l1)).
event(3, part(l2, lamp)).
event(4, ok(l2)).
event(5, part(i1, switch)).
event(6, down(i1)).
event(7, part(i2, switch)).
event(8, up(i2)).
event(9, part(i3, switch)).
event(10, up(i3)).
event(11, part(cc1, fuse)).
event(12, ok(cc1)).
event(13, part(cc2, fuse)).
event(14, ok(cc2)).
event(15, part(c0, wire)).
event(16, part(c1, wire)).
event(17, part(c2, wire)).
event(18, part(c3, wire)).
event(19, part(c4, wire)).
event(20, part(c5, wire)).
event(21, part(c6, wire)).
event(22, part(e1, socket)).
event(23, part(e2, socket)).
event(24, part(source, source)).
event(25, connects(l1, c0, true)).
event(26, connects(c0, c1, up(i2))).
event(27, connects(c0, c2, down(i2))).
event(28, connects(c1, c3, up(i1))).
event(29, connects(c2, c3, down(i1))).
event(30, connects(l2, c4, true)).
event(31, connects(c4, c3, up(i3))).
event(32, connects(e1, c3, true)).
event(33, connects(c3, c5, ok(cc1))).
event(34, connects(e2, c6, true)).
event(35, connects(c6, c5, ok(cc2))).
event(36, connects(c5, source, true)).
event(37, up(i1)).
event(38, blown(cc1)).
event(39, ok(cc1)).
event(40, down(i2)).
event(41, down(i1)).
event(42, blown(l2)).
