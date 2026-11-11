"""The harness's start check: the image must hold what the release hashed.

continuo sets ``CONTENT_HASH`` to the ``content_hash`` the release promoted for
the node. The check recomputes the node's hash from the files in the image with
the helpers ``merge`` uses (``gather_hash_inputs`` and ``hash_parts``), so the
two can never disagree, and refuses to run a node whose image reproduces a
different one: the image holds other code, imports, assets or contract than the
release.
"""

import logging
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path

from continuo_python_runtime.contract.model import Node
from continuo_python_runtime.errors import ContractError, ImageContractMismatch
from continuo_python_runtime.hash_inputs import gather_hash_inputs
from continuo_python_runtime.hashing import hash_parts

logger = logging.getLogger("continuo_python_runtime.content_check")

_ENV_KEY = "CONTENT_HASH"


def check_image_contract(node: Node, app_root: Path, env: Mapping[str, str]) -> None:
    """Fail unless the image reproduces the ``content_hash`` in ``CONTENT_HASH``.

    An unset or empty ``CONTENT_HASH`` logs a warning and skips the check. Any
    other value is compared as an exact string, so a value without the
    ``sha256:`` prefix, in upper case or with surrounding whitespace is a
    mismatch.

    Raises:
        ImageContractMismatch: The recomputed ``content_hash`` differs, or a
            file the release hashed is missing from the image.
    """
    released = env.get(_ENV_KEY)
    if not released:
        logger.warning(
            "%s is not set: skipping the check that the image holds the release's content for %s",
            _ENV_KEY, node.relation,
        )
        return

    try:
        inputs = gather_hash_inputs(node, app_root)
        parts = hash_parts(node, inputs.primary, inputs.units)
    except ContractError as exc:
        raise ImageContractMismatch(_message(node, released, f"cannot hash the image: {exc}")) from exc

    if parts["content_hash"] != released:
        detail = (
            f"the image's content_hash {parts['content_hash']} "
            f"(source_hash {parts['source_hash']}, "
            f"shared_code_hash {parts['shared_code_hash']}, "
            f"config_hash {parts['config_hash']}, "
            f"units {sorted(inputs.units)})"
        )
        raise ImageContractMismatch(_message(node, released, detail))

    logger.info("image holds the release's content for %s (%s)", node.relation, released)


def _message(node: Node, released: str, detail: str) -> str:
    return (
        f"the image does not hold what the release hashed for {node.relation}: "
        f"{_ENV_KEY} {released} differs from {detail}. "
        f"This image runs runtime {metadata.version('continuo-python-runtime')}. "
        "Fix: rebuild the image from the released commit, with CI on the runtime of the image's FROM tag."
    )
