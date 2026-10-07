"""LpkUnpackerGUI application package."""

import tempfile as _tempfile
from pathlib import Path as _Path


def _use_long_temp_path() -> None:
    """Make every temporary directory use the resolved (long) TEMP path.

    Windows may expose TEMP in 8.3 form (``C:\\Users\\RUNNER~1\\...``) when the
    user name is long or non-ASCII.  Editor workspaces compare stored paths
    with ``Path.resolve()`` results, which expand short names, so mixing both
    forms made saves fail with "Asset changed/escaped".
    """
    try:
        _tempfile.tempdir = str(_Path(_tempfile.gettempdir()).resolve())
    except OSError:
        pass


_use_long_temp_path()
