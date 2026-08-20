"""The contract's physical-layout ``config`` guard.

A node's ``config`` block is written in the ACTIVE ENGINE's own vocabulary
(postgres: ``indexes``; trino: ``partitioning``/``sorted_by``/…) rather than
under an engine namespace: the contract is engine-bound anyway, so there is no
"someone else's namespace" to excuse a key the engine does not know. An
unrecognized key is therefore an authoring error and must reject the node — a
config the author believes is applied but is silently dropped would ship an
unindexed, unpartitioned table to production behind a green release gate.

Adapters call :func:`ensure_known_keys` before building any DDL.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


def ensure_known_keys(
    config: object, known: Iterable[str], engine: str, where: str = "config"
) -> None:
    """Raise ValueError unless *config* is a mapping whose keys all lie in *known*.

    Parameters
    ----------
    config
        The physical-layout block as it arrived from the validation spec.
        Anything that is not a mapping is rejected rather than coerced.
    known
        The keys this engine's adapter recognizes. May be empty.
    engine
        Engine name, quoted into the message so an author reading a failed
        release gate knows which vocabulary was expected.
    where
        Label for the block under inspection, so a nested caller (an index
        entry, say) can point the message at the right part of the config.

    Raises
    ------
    ValueError
        If *config* is not a mapping, or carries any key outside *known*.
    """
    if not isinstance(config, Mapping):
        raise ValueError(f"{where} must be a mapping, got {type(config).__name__}")
    known_set = set(known)
    unknown = sorted(repr(key) for key in config if key not in known_set)
    if unknown:
        raise ValueError(
            f"unrecognized {where} key(s) {', '.join(unknown)} for engine {engine!r}; "
            f"known keys: {', '.join(sorted(known_set)) or '(none)'}"
        )
