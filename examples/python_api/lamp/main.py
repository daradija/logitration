# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
from logitration import LogicAgent

phrases = {
    "on(L)":              "{L} is on",
    "lamp(L)":            "{L} is a lamp",
    "plugged_into(L, S)": {"say": "{L} is plugged into {S}", "ask": "Where is {L} plugged in?"},
    "switch(L, V)":       {"say": ["the switch of {L} is {V}", "turn {L} {V}"], "changes": True,
                           "ask": "Is the switch of {L} on or off?", "values": {"V": ["on", "off"]}},
    "bulb(L, V)":         {"say": ["the bulb of {L} is {V}", "the bulb of {L} breaks{V=broken}"], "changes": True},
    "power(S, V)":        {"say": ["the power at {S} is {V}", "the power at {S} goes {V}"], "changes": True},
}
agent = LogicAgent(rules="lamp.pl", phrases=phrases, domain="desk lamps")
print(agent.say("desk is a lamp. desk is plugged into wall. the switch of desk is off. then turn desk on"))
print(agent.say("yes"))
print(agent.say("why is desk on?"))
