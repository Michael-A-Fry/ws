"""ws: your memory, skills and rules, in any AI."""

# Aliased: a bare `version` here would be the package attribute `ws.version`, which is the `ws version` module.
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

try:
    __version__ = _distribution_version("ws-aitoolkit")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+src"
