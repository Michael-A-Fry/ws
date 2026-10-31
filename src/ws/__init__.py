"""ws: your memory, skills and rules, in any AI."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ws-aitoolkit")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+src"
