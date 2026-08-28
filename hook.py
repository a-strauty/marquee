#!/usr/bin/env python3
"""Install / move / remove the dashboard's dofile hook in custom.lua.

Matching on the exact path is not enough: if the dashboard folder moves,
the old line survives and points at a file that no longer exists. Lua's
dofile() raises on a missing file, which would take down the rest of
custom.lua with it. So every dashboard hook is stripped by *shape* first,
then the current one is re-added.

Usage:
    hook.py <custom.lua> <monitor> <dofile-line>   install / move
    hook.py <custom.lua> --remove                  uninstall
"""

import pathlib
import re
import sys

HOOK_RE = re.compile(r'dofile\(\s*["\'].*dashboard\.lua["\']\s*\)')
COMMENT_RE = re.compile(r'^\s*--\s*Touch dashboard panel\b')


def main():
    path = pathlib.Path(sys.argv[1])
    remove = sys.argv[2] == "--remove"
    monitor = "" if remove else sys.argv[2]
    hook = "" if remove else sys.argv[3]

    lines = path.read_text().splitlines() if path.exists() else []

    kept = []
    for line in lines:
        if HOOK_RE.search(line):
            # drop the hook and the comment immediately above it
            if kept and COMMENT_RE.match(kept[-1]):
                kept.pop()
            continue
        kept.append(line)

    while kept and not kept[-1].strip():
        kept.pop()

    if not remove:
        if kept:
            kept.append("")
        kept.append("-- Touch dashboard panel ({})".format(monitor))
        kept.append(hook)

    path.write_text("\n".join(kept) + "\n" if kept else "")
    print("removed" if remove else "hooked")


if __name__ == "__main__":
    main()
