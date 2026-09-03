# PDF Optimizer

PDF Optimizer is a Windows desktop app for content-aware PDF compression and complete folder cloning. It automatically distinguishes text/vector documents from mixed or image-heavy scans, then applies the selected compression level while keeping source files untouched.

## Compression levels

| Level | Automatic behavior | Quality tradeoff |
| --- | --- | --- |
| **Minimum** | Structural/object-stream and lossless Flate compression only | Strictly lossless; images and vectors are not re-encoded |
| **Medium** | Keeps text/vector PDFs on the lossless path; recompresses sufficiently large images in mixed/scanned PDFs at high quality | Recommended for everyday use; differences should be difficult to notice at normal viewing size |
| **Strong** | Uses the same content detection with stronger JPEG compression and a smaller maximum raster dimension | Produces smaller scans; softness or JPEG artifacts may be visible when zoomed |

Text, fonts, vectors, links, forms, annotations, and page structure are never rasterized. Medium and Strong only target qualifying RGB or grayscale raster images. Transparent, masked, monochrome, small, and unusual color-space images are conservatively skipped.

Size reduction is not guaranteed. If a candidate is not smaller, the app discards it, creates an exact byte-for-byte copy under the output name, and reports **Already optimal**.

## Folder clone mode

Use **Add folder** to create a complete sibling clone named `<folder>_optimized` (or place it in a chosen output directory). The app:

- Recreates every subfolder, including empty folders.
- Copies Word, Excel, images, archives, and every other non-PDF file without changing their names or contents.
- Optimizes each PDF into its original relative location and original filename inside the clone.
- Copies signed, encrypted, or malformed PDFs unchanged so the cloned folder is still complete.
- Builds the clone in a temporary directory and publishes it only after the full tree is ready; canceled work leaves no partial clone.

Existing clone names are never overwritten; `_optimized_1`, `_optimized_2`, and so on are used when necessary.

## Features

- Add multiple PDFs or a whole folder with the picker or drag and drop.
- Choose Minimum, Medium, or Strong compression.
- Automatically use a lossless text/vector path or an image-aware mixed/scan path.
- Clone complete folder trees while preserving all non-PDF files and relative paths.
- Optimize the batch in the background while the interface remains responsive.
- See progress, per-file status, original and output sizes, and savings.
- Cancel queued work safely.
- Open each completed output's containing folder from the app.
- Save PDF results and cloned folders beside each source by default, or choose one output location for the batch.
- Generate collision-safe `_optimized` output names without touching originals or overwriting existing outputs.

## Signed and encrypted PDFs

Rewriting a digitally signed PDF invalidates its cryptographic signature, even if the pages still look identical. Standalone signed PDFs are therefore skipped. In folder clone mode they are copied unchanged.

Encrypted or password-protected standalone PDFs are also skipped; folder clone mode copies them unchanged. The app does not ask for, store, or remove PDF passwords.

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

## Dear PyGui alternative

An optional Dear PyGui interface is included alongside the standard Tkinter app. Double-click `run_dearpygui.bat` to create its separate `.venv-dearpygui` environment and launch it; the existing `run.bat` workflow is unchanged.

To install and run the alternative interface manually:

```powershell
py -3 -m venv .venv-dearpygui
.venv-dearpygui\Scripts\python.exe -m pip install -r requirements-dearpygui.txt
.venv-dearpygui\Scripts\python.exe app_dearpygui.py
```

Use `app_dearpygui.py --smoke-test` for a non-interactive startup check.

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

PyInstaller packages applications for the operating system on which it runs, so create the Windows executable on Windows. The included spec uses the platform-aware CustomTkinter and TkinterDnD2 hooks and packages pikepdf/qpdf for the standalone app.

## Notes on results

- Minimum compression is most effective on PDFs with inefficient object layout, uncompressed streams, or obsolete cross-reference organization.
- Medium and Strong can substantially shrink image-heavy documents because those modes intentionally re-encode eligible raster images.
- Automatic classification is conservative: unsupported or risky image formats are kept as-is.
- “0 B saved” or **Already optimal** is a valid result, not an optimization failure.
- Always keep important source documents and independently verify critical outputs before distribution.
