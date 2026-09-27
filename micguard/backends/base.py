"""Backend interface shared by every platform.

A backend knows how to enumerate physical audio inputs, report whether each is
live, mute all of them at the device/endpoint level (so every app is affected),
and put back the levels it found before muting.

Muted means: hardware/endpoint mute set, OR input volume at zero. Both are set
when muting, so either one being reverted by another app still leaves the other
in place until the enforcement loop catches it.
"""
import json
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

RESTORE_FALLBACK_VOLUME = 0.75


class BackendError(OSError):
    """Raised when the platform audio API fails."""


@dataclass
class InputDevice:
    uid: str
    name: str
    live: bool
    controllable: bool = True
    detail: str = ""

    @property
    def label(self):
        return self.name if self.controllable else f"{self.name} (no software control)"


def state_dir():
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    d = base / "micguard"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Backend(ABC):
    """Subclasses implement devices(), _mute_device(), _restore_device(), _open_device()."""

    name = "base"

    def __init__(self):
        self.state_file = state_dir() / f"state-{self.name}.json"
        self.saved = self._load()

    # ---- implemented per platform

    @abstractmethod
    def devices(self):
        """Return a list of InputDevice for every physical input."""

    @abstractmethod
    def _mute_device(self, uid):
        """Mute one device. Return its pre-mute levels (JSON-serialisable) and whether anything changed."""

    @abstractmethod
    def _restore_device(self, uid, saved):
        """Put back levels previously returned by _mute_device."""

    @abstractmethod
    def _open_device(self, uid):
        """Unmute a device with no saved levels (volume to RESTORE_FALLBACK_VOLUME if it was 0)."""

    def close(self):
        pass

    # ---- shared logic

    def live_devices(self):
        return [d for d in self.devices() if d.live or not d.controllable]

    def mute_all(self):
        """Mute every controllable input. Returns (changed_names, uncontrollable_names)."""
        changed, uncontrollable = [], []
        for d in self.devices():
            if not d.controllable:
                uncontrollable.append(d.name)
                continue
            before, touched = self._mute_device(d.uid)
            # Only record levels the first time; a re-mute must not save "muted" as the original.
            if d.uid not in self.saved and d.live:
                self.saved[d.uid] = before
            if touched:
                changed.append(d.name)
        self._save()
        return changed, uncontrollable

    def restore_all(self):
        """Restore saved levels; unmute anything muted with no saved levels. Returns names touched."""
        touched = []
        for d in self.devices():
            if not d.controllable:
                continue
            saved = self.saved.pop(d.uid, None)
            if saved is not None:
                self._restore_device(d.uid, saved)
                touched.append(d.name)
            elif not d.live:
                self._open_device(d.uid)
                touched.append(d.name)
        self._save()
        return touched

    def _load(self):
        try:
            return json.loads(self.state_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        try:
            self.state_file.write_text(json.dumps(self.saved, indent=2))
        except OSError:
            pass
