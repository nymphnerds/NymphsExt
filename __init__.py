"""Live Blender addon entrypoint for Nymphs."""

try:
    from .Nymphs import *  # noqa: F401,F403
except ImportError:
    from Nymphs import *  # type: ignore # noqa: F401,F403
