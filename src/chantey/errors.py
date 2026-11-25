"""Error family. Every fallible operation raises a ChanteyError subclass;
the CLI turns them into a clean stderr message and a non-zero exit."""

from __future__ import annotations


class ChanteyError(Exception):
    """Base class for all chantey errors."""


class MissingBinaryError(ChanteyError):
    """A required external program is not installed; message names the install."""


class ToolRunError(ChanteyError):
    """An external program ran and failed; message carries its output tail."""


class OutputCapError(ToolRunError):
    """An external program was stopped at chantey's cap on the file it writes."""

    def __init__(self, what: str, limit: int):
        super().__init__(f"{what} was stopped at chantey's {limit:,}-byte output cap")
        self.limit = limit


class Stopped(ChanteyError):
    """A signal stopped the run; raised from the handler so cleanup runs."""

    def __init__(self, signum: int, name: str):
        super().__init__(f"stopped by {name}; temporary files removed")
        self.signum = signum


class ScoreLoadError(ChanteyError):
    """A score file could not be read or parsed."""


class IngestError(ChanteyError):
    """OMR or normalization produced nothing usable."""


class OutputExistsError(ChanteyError):
    """An output file exists and --force was not given (non-clobbering rule)."""
