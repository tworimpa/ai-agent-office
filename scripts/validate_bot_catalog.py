"""Check a catalog release before it is published on main."""
from argparse import ArgumentParser
from pathlib import Path

from agent_office import shared_bot_catalog


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=Path(__file__).resolve().parents[1] / "catalog" / "bots.json")
    parser.add_argument("--packages", action="store_true", help="Validate each package at its public Git commit")
    args = parser.parse_args()
    try:
        with args.file.open("rb") as stream:
            catalog = shared_bot_catalog.loads(stream.read(shared_bot_catalog.MAX_INDEX_BYTES + 1))
        if args.packages:
            shared_bot_catalog.verify_packages(catalog)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Validated {len(catalog['entries'])} catalog entries"
          + (" against their pinned Git packages." if args.packages else "."))


if __name__ == "__main__":
    main()
