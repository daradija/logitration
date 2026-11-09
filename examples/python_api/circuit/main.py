# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""The electrical-circuit agent of circuit-a2a, rebuilt on logitration. Run: python main.py [simulated|claude-cli|anthropic-api]"""
import sys
from pathlib import Path

from logitration import LogicAgent

here = Path(__file__).parent
agent = LogicAgent(rules=here / "circuit.pl", phrases=here / "phrases.json", name="circuit", domain="electrical circuits",
                   translator=sys.argv[1] if len(sys.argv) > 1 else "simulated")
agent.serve(port=8923)
