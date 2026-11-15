"""The distributions installed in the running Python environment.

``continuo-runtime packages`` prints them, so that a producer bundle records exactly
what the image its service runs holds.
"""

import logging
import re
from importlib import metadata

logger = logging.getLogger(__name__)

_NAME_SEPARATORS = re.compile(r"[-_.]+")


def _normalise_name(name: str) -> str:
    """The PEP 503 normalised form of a distribution name: lower case, every run of ``-``, ``_`` and ``.`` one ``-``."""
    return _NAME_SEPARATORS.sub("-", name).lower()


def installed_packages() -> dict[str, str]:
    """Every installed distribution, as ``{normalised name: version}``.

    When two distributions normalise to the same name (one on each of two ``sys.path``
    entries), the version ``importlib.metadata.version`` resolves wins: the one an
    ``import`` of the package would find. When the metadata cannot resolve the name
    (a folder that does not carry its distribution's name), the first one found wins.
    A distribution whose metadata has no name or no version is logged and left out,
    since it names nothing a release can record.
    """
    packages: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        version = dist.metadata.get("Version")
        if not name or not version:
            logger.warning(
                "skipping a distribution under %s: its metadata has name %r and version %r",
                dist.locate_file(""),
                name,
                version,
            )
            continue
        key = _normalise_name(name)
        if key not in packages:
            packages[key] = version
            continue
        try:
            packages[key] = metadata.version(key)
        except metadata.PackageNotFoundError:
            logger.warning(
                "%s appears twice and importlib.metadata cannot tell which one it resolves, "
                "keeping the first one found (%s)",
                key,
                packages[key],
            )
    return packages
