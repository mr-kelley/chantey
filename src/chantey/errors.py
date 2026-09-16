"""Error family. Every fallible operation raises a ChanteyError subclass;
the CLI turns them into a clean stderr message and a non-zero exit."""

from __future__ import annotations


class ChanteyError(Exception):
    """Base class for all chantey errors."""


class MissingBinaryError(ChanteyError):
    """A required external program is not installed; message names the install."""


class ToolRunError(ChanteyError):
    """An external program ran and failed; message carries its output tail."""


class ScoreLoadError(ChanteyError):
    """A score file could not be read or parsed."""


class IngestError(ChanteyError):
    """OMR or normalization produced nothing usable."""


class OutputExistsError(ChanteyError):
    """An output file exists and --force was not given (non-clobbering rule)."""
