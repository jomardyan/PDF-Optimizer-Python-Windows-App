"""Public API for the PDF Optimizer engine."""

from .folder_optimizer import (
    FolderOptimizationResult,
    FolderProgress,
    FolderStage,
    clone_and_optimize_folder,
    next_available_clone_path,
)
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
from .smart_images import CompressionLevel, DocumentType

__all__ = [
    "CancellationEvent",
    "CompressionLevel",
    "DocumentType",
    "EncryptedPDFError",
    "FolderOptimizationResult",
    "FolderProgress",
    "FolderStage",
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
    "clone_and_optimize_folder",
    "next_available_clone_path",
    "next_available_output_path",
    "optimize_pdf",
]
