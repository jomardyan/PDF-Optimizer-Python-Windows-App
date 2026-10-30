# PDF Optimizer – Free Offline PDF Compressor for Windows (Batch & Folder Compression)

**PDF Optimizer is a free Windows desktop app that reduces PDF file size without uploading anything.** Compress a single PDF, a batch of PDFs, or an entire folder tree. Everything runs locally on your PC with Python, [pikepdf](https://github.com/pikepdf/pikepdf) (qpdf) and Pillow, so confidential documents such as contracts, invoices, medical records, and scans never leave your computer.

Choose **Minimum** (lossless), **Medium** (balanced), or **Strong** (maximum compression, typically about **45–50 % smaller** on scanned and print-to-PDF documents). Your original files are never modified.

> **At a glance:** offline PDF compression · batch PDF optimizer · compress a whole folder and keep its structure · parallel processing · text stays selectable and searchable · no upload, no account, no watermark, no page limit.

## Contents

- [Why PDF Optimizer?](#why-pdf-optimizer)
- [How to compress a PDF](#how-to-compress-a-pdf)
- [Compression levels](#compression-levels)
- [Typical results](#typical-results)
- [How the compression works](#how-the-compression-works)
- [Compress an entire folder (folder clone mode)](#compress-an-entire-folder-folder-clone-mode)
- [Features](#features)
- [Repeat batch workflows](#repeat-batch-workflows)
- [Signed and encrypted PDFs](#signed-and-encrypted-pdfs)
- [Requirements](#requirements)
- [Download, install and run](#download-install-and-run)
- [Build a Windows executable (.exe)](#build-a-windows-executable-exe)
- [Tests](#tests)
- [FAQ](#faq)
- [Notes on results](#notes-on-results)

## Why PDF Optimizer?

| | PDF Optimizer | Typical online PDF compressors |
| --- | --- | --- |
| **Privacy** | Files stay on your PC | Files are uploaded to a server |
| **Cost / limits** | Free, no file-count or page limits | Daily limits, paid tiers, watermarks |
| **Batch & folders** | Whole folder trees, parallel workers | Often one file at a time |
| **Non-PDF files** | Copied unchanged into the clone | Not handled |
| **Text** | Stays real, selectable, searchable text | Some tools rasterize pages |
| **Originals** | Never touched; collision-safe output names | Depends on the service |

## How to compress a PDF

1. Start the app (see [Download, install and run](#download-install-and-run)).
2. Click **Add PDFs** or **Add folder**, or drag and drop files onto the window.
3. Pick **Minimum**, **Medium**, or **Strong**.
4. Click **Optimize**. The progress bar, per-file status, original size, new size and savings update live.
5. Open the result from the app. The optimized copy is saved beside the original (name ends in `_optimized`) or in an output folder you choose.

## Compression levels

| Level | Automatic behavior | Quality tradeoff |
| --- | --- | --- |
| **Minimum** | Merges page content streams, shortens redundant digits, and recompresses Flate data at the maximum level; object streams | Strictly lossless; pages render pixel-for-pixel identically and images are not re-encoded |
| **Medium** | Everything in Minimum, plus coordinates in vector drawings trimmed to 3 decimals and high-quality JPEG recompression of large images in mixed/scanned PDFs (colour scans with no real colour are stored as grayscale) | Recommended for everyday use; differences should be difficult to notice at normal viewing size |
| **Strong** | Everything in Medium, plus vector coordinates trimmed to 2 decimals and scans reduced to about 130 dpi at the printed page size with stronger JPEG compression | Roughly halves typical scanned and print-to-PDF documents; softness may be visible when zoomed |

Text, fonts, vectors, links, forms, annotations, and page structure are never rasterized. Medium and Strong only target qualifying RGB, ICC-based, or grayscale raster images. Transparent, masked, monochrome, small, and unusual color-space images are conservatively skipped. Page content that contains text strings, inline images, or comments is never rewritten; only pure vector drawing data (for example text converted to outlines by a browser's print-to-PDF) is shortened.

Size reduction is not guaranteed. If a candidate is not smaller, the app discards it, creates an exact byte-for-byte copy under the output name, and reports **Already optimal**.

## Typical results

Measured on real documents with the app's three levels:

| Document type | Minimum | Medium | Strong |
| --- | --- | --- | --- |
| Print-to-PDF / text-as-outlines mixed with scans (about 6 MB, 16 pages) | 27–29 % smaller | 30–32 % smaller | **47–48 % smaller** |
| Small scanned receipt | – | – | about 66 % smaller |

Minimum is verified pixel-identical to the original when rendered. Your results depend on the document: PDFs that are already well compressed may shrink very little, and that is reported as **Already optimal**.

## How the compression works

PDF Optimizer analyses each file and applies only the techniques that are safe for its content:

- **Vector content compression.** Many PDFs, especially browser "Print to PDF" output and converted slides, hold megabytes of drawing commands written with six decimals and split into dozens of tiny streams. The app merges those streams, removes redundant digits, and (Medium/Strong) trims path coordinates to a precision far below a hundredth of a millimetre.
- **Maximum-level Flate recompression.** Streams are recompressed with zlib level 9, which is tighter than the default level used by most PDF writers.
- **DPI-targeted scan downscaling.** Scans are resampled to a resolution based on the printed page size, instead of a fixed pixel limit, and re-encoded as JPEG.
- **Smart grayscale detection.** Colour scans that contain no real colour (for example yellowed paper) are stored as grayscale.
- **Lossless structural optimization.** Object streams and a clean cross-reference table remove structural overhead.
- **Safe fallback.** The result is validated, and if it is not smaller it is discarded and the original bytes are copied.

## Compress an entire folder (folder clone mode)

Use **Add folder** to create a complete sibling clone named `<folder>_optimized` (or place it in a chosen output directory). The app:

- Recreates every subfolder, including empty folders.
- Copies Word, Excel, images, archives, and every other non-PDF file without changing their names or contents.
- Optimizes each PDF into its original relative location and original filename inside the clone.
- Keeps each optimized PDF's modified time, and copies signed, encrypted, malformed, or otherwise unprocessable PDFs unchanged so the cloned folder is still complete.
- Optimizes up to four PDFs at once in separate worker processes (about 3x faster on image-heavy folders). If a worker crashes on a damaged PDF, only that file is copied unchanged.
- Builds the clone in a temporary directory and publishes it only after the full tree is ready; canceled work leaves no partial clone.

Existing clone names are never overwritten; `_optimized_1`, `_optimized_2`, and so on are used when necessary. Windows directory junctions are rejected to prevent loops or copying data outside the selected tree; select the junction's target folder directly. Symbolic links are preserved when Windows permissions allow it.

This makes it a practical way to **shrink an archive of documents for email, backup, or cloud storage** while keeping the exact folder layout.

## Features

- Add multiple PDFs or a whole folder with the picker or drag and drop.
- Choose Minimum, Medium, or Strong compression.
- Automatically use a lossless text/vector path or an image-aware mixed/scan path.
- Clone complete folder trees while preserving all non-PDF files and relative paths.
- Optimize the batch in the background while the interface remains responsive.
- See progress, per-file status, original and output sizes, and savings.
- Double-click a queue item (or use **View selected details** in the menu) to read its full path and result.
- Use **Change…** to select another custom output folder; canceling the picker keeps the previous selection.
- Cancel queued work safely.
- Open each completed output's containing folder from the app.
- Save PDF results and cloned folders beside each source by default, or choose one output location for the batch.
- Generate collision-safe `_optimized` output names without touching originals or overwriting existing outputs.
- Light and dark appearance.

## Repeat batch workflows

- **Saved preferences:** compression strength, appearance, and the custom output folder are remembered automatically. Use **Reset saved preferences** in the `•••` menu to return to defaults.
- **Save/load queues:** use `••• → Queue` or **Ctrl+S / Ctrl+L** to save a JSON queue and reload it later. The file stores source paths, their order, and settings; it does not embed PDFs or start processing. Loading replaces the current queue after validation. Missing or unsupported sources are reported; if none are available, the current queue is kept.
- **Retry unfinished:** after a batch with failed or canceled items, use the queue's **Retry unfinished** button. It reruns only those items with the current settings and keeps successful outputs and their displayed results. Signed and encrypted skips are not retried automatically.
- **Optimize selected:** select a row, then choose **Optimize selected** from the `•••` menu.
- **Export CSV:** export the latest attempt for each processed item still in the queue, including failures and cancellations. Reports contain source/output paths, compression level, sizes, savings, page count, duration, time, status, and messages. Folder byte totals refer to PDFs, excluding unchanged non-PDF files. Retrying replaces that item's record; clearing/removing queue items removes their records, so export first if you want to keep them.
- **Reorder:** use **Alt+Up / Alt+Down**, or the Queue submenu, to change processing order.
- **Open documents:** use **Open original** or **Open optimized copy** to inspect files in your default PDF viewer (folder items open in Explorer).

Preferences are stored in `%LOCALAPPDATA%\PDFOptimizer\preferences.json` on Windows. Set `PDF_OPTIMIZER_CONFIG_DIR` to use a different configuration directory. Source documents and batch results are not automatically saved in preferences. CSV files use UTF-8 with a BOM for spreadsheet compatibility.

## Signed and encrypted PDFs

Rewriting a digitally signed PDF invalidates its cryptographic signature, even if the pages still look identical. Standalone signed PDFs are therefore skipped. In folder clone mode they are copied unchanged.

Encrypted or password-protected standalone PDFs are also skipped; folder clone mode copies them unchanged. The app does not ask for, store, or remove PDF passwords.

## Requirements

- 64-bit Windows 10 or Windows 11
- Python 3.11 or newer when running from source
- Internet access during the first setup, so Python can install dependencies

The normal Windows Python installer includes Tk/Tkinter, which the GUI requires. If you use a custom Python distribution, make sure Tkinter is available.

## Download, install and run

### Quick start (recommended)

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/). Enabling the Python launcher during installation is recommended.
2. [Clone or download this repository](https://github.com/jomardyan/PDF-Optimizer-Python-Windows-App).
3. Double-click `run.bat`.

The launcher works from any folder path, creates an isolated `.venv` beside the app, installs or checks the runtime dependencies, and then opens PDF Optimizer. If setup fails, run `run.bat` from Command Prompt to keep the error message visible.

### Manual installation

From PowerShell or Command Prompt in the project directory:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

If `py` is unavailable but `python` points to Python 3.11 or newer, replace `py -3` with `python`.

Use `app.py --smoke-test` for a non-interactive startup check.

## Build a Windows executable (.exe)

Double-click `build.bat`, or invoke it from a terminal to see the complete build log:

```powershell
.\build.bat
```

The builder bootstraps `.venv` when necessary, installs the development dependencies, runs the tests, and packages the app with PyInstaller. A successful build is written to:

```text
dist\PDFOptimizer.exe
```

PyInstaller packages applications for the operating system on which it runs, so create the Windows executable on Windows. The included spec uses the platform-aware CustomTkinter and TkinterDnD2 hooks and packages pikepdf/qpdf for the standalone app.

## Tests

Install the development requirements and run pytest:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```

## FAQ

### How do I reduce the size of a PDF without losing quality?
Use **Minimum**. It is strictly lossless: pages render pixel-for-pixel identically, and only the way the data is stored changes. Savings are largest on PDFs with inefficient structure or uncompressed streams.

### How can I compress a scanned PDF to a much smaller size?
Use **Medium** or **Strong**. Scans are resampled to a target resolution for the printed page size (about 130 dpi at Strong) and re-encoded as JPEG, and colour scans without real colour are stored as grayscale. Text in the PDF stays text.

### Can I compress many PDFs, or a whole folder, at once?
Yes. Add several files or a folder. In folder mode the app builds an `_optimized` copy of the whole tree, optimizes every PDF in it (up to four in parallel), and copies all non-PDF files unchanged.

### Is it safe for confidential documents?
Processing is entirely local. The app does not upload files or need an account, and it never overwrites your originals. Always keep the source documents and verify critical outputs before distribution.

### Will the text still be searchable and selectable?
Yes. Text, fonts, links, forms and annotations are never rasterized.

### Why did a file say "Already optimal"?
The result was not smaller than the original, so the app kept an exact copy instead. That is a valid outcome for PDFs that are already well compressed.

### Does it compress digitally signed or password-protected PDFs?
No. Rewriting would break a signature, and the app does not handle passwords. Standalone, such files are skipped; in folder clone mode they are copied unchanged.

### Does it work on macOS or Linux?
The GUI and packaging target Windows 10/11. The optimization engine in `pdf_optimizer/` is plain Python and is also usable as a library.

## Notes on results

- Minimum compression is most effective on PDFs with inefficient object layout, uncompressed streams, or obsolete cross-reference organization.
- Medium and Strong can substantially shrink image-heavy documents because those modes intentionally re-encode eligible raster images.
- Vector-heavy PDFs (for example text converted to outlines by a browser's print-to-PDF) benefit from the content-stream pass, which Strong takes furthest. It is slower per file because the result is fully validated.
- Automatic classification is conservative: unsupported or risky image formats are kept as-is.
- “0 B saved” or **Already optimal** is a valid result, not an optimization failure.
- Always keep important source documents and independently verify critical outputs before distribution.

---

**Keywords:** PDF compressor, reduce PDF file size, compress PDF offline, batch PDF compression, compress PDF folder, PDF optimizer for Windows, shrink scanned PDF, lossless PDF optimization, Python PDF compression, pikepdf, qpdf, CustomTkinter.
