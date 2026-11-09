"""Choosing the translator: one shared assistant for every command that needs an LLM (server, owner_claude).

  * No flags, no A2A_TRANSLATOR, in a terminal -> it detects what is available, asks a short question, tests the choice, and goes on.
  * No flags, no terminal (nohup, CI, pipe)  -> it picks the best detected backend (claude-cli, anthropic-api, simulated), says so, and goes on.
  * Flags or environment                      -> it uses exactly what you said (flag > env > assistant), tests it quickly (--no-test skips), and
                                                 exits with a clear error if it is not available. It never falls back silently.

The API key is only ever checked for PRESENCE. It is never printed, logged or written to disk (a key pasted into the menu lives in this
process's memory only, read with getpass: no echo).
"""
import getpass
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from circuit_a2a import translator as T

BACKENDS = ("claude-cli", "anthropic-api", "simulated")      # also the order of preference
TEST_TIMEOUT = 60
CLI_URL = "https://code.claude.com/docs/en/setup"
KEY_URL = "https://console.anthropic.com/"


class WizardError(SystemExit):
    """a clear message and a non-zero exit"""


# ------------------------------------------------------------------ detection
def find_claude(env=None):
    env = os.environ if env is None else env
    exe = shutil.which("claude", path=env.get("PATH", ""))
    if not exe:
        alt = Path(env.get("HOME") or Path.home()) / ".local" / "bin" / "claude"
        exe = str(alt) if alt.is_file() and os.access(alt, os.X_OK) else None
    return exe


def detect(env=None):
    """-> {"cli": {"found": bool, "path": str|None, "version": str|None}, "api": bool}. Only the PRESENCE of the key is read."""
    env = os.environ if env is None else env
    exe = find_claude(env)
    version = None
    if exe:
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15, env=dict(env))
            if r.returncode == 0 and r.stdout.strip():
                version = r.stdout.strip().splitlines()[0][:60]
        except (OSError, subprocess.SubprocessError):
            version = None
    return {"cli": {"found": bool(exe and version), "path": exe, "version": version}, "api": bool(env.get("ANTHROPIC_API_KEY"))}


def available(kind, det):
    return {"claude-cli": det["cli"]["found"], "anthropic-api": det["api"], "simulated": True}[kind]


def recommend(det, need_llm=False):
    for k in BACKENDS:
        if k == "simulated" and need_llm:
            continue
        if available(k, det):
            return k
    return None


def describe(kind, det):
    if kind == "claude-cli":
        return f"Claude Code CLI found ({det['cli']['version']}), subscription login" if det["cli"]["found"] else "Claude Code CLI not found"
    if kind == "anthropic-api":
        return "ANTHROPIC_API_KEY is set" if det["api"] else "ANTHROPIC_API_KEY is not set"
    return "built-in simulated translator"


# ------------------------------------------------------------------ testing
def hint_for(kind, err):
    e = (err or "").lower()
    if kind == "simulated":
        return "this should never fail; please report it"
    if "timed out" in e or "timeout" in e:
        return "it took longer than 60 s: check your internet connection or try again (a slow first call is normal on a cold start)"
    if "cannot run claude" in e:
        return f"the claude executable could not be started; reinstall Claude Code ({CLI_URL})"
    if "request failed" in e:   # urllib: DNS, refused, offline
        return "could not reach the API: check your internet connection, proxy or VPN (this is not a problem with your key)"
    if "http 401" in e or "authentication" in e or "invalid x-api-key" in e:
        return f"the API rejected the key: check it, or create a new one at {KEY_URL}"
    if "http 403" in e or "permission" in e:
        return "the key or account has no permission for this model: try --model with another model"
    if "http 429" in e or "rate_limit" in e:
        return "rate limit or spend cap reached: wait a minute, or check your limits in the Console"
    if "http 404" in e or "not_found" in e or "model" in e:
        return "the model name may be wrong: try --model with another one"
    if kind == "claude-cli" and ("rc=" in e or "login" in e or "auth" in e):
        return "the CLI is probably not logged in: run `claude auth login` (or just `claude`) once, then try again; with no network it fails the same way"
    return "see the message above; --no-test skips this test"


def build(kind, model=None, strip_api_key=False):
    return T.make_translator(kind, model, strip_api_key=strip_api_key)


def test_backend(kind, model=None, strip_api_key=False, timeout=TEST_TIMEOUT):
    """One real minimal translation (a short sentence through the same code the agent uses). -> (ok, seconds, detail)"""
    t0 = time.perf_counter()
    try:
        tr = build(kind, model, strip_api_key)
        tr.attempts, tr.timeout = 1, timeout
        res = tr.interpret("There is a lamp called l1.", {"circuit": None, "current_draft": None})
        if not isinstance(res, dict) or "intent" not in res:
            raise T.TranslatorError("the reply was not the expected JSON")
    except T.TranslatorError as e:
        return False, time.perf_counter() - t0, str(e)[:200]
    return True, time.perf_counter() - t0, getattr(tr, "model", None) or "built-in grammar"


# ------------------------------------------------------------------ arguments
def add_arguments(ap):
    g = ap.add_argument_group("translator (what understands natural language)")
    g.add_argument("--translator", choices=BACKENDS, help="claude-cli = your Claude subscription through the Claude Code CLI; anthropic-api = an API key "
                   "(ANTHROPIC_API_KEY); simulated = built-in closed grammar, no AI. Without it the program asks (or auto-detects without a terminal). "
                   "Env: A2A_TRANSLATOR")
    g.add_argument("--model", metavar="NAME", help=f"model for claude-cli / anthropic-api (default {T.DEFAULT_MODEL}). Env: A2A_MODEL")
    g.add_argument("--no-test", action="store_true", help="do not test the backend with a real translation before starting")
    g.add_argument("--use-subscription", action="store_true", help="claude-cli: ignore ANTHROPIC_API_KEY so the CLI bills your subscription. Env: A2A_CLI_USE_SUBSCRIPTION=1")
    return g


# ------------------------------------------------------------------ the assistant
def choose(args, need_llm=False, env=None, interactive=None, ask=input, secret=getpass.getpass, out=None, tester=None, detector=None):
    """-> (kind, model, strip_api_key). Precedence: flag > env > assistant. May print; exits (WizardError) on an unusable explicit choice."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    tester = tester or test_backend
    P = lambda *a: print(*a, file=out, flush=True)           # noqa: E731
    model = getattr(args, "model", None) or env.get("A2A_MODEL") or None
    strip = bool(getattr(args, "use_subscription", False)) or env.get("A2A_CLI_USE_SUBSCRIPTION") == "1"
    no_test = bool(getattr(args, "no_test", False))
    explicit = getattr(args, "translator", None) or env.get("A2A_TRANSLATOR") or None
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    det = None if explicit == "simulated" else (detector or detect)(env)      # `claude --version` is not run when it cannot matter

    def run_test(kind, st):
        print(f"testing {kind}… ", end="", file=out, flush=True)
        ok, secs, detail = tester(kind, model, st)
        if ok:
            P(f"OK ({secs:.1f} s, model {detail})" if kind != "simulated" else f"OK ({secs:.1f} s, {detail})")
        else:
            P(f"FAILED ({secs:.1f} s)\n  {detail}\n  how to fix: {hint_for(kind, detail)}")
        return ok

    # -- 1. explicit: exactly what was asked, no questions, no silent fallback
    if explicit:
        if explicit not in BACKENDS:
            raise WizardError(f"error: unknown translator {explicit!r}; use one of: {', '.join(BACKENDS)}")
        if need_llm and explicit == "simulated":
            raise WizardError("error: this command needs a real LLM; use --translator claude-cli or anthropic-api")
        if explicit != "simulated" and not available(explicit, det):
            raise WizardError(f"error: translator {explicit} was requested but is not available: {describe(explicit, det)}. "
                              + (f"Install it from {CLI_URL} and run `claude auth login`." if explicit == "claude-cli"
                                 else f"Create a key at {KEY_URL} and export ANTHROPIC_API_KEY (or run without --translator to be guided)."))
        if not no_test and not run_test(explicit, strip):
            raise WizardError(f"error: translator {explicit} failed its test (use --no-test to start anyway, or pick another with --translator)")
        P(f"translator: {explicit} (requested {'by --translator' if getattr(args, 'translator', None) else 'by A2A_TRANSLATOR'})")
        return explicit, model, strip

    rec = recommend(det, need_llm)

    # -- 2. no terminal: pick the best detected one, say so, go on
    if not interactive:
        order = [k for k in BACKENDS if available(k, det) and not (need_llm and k == "simulated")]
        if not order:
            raise WizardError("error: this command needs a real LLM and none was found. Install the Claude Code CLI "
                              f"({CLI_URL}) or export ANTHROPIC_API_KEY ({KEY_URL}), or pass --translator.")
        for i, kind in enumerate(order):
            st = strip
            P(f"translator: {kind} (auto-selected, no terminal to ask: {describe(kind, det)}; pass --translator to choose)")
            if no_test or run_test(kind, st):
                return kind, model, st
            if i + 1 < len(order):
                P(f"  {kind} failed its test; trying the next detected backend")
        raise WizardError("error: no detected backend passed its test; see above (--no-test skips the test)")

    # -- 3. terminal: ask
    cli, api = det["cli"], det["api"]
    while True:
        P("\nHow should the agent understand natural language?\n")
        P(f"  1) My Claude subscription via the Claude Code CLI    [{'detected: ' + cli['version'] if cli['found'] else 'not found: install from ' + CLI_URL}]")
        P(f"  2) Anthropic API key                                  [{'ANTHROPIC_API_KEY found' if api else 'ANTHROPIC_API_KEY not set: get one at ' + KEY_URL}]")
        if not need_llm:
            P("  3) Built-in simulated translator (no AI, limited English, good for a first try)")
        top = 2 if need_llm else 3
        default = {"claude-cli": "1", "anthropic-api": "2", "simulated": "3"}[rec] if rec else "1"
        try:
            raw = ask(f"\nChoose 1-{top} [{default}, Enter = recommended]: ").strip()
        except EOFError:
            P("\n(no more input: using the recommended option)")
            raw = ""
        raw = raw or default
        if raw not in [str(i) for i in range(1, top + 1)]:
            P(f"  please type a number from 1 to {top}")
            continue
        kind = BACKENDS[int(raw) - 1]
        st = strip
        if kind == "claude-cli":
            if not cli["found"]:
                P(f"  The `claude` command was not found. Install Claude Code ({CLI_URL}), run `claude auth login` once, and start this again; or pick another option.")
                continue
            if det["api"] and not strip:
                try:
                    a = ask("  ANTHROPIC_API_KEY is also set, and the CLI would use it (billing the API, not your plan). Use your subscription instead? [Y/n]: ").strip().lower()
                except EOFError:
                    a = ""
                st = a not in ("n", "no")
        if kind == "anthropic-api" and not env.get("ANTHROPIC_API_KEY"):
            P("  No ANTHROPIC_API_KEY found. To get one:\n"
              f"    1. Create an account and a key at {KEY_URL} (pay-as-you-go, separate from a claude.ai subscription).\n"
              "    2. In your shell run: export ANTHROPIC_API_KEY=your-key   then start this program again.")
            try:
                k = secret("  Or paste the key now, for this run only (hidden, kept in memory, never saved); Enter = back to the menu: ").strip()
            except EOFError:
                k = ""
            if not k:
                continue
            env["ANTHROPIC_API_KEY"] = k
            det["api"] = api = True
        if kind == "simulated" or no_test or run_test(kind, st):
            if kind == "simulated" and not no_test:
                run_test(kind, st)
            P(f"translator: {kind} (chosen by you)")
            return kind, model, st
        try:
            a = ask("  Choose another option? [Y/n]: ").strip().lower()
        except EOFError:
            a = "n"
        if a in ("n", "no"):
            raise WizardError("error: the chosen translator failed its test")
