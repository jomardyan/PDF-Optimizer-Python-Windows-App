"""Desktop entry point for PDF Optimizer."""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    try:
        from pdf_optimizer.gui import PDFOptimizerApp
    except ModuleNotFoundError as exc:
        missing = exc.name or "a required package"
        print(
            f"PDF Optimizer could not start because {missing!r} is missing.\n"
            "Run run.bat, or install the packages with:\n"
            "    python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    app = PDFOptimizerApp()
    if "--smoke-test" in sys.argv:
        app.withdraw()
        app.update_idletasks()
        app.update()
        app.destroy()
        print("GUI smoke test passed")
        return 0

    app.mainloop()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
