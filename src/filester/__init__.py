from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("filester-cli")
except PackageNotFoundError:
    # running from a source checkout without an install
    __version__ = "0.0.0-dev"
