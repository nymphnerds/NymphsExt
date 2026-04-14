"""Live Blender addon entrypoint for Nymphs3D2."""

try:
    from .Nymphs3D2 import *  # noqa: F401,F403
except ImportError:
    from Nymphs3D2 import *  # type: ignore # noqa: F401,F403
