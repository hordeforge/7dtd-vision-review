"""Ask 7dtd-playtest where its game installs are; one answer per invocation.

The e2e used to carry these queries as `python3 -c` bodies inside
`e2e.sh`: a sibling repository's module imported through a `sys.path`
insert, invisible to the linter and the type checker. The queries stay here,
one named command each, and the shell only passes arguments.

The first argument is the 7dtd-playtest checkout; the rest select the
question. Detection reads the Steam library and never contacts a network,
but a missing or broken checkout prints nothing and exits non-zero, which
the caller reads as "not detected".

Usage:
  playtest_detect.py PLAYTEST_ROOT game            client install dir, or nothing
  playtest_detect.py PLAYTEST_ROOT compat PATH     Proton prefix for PATH
  playtest_detect.py PLAYTEST_ROOT server          dedicated server dir
"""

from __future__ import annotations

import pathlib
import sys

SERVER_STEM = "7 Days to Die Dedicated Server"
SERVER_BINARY = "7DaysToDieServer.x86_64"


USAGE = "usage: playtest_detect.py PLAYTEST_ROOT {game|compat PATH|server}"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(USAGE, file=sys.stderr)
        return 2
    root, command, arguments = pathlib.Path(argv[1]), argv[2], argv[3:]
    if command not in ("game", "compat", "server") or len(arguments) != (
        1 if command == "compat" else 0
    ):
        print(USAGE, file=sys.stderr)
        return 2
    sys.path.insert(0, str(root / "scripts"))
    # The sibling checkout is a runtime argument, so no stub can exist for it;
    # its three callables are used exactly as playtest_run declares them.
    import playtest_run  # type: ignore[import-not-found]

    if command == "game":
        print(playtest_run.client_game_dir() or "")
    elif command == "compat":
        print(playtest_run.client_compat_for_game(pathlib.Path(arguments[0])))
    else:
        for library in playtest_run.steam_library_dirs():
            candidate = library / "common" / SERVER_STEM
            if (candidate / SERVER_BINARY).is_file():
                print(candidate)
                break
        else:
            default = playtest_run.DEFAULT_GAME_SRV
            if (default / SERVER_BINARY).is_file():
                print(default)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
