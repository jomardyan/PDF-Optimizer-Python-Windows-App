"""Allow the app to be launched with ``python -m pdf_optimizer``."""

from app import main

if __name__ == "__main__":
    raise SystemExit(main())

