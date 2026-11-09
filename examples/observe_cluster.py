#!/usr/bin/env python3
# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""observe_cluster.py: the agent runs this to LOOK at the cluster. Read-only: it prints one fact per line, in the words of
cluster.logi ("n1 has 16 GB of ram"). The client never tells the agent how the cluster is.

  python3 observe_cluster.py            the nodes and the waiting jobs of cluster_state.txt (always the same: for the example)
  python3 observe_cluster.py --local    the same, plus this very machine as node `here` (real free disk and installed ram)
"""
import os
import shutil
import sys
from pathlib import Path

GB = 1024 ** 3


def local():
    ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // GB
    disk = shutil.disk_usage("/").free // GB
    return [("node", "here", ram, disk)]


def main(argv):
    rows = []
    for line in (Path(__file__).with_name("cluster_state.txt")).read_text().splitlines():
        p = line.split()
        if len(p) == 4 and p[0] in ("node", "job"):
            rows.append((p[0], p[1], None if p[2] == "-" else int(p[2]), int(p[3])))
    if "--local" in argv:
        rows += local()
    for kind, name, ram, disk in rows:
        if kind == "node":
            print(f"{name} is a node\n{name} has {ram} GB of ram\n{name} has {disk} GB of disk")
        else:
            print(f"{name} is a job" + (f"\n{name} needs {ram} GB of ram" if ram is not None else "") + f"\n{name} needs {disk} GB of disk")


if __name__ == "__main__":
    main(sys.argv[1:])
