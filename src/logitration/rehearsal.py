# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""Worlds a programmed script can act on. A script never sees a machine: it sees a `world` with two doors,

    world.run(machine, "ollama", "list")   -> Result(.rc, .out, .err)     one command on one machine
    world.machines()                       -> ["m1", "m2", ...]

FakeWorld is the REHEARSAL world: pretend machines with pretend ollama / brew / systemctl / ss / cat / df, in memory, to try a
script before it touches anything (a mock, not the real tools: the formats copy the real ones, the behaviour is simplified).
SshWorld is the real one (`ssh HOST -- command`), never used unless you ask for it. Door wraps either and only lets the
commands the program declared (`via`) go through.
"""
import copy
import shlex
import subprocess


class WorldError(RuntimeError):
    pass


class Result:
    def __init__(self, rc=0, out="", err=""):
        self.rc, self.out, self.err = rc, out, err

    def __repr__(self):
        return f"Result(rc={self.rc}, out={self.out[:40]!r}, err={self.err[:40]!r})"


OLLAMA_KNOWN = {"llama3", "mistral", "qwen2", "phi3", "gemma2", "llama3.1", "codellama"}
BREW_KNOWN = {"jq", "wget", "htop", "tree", "ripgrep", "git"}
SERVICES_KNOWN = {"nginx", "redis", "cron", "postgres", "ollama"}


class FakeWorld:
    """machines = {"m1": {"ollama": [names], "brew": [names], "files": {path: text}, "ports": [ints],
                          "services": {name: True|False}, "disk_gb": int, "vm_stat": free memory in MB}}"""

    def __init__(self, machines):
        self.state = copy.deepcopy(machines)
        for m in self.state.values():
            for k, v in (("ollama", []), ("brew", []), ("files", {}), ("ports", []), ("services", {}), ("disk_gb", 100)):
                m.setdefault(k, copy.deepcopy(v))
            if isinstance(m.get("vm_stat"), list):                  # `rehearse n1 vm_stat: 16000`: the free memory, in MB
                m["vm_stat"] = int(m["vm_stat"][0]) if m["vm_stat"] else 0
            m.setdefault("vm_stat", 8192)
        self.calls = []

    def copy(self):
        return FakeWorld(self.state)

    def machines(self):
        return sorted(self.state)

    # ---- the oracle (the scripts cannot reach it: they only get run/machines through the Door)
    def items(self, machine, tool):
        return set(self.state[machine][tool])

    # ---- the door
    def run(self, machine, *argv):
        if machine not in self.state:
            raise WorldError(f"unknown machine {machine!r} (known: {', '.join(self.machines())})")
        if not argv or not all(isinstance(a, str) for a in argv):
            raise WorldError("run(machine, command, *args): every part must be a text")
        self.calls.append((machine, argv))
        fn = getattr(self, "_cmd_" + argv[0].replace("-", "_"), None)
        if fn is None:
            return Result(127, "", f"sh: {argv[0]}: command not found")
        return fn(self.state[machine], list(argv[1:]))

    def _cmd_ollama(self, m, a):
        tool = m["ollama"]
        if a[:1] == ["list"]:
            rows = ["NAME             ID              SIZE      MODIFIED"]
            rows += [f"{n + ':latest':<16} {abs(hash(n)) % 10**12:012d}    4.7 GB    2 days ago" for n in tool]
            return Result(0, "\n".join(rows) + "\n")
        if a[:1] in (["pull"], ["rm"], ["show"]) and len(a) == 2:
            name = a[1].split(":")[0] if a[1].endswith(":latest") else a[1]
            if a[0] == "pull":
                if name not in OLLAMA_KNOWN:
                    return Result(1, "", "Error: pull model manifest: file does not exist")
                if name not in tool:
                    tool.append(name)
                return Result(0, "pulling manifest\npulling 6a0746a1ec1a... 100%\nverifying sha256 digest\nwriting manifest\nsuccess\n")
            if name not in tool:
                return Result(1, "", f"Error: model '{a[1]}' not found")
            if a[0] == "rm":
                tool.remove(name)
                return Result(0, f"deleted '{a[1]}'\n")
            return Result(0, f"  Model\n    architecture    llama\n    name            {name}\n")
        return Result(1, "", "Error: unknown command or arguments")

    def _cmd_brew(self, m, a):
        tool = m["brew"]
        if a[:1] == ["list"]:
            names = [x for x in a[1:] if not x.startswith("-")]          # `brew list [--formula] NAME`: exit 1 when NAME is not installed (as the real one)
            if names:
                if names[0] not in tool:
                    return Result(1, "", f"Error: No such keg: /opt/homebrew/Cellar/{names[0]}")
                return Result(0, f"/opt/homebrew/Cellar/{names[0]}/1.0/bin/{names[0]}\n")
            return Result(0, "".join(n + "\n" for n in sorted(tool)))
        if a[:1] in (["install"], ["uninstall"]) and len(a) == 2:
            if a[0] == "install":
                if a[1] not in BREW_KNOWN:
                    return Result(1, "", f'Error: No available formula with the name "{a[1]}".')
                if a[1] not in tool:
                    tool.append(a[1])
                return Result(0, f"==> Fetching {a[1]}\n==> Pouring {a[1]}\n")
            if a[1] not in tool:
                return Result(1, "", f"Error: No such keg: /opt/homebrew/Cellar/{a[1]}")
            tool.remove(a[1])
            return Result(0, f"Uninstalling /opt/homebrew/Cellar/{a[1]}...\n")
        return Result(1, "", "Error: Unknown command")

    def _cmd_cat(self, m, a):
        if len(a) != 1:
            return Result(1, "", "usage: cat file")
        if a[0] not in m["files"]:
            return Result(1, "", f"cat: {a[0]}: No such file or directory")
        return Result(0, m["files"][a[0]])

    def _cmd_ss(self, m, a):
        rows = ["State    Recv-Q   Send-Q     Local Address:Port      Peer Address:Port  Process"]
        for p in m["ports"]:
            rows.append(f"LISTEN   0        128              0.0.0.0:{p:<10}     0.0.0.0:*")
            rows.append(f"LISTEN   0        128                 [::]:{p:<10}        [::]:*")
        return Result(0, "\n".join(rows) + "\n")

    def _cmd_systemctl(self, m, a):
        sv = m["services"]
        if a[:1] == ["list-units"]:
            rows = [f"{n}.service loaded active running {n} daemon" for n in sorted(sv) if sv[n]]
            return Result(0, "\n".join(rows) + "\n")
        if len(a) == 2 and a[0] in ("is-active", "start", "stop"):
            name = a[1][:-8] if a[1].endswith(".service") else a[1]
            if name not in sv:
                return Result(5 if a[0] != "is-active" else 4, "inactive\n" if a[0] == "is-active" else "", f"Unit {name}.service not found.")
            if a[0] == "is-active":
                return Result(0 if sv[name] else 3, "active\n" if sv[name] else "inactive\n")
            sv[name] = a[0] == "start"
            return Result(0)
        return Result(1, "", "Unknown command")

    def _cmd_vm_stat(self, m, a):
        """macOS: the free memory in pages of 16 KB (a small part of it 'speculative'), as the real command prints it"""
        pages = m["vm_stat"] * 1024 * 1024 // 16384
        return Result(0, "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                         f"Pages free:                               {pages - 100}.\n"
                         "Pages active:                             250000.\n"
                         f"Pages speculative:                        100.\n")

    def _cmd_df(self, m, a):
        free = m["disk_gb"] * 1024 * 1024
        return Result(0, "Filesystem   1K-blocks      Used Available Use% Mounted on\n"
                         f"/dev/sda1    {free * 2}  {free}  {free}  50% /\n")


class Door:
    """what a script receives as `world`: run and machines, and only the commands the program declared"""

    def __init__(self, world, allowed):
        self._world, self._allowed = world, frozenset(allowed)

    def run(self, machine, *argv):
        if not argv or argv[0] not in self._allowed:
            raise PermissionError(f"command {argv[0] if argv else None!r} is not declared for this program "
                                  f"(allowed: {', '.join(sorted(self._allowed))})")
        return self._world.run(machine, *argv)

    def machines(self):
        return list(self._world.machines())


class SshWorld:
    """the real machines: `ssh HOST -- command`. BatchMode (no password prompts), a timeout, nothing else. Not used unless asked for."""

    def __init__(self, hosts, timeout=30):
        self.hosts, self.timeout = list(hosts), timeout

    def machines(self):
        return list(self.hosts)

    def run(self, machine, *argv):
        if machine not in self.hosts:
            raise WorldError(f"unknown machine {machine!r}")
        try:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", machine, "--", " ".join(shlex.quote(a) for a in argv)],
                               capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise WorldError(f"{type(e).__name__}: {str(e)[:100]}")
        return Result(r.returncode, r.stdout, r.stderr)
