"""Deterministic error taxonomy for the harness.

The sentinel result block's ``message`` starts with ``<ErrorClass>: `` so the
remediation classifier can key off it without parsing free text.
"""


class HarnessError(Exception):
    """Base for all runtime failures the harness converts to a sentinel block."""

    @property
    def error_class(self) -> str:
        return type(self).__name__

    def sentinel_message(self) -> str:
        return f"{self.error_class}: {self}"


class ContractError(HarnessError):
    """Contract missing, invalid, node not found, or script missing."""


class ReadError(HarnessError):
    """Unknown read name, or a declared read failed at the warehouse."""


class ScriptError(HarnessError):
    """run() raised, has the wrong signature, or returned a non-Arrow-convertible value."""


class ConformError(HarnessError):
    """Structural mismatch, strict-cast failure, or VARCHAR overflow."""


class LoadError(HarnessError):
    """DDL or INSERT failure at the warehouse during the write."""
