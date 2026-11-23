"""``python -m converter_studio``: the command-line tool."""

import sys

from ._cli import main

if __name__ == "__main__":
    sys.exit(main())
