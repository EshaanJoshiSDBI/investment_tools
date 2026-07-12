class MfTrackerError(Exception):
    """Base error for expected ingestion failures."""


class UnsupportedWorkbookError(MfTrackerError):
    pass


class WorkbookReadError(MfTrackerError):
    pass


class ValidationError(MfTrackerError):
    pass


class SnapshotConflictError(MfTrackerError):
    pass


class PersistenceError(MfTrackerError):
    pass


class MigrationError(PersistenceError):
    pass


class SourceArchiveError(PersistenceError):
    pass


class BundleError(PersistenceError):
    pass
