# PDF Optimizer

PDF Optimizer is a Windows desktop app for batch-optimizing PDF files without reducing visible quality. It uses pikepdf and qpdf to rewrite the PDF structure more efficiently while leaving page content, image resolution, image codecs, fonts, and vector graphics intact.

## What “lossless” means here

The optimizer uses only structural, lossless PDF transformations. It does not downsample images, convert pages to pictures, lower JPEG quality, remove pages, or intentionally alter document content. The optimized file is not byte-for-byte identical to the source because its internal object layout and compression can change, but its rendered content should remain visually unchanged.

Size reduction is not guaranteed. PDFs that are already efficient—especially scanned documents dominated by JPEG images—may not become smaller without lossy image recompression. When no smaller lossless candidate is available, the app creates a byte-for-byte copy under the unique `_optimized` output name and reports **Already optimal**.

The source PDF is never overwritten or modified. Every successfully processed file receives a separate, uniquely named output file, so existing files are preserved.

## Features

- Add multiple PDFs with the file picker or drag and drop.
- Optimize the batch in the background while the interface remains responsive.
- See progress, per-file status, original and output sizes, and savings.
- Cancel queued work safely.
- Open each completed output's containing folder from the app.
- Save results beside each source by default, or choose one output folder for the batch.
- Generate collision-safe `_optimized` output names without touching originals or overwriting existing outputs.

## Signed and encrypted PDFs

Rewriting a digitally signed PDF invalidates its cryptographic signature, even if the pages still look identical. PDF Optimizer therefore detects and skips signed files instead of producing a misleading “signed” result.

Encrypted or password-protected PDFs are also skipped. The app does not ask for, store, or remove PDF passwords. Decrypt an authorized copy with an appropriate tool first, then optimize that copy if permitted.

## Requirements

- 64-bit Windows 10 or Windows 11
- Python 3.11 or newer when running from source
- Internet access during the first setup, so Python can install dependencies

The normal Windows Python installer includes Tk/Tkinter, which the GUI requires. If you use a custom Python distribution, make sure Tkinter is available.

## Quick start

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/). Enabling the Python launcher during installation is recommended.
2. Double-click `run.bat`.

The launcher works from any folder path, creates an isolated `.venv` beside the app, installs or checks the runtime dependencies, and then opens PDF Optimizer. If setup fails, run `run.bat` from Command Prompt to keep the error message visible.

## Manual installation and run

From PowerShell or Command Prompt in the project directory:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

If `py` is unavailable but `python` points to Python 3.11 or newer, replace `py -3` with `python`.

## Tests

Install the development requirements and run pytest:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```

## Build a Windows executable

Double-click `build.bat`, or invoke it from a terminal to see the complete build log:

```powershell
.\build.bat
```

The builder bootstraps `.venv` when necessary, installs the development dependencies, runs the tests, and packages the app with PyInstaller. A successful build is written to:

```text
dist\PDFOptimizer.exe
```

PyInstaller packages applications for the operating system on which it runs, so create the Windows executable on Windows. The included spec file collects CustomTkinter, pikepdf, and TkinterDnD2 resources required by the standalone app.

## Notes on results

- Lossless optimization is most effective on PDFs with inefficient object layout, duplicate structures, uncompressed streams, or obsolete cross-reference organization.
- It generally cannot substantially shrink image-heavy files without changing image data and therefore visible fidelity.
- “0 B saved” or **Already optimal** is a valid result, not an optimization failure.
- Always keep important source documents and independently verify critical outputs before distribution.
