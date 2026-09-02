"""Public API for the PDF Optimizer engine."""

from .optimizer import (
    CancellationEvent,
    EncryptedPDFError,
    InputPDFError,
    InvalidPDFError,
    OptimizationError,
    OptimizationOptions,
    OptimizationResult,
    OptimizationStage,
    OptimizationStatus,
    OutputWriteError,
    ProgressCallback,
    SignedPDFError,
    ValidationError,
    next_available_output_path,
    optimize_pdf,
)

__all__ = [
    "CancellationEvent",
    "EncryptedPDFError",
    "InputPDFError",
    "InvalidPDFError",
    "OptimizationError",
    "OptimizationOptions",
    "OptimizationResult",
    "OptimizationStage",
    "OptimizationStatus",
    "OutputWriteError",
    "ProgressCallback",
    "SignedPDFError",
    "ValidationError",
    "next_available_output_path",
    "optimize_pdf",
]
