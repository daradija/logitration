# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""What the agent says: a str (print it) with .state and .data."""


class Reply(str):
    """What the agent says: a str (print it) with .state ('completed' | 'input-required' | 'rejected' | 'failed')
    and .data (the logic's result, for programs)."""

    def __new__(cls, text, state="completed", data=None):
        r = super().__new__(cls, text)
        r.text, r.state, r.data = text, state, data or {}
        return r

    @property
    def needs_input(self):
        return self.state == "input-required"

