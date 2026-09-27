"""Pick the audio backend for this platform. Override with MICGUARD_BACKEND=coreaudio|wasapi|pulse|dummy."""
import os
import sys

from .base import Backend, BackendError, InputDevice

__all__ = ["Backend", "BackendError", "InputDevice", "load_backend"]


def load_backend():
    name = os.environ.get("MICGUARD_BACKEND")
    if not name:
        name = {"darwin": "coreaudio", "win32": "wasapi"}.get(sys.platform, "pulse")
    try:
        if name == "coreaudio":
            from .coreaudio import CoreAudioBackend as cls
        elif name == "wasapi":
            from .wasapi import WasapiBackend as cls
        elif name == "pulse":
            from .pulse import PulseBackend as cls
        elif name == "dummy":
            from .dummy import DummyBackend as cls
        else:
            raise BackendError(f"unknown backend '{name}'")
    except ImportError as e:
        hint = {"wasapi": "pip install pycaw", "pulse": "pip install pulsectl"}.get(name, "")
        raise BackendError(f"backend '{name}' unavailable: {e}. {hint}".strip()) from e
    return cls()
