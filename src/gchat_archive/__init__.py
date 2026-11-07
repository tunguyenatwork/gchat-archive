"""Local, searchable archive of Google Chat history."""

try:                                    # the installed package is the truth
    from importlib.metadata import version as _dist_version

    __version__ = _dist_version("gchat-archive")
except Exception:                       # running from a source tree: no metadata
    __version__ = "0.2.4"
