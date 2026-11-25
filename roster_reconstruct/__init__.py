"""Reconstruct visible roster rows from vector page descriptions."""

from .archive import ArchiveStore, ingest, list_batches, preview, query
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
    ArchiveError,
    DuplicateCellError,
    PageDescriptionError,
    RosterError,
)
from .staging import StagingStore

__all__ = [
    "ArchiveError",
    "ArchiveStore",
    "BookStreamProcessor",
    "CROSS_BOUNDARY_REASON",
    "DuplicateCellError",
    "PARTIAL_REASON",
    "PageBuilder",
    "PageDescriptionError",
    "RosterError",
    "StagingStore",
    "ingest",
    "iter_business_rows",
    "list_batches",
    "process_jsonl",
    "process_page_description",
    "preview",
    "query",
    "write_jsonl",
]

__version__ = "1.1.0"
