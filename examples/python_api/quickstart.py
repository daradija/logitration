# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
from logitration import LogicAgent

phrases = {
    "on(L)":        "{L} is on",
    "lamp(L)":      "{L} is a lamp",
    "switch(L, V)": {"say": ["the switch of {L} is {V}", "turn {L} {V}"], "changes": True},
}
rules = "on(L) :- lamp(L), switch(L, on)."

agent = LogicAgent(rules=rules, phrases=phrases)
print(agent.say("desk is a lamp. the switch of desk is off. then turn desk on"))
print(agent.say("yes"))
print(agent.say("why is desk on?"))
url = agent.serve(port=8931, block=False)          # now it is an A2A agent
print(LogicAgent.connect(url).say("is desk on?"))  # ... that anyone can talk to
