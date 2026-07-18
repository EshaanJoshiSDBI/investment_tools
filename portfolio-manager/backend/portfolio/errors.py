class PortfolioPersistenceError(Exception):
    """Base error for expected persistence failures."""


class MigrationError(PortfolioPersistenceError):
    pass


class SnapshotNotFoundError(PortfolioPersistenceError):
    pass


class InactiveSnapshotError(PortfolioPersistenceError):
    pass


class DuplicateImportError(PortfolioPersistenceError):
    def __init__(self, message: str, snapshot_id: str):
        super().__init__(message)
        self.snapshot_id = snapshot_id


class BundleError(PortfolioPersistenceError):
    pass
