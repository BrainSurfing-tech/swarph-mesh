"""Fail-closed binary resolution for the CLI-lane adapters (#823).

``shutil.which(name) or "<guess>"`` manufactured a plausible path when the
binary was absent: the exec then died in ~5ms as ``FileNotFoundError`` deep
in subprocess, which reads as a flaky worker rather than a missing
dependency (828 identical failures over 16 days on one box — card #816).
The exec-time catches can only say "firejail or agy not found" — by then
the manufactured path has erased which one. Resolution must instead fail
at resolve time, naming the one binary that is missing and every location
searched.
"""

from __future__ import annotations

import os
import shutil

from swarph_mesh.exceptions import BinaryNotFound


def which_or_raise(name: str, *, env_var: str, checked: tuple[str, ...] = ()) -> str:
    """PATH-resolve ``name``; a miss RAISES naming every location searched.

    ``checked`` carries the adapter-specific locations already probed (and
    found absent) before this PATH lookup. Never returns a path that was
    not actually found — a guessed path is the #823 defect.
    """
    found = shutil.which(name)
    if found:
        return found
    probed = "".join(f"{c} (absent), " for c in checked)
    raise BinaryNotFound(
        f"`{name}` not found: searched {probed}"
        f"PATH={os.environ.get('PATH', '')!r} and ${env_var} is unset. "
        f"Install {name} or set ${env_var} to its absolute path."
    )
