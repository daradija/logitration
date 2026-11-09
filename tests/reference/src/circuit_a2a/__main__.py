"""python -m circuit_a2a <command> [options]   (or: circuit-a2a <command> ...)

commands:
  server         start the agent (asks how to understand English if you do not say; see `server --help`)
  say            one turn of conversation with a running agent
  owner_cli      chat with a running agent in the terminal
  owner_claude   a Claude session plays the owner of a circuit (needs an LLM)
Each command has --help."""
import importlib
import sys

COMMANDS = ("server", "owner_cli", "say", "owner_claude")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    if not argv or argv[0] not in COMMANDS:
        sys.exit(__doc__.strip())
    importlib.import_module("circuit_a2a." + argv[0]).main(argv[1:])


if __name__ == "__main__":
    main()
