"""Entry point for `python3 -m agentbus`, which is how the CLI runs before any install step."""
import sys

from .cli import main

sys.exit(main())
