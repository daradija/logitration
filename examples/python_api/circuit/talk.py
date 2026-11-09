# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""Talk to a running agent over A2A: python talk.py "which lamps are lit?" [session]"""
import sys

from logitration import LogicAgent

agent = LogicAgent.connect("http://127.0.0.1:8923/")
reply = agent.say(sys.argv[1], session=sys.argv[2] if len(sys.argv) > 2 else "demo")
print(reply, f"\n[{reply.state}]")
