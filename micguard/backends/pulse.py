"""Linux backend: PulseAudio sources via pulsectl.

Works on PulseAudio and on PipeWire through pipewire-pulse (default on current
Ubuntu/Fedora/Debian desktops). Source mute/volume is server-side, so every
client is affected.
Requires: pip install pulsectl
"""
import pulsectl

from .base import RESTORE_FALLBACK_VOLUME, Backend, BackendError, InputDevice

PA_INVALID = 0xFFFFFFFF


def _is_monitor(src):
    mon = getattr(src, "monitor_of_sink", None)
    return (mon is not None and mon != PA_INVALID) or src.name.endswith(".monitor")


class PulseBackend(Backend):
    name = "pulse"

    def __init__(self):
        super().__init__()
        self._pulse = None
        self._connect()

    def _connect(self):
        try:
            self._pulse = pulsectl.Pulse("micguard")
        except pulsectl.PulseError as e:
            self._pulse = None
            raise BackendError(f"cannot connect to PulseAudio/PipeWire: {e}") from e

    def _call(self, fn):
        """Run fn(pulse); reconnect once if the server went away (pipewire restart, re-login)."""
        if self._pulse is None:
            self._connect()
        try:
            return fn(self._pulse)
        except (pulsectl.PulseError, pulsectl.PulseDisconnected):
            try:
                self._pulse.close()
            except Exception:
                pass
            self._connect()
            try:
                return fn(self._pulse)
            except (pulsectl.PulseError, pulsectl.PulseDisconnected) as e:
                raise BackendError(str(e)) from e

    def close(self):
        if self._pulse is not None:
            self._pulse.close()
            self._pulse = None

    def _sources(self):
        return {s.name: s for s in self._call(lambda p: p.source_list()) if not _is_monitor(s)}

    def devices(self):
        return [
            InputDevice(
                uid=name,
                name=s.description or name,
                live=not (s.mute or s.volume.value_flat == 0.0),
                detail=s.driver or "",
            )
            for name, s in self._sources().items()
        ]

    def _src(self, uid):
        src = self._sources().get(uid)
        if src is None:
            raise BackendError(f"source {uid} disappeared")
        return src

    def _mute_device(self, uid):
        src = self._src(uid)
        before = {"mute": bool(src.mute), "volumes": list(src.volume.values)}
        touched = False
        if not src.mute:
            self._call(lambda p: p.mute(src, True))
            touched = True
        if any(v != 0.0 for v in src.volume.values):
            self._call(lambda p: p.volume_set_all_chans(src, 0.0))
            touched = True
        return before, touched

    def _restore_device(self, uid, saved):
        src = self._src(uid)
        vols = saved.get("volumes") or [RESTORE_FALLBACK_VOLUME]
        if len(vols) != len(src.volume.values):  # channel map changed; fall back to flat level
            vols = [sum(vols) / len(vols)] * len(src.volume.values)
        self._call(lambda p: p.volume_set(src, pulsectl.PulseVolumeInfo(vols)))
        self._call(lambda p: p.mute(src, bool(saved.get("mute"))))

    def _open_device(self, uid):
        src = self._src(uid)
        if src.volume.value_flat == 0.0:
            self._call(lambda p: p.volume_set_all_chans(src, RESTORE_FALLBACK_VOLUME))
        self._call(lambda p: p.mute(src, False))
