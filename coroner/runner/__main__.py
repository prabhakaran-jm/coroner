"""`python -m coroner.runner` entry point."""
import sys

from coroner.runner.cli import main

if __name__ == "__main__":
    sys.exit(main())
