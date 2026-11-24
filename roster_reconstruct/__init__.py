"""Reconstruct visible roster rows from vector page descriptions."""

from .book import (
    BookStreamProcessor,
    iter_business_rows,
    process_jsonl,
    process_page_description,
    write_jsonl,
)
from .engine import (
    CROSS_BOUNDARY_REASON,
    PARTIAL_REASON,
    PageBuilder,
)
from .errors import (
    DuplicateCellError,
    PageDescriptionError,
    RosterError,
)
from .staging import StagingStore

__all__ = [
    "BookStreamProcessor",
    "CROSS_BOUNDARY_REASON",
    "DuplicateCellError",
    "PARTIAL_REASON",
    "PageBuilder",
    "PageDescriptionError",
    "RosterError",
    "StagingStore",
    "iter_business_rows",
    "process_jsonl",
    "process_page_description",
    "write_jsonl",
]

__version__ = "1.0.0"
