"""Windows backend: Core Audio IAudioEndpointVolume on every active capture endpoint.

Endpoint mute/volume is system-wide, the same thing the Sound control panel's
Recording tab sets, so it applies to Teams, Zoom, browsers, everything.
Requires: pip install pycaw  (pulls in comtypes)
"""
import comtypes
from comtypes import COMError
from pycaw.constants import CLSID_MMDeviceEnumerator, DEVICE_STATE, EDataFlow
from pycaw.pycaw import AudioUtilities, IMMDeviceEnumerator

from .base import RESTORE_FALLBACK_VOLUME, Backend, BackendError, InputDevice


class WasapiBackend(Backend):
    name = "wasapi"

    def __init__(self):
        super().__init__()
        # comtypes initialises COM (STA) for this thread on import; Qt's OleInitialize is also STA.
        self._enum = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER
        )
        self._cache = {}  # endpoint id -> (friendly name, IAudioEndpointVolume)

    def _endpoints(self):
        try:
            coll = self._enum.EnumAudioEndpoints(EDataFlow.eCapture.value, DEVICE_STATE.ACTIVE.value)
            seen = {}
            for i in range(coll.GetCount()):
                raw = coll.Item(i)
                uid = raw.GetId()
                if uid not in self._cache:
                    dev = AudioUtilities.CreateDevice(raw)
                    self._cache[uid] = (dev.FriendlyName or uid, dev.EndpointVolume)
                seen[uid] = self._cache[uid]
        except COMError as e:
            raise BackendError(f"endpoint enumeration failed: {e}") from e
        self._cache = seen
        return seen

    def devices(self):
        out = []
        for uid, (name, vol) in self._endpoints().items():
            try:
                muted = bool(vol.GetMute()) or vol.GetMasterVolumeLevelScalar() == 0.0
                out.append(InputDevice(uid=uid, name=name, live=not muted))
            except COMError:
                out.append(InputDevice(uid=uid, name=name, live=True, controllable=False))
        return out

    def _vol(self, uid):
        entry = self._cache.get(uid)
        if entry is None:
            raise BackendError(f"endpoint {uid} disappeared")
        return entry[1]

    def _mute_device(self, uid):
        vol = self._vol(uid)
        try:
            before = {"mute": bool(vol.GetMute()), "volume": float(vol.GetMasterVolumeLevelScalar())}
            touched = False
            if not before["mute"]:
                vol.SetMute(1, None)
                touched = True
            if before["volume"] != 0.0:
                vol.SetMasterVolumeLevelScalar(0.0, None)
                touched = True
        except COMError as e:
            raise BackendError(f"mute {uid}: {e}") from e
        return before, touched

    def _restore_device(self, uid, saved):
        vol = self._vol(uid)
        try:
            vol.SetMasterVolumeLevelScalar(float(saved.get("volume", RESTORE_FALLBACK_VOLUME)), None)
            vol.SetMute(1 if saved.get("mute") else 0, None)
        except COMError as e:
            raise BackendError(f"restore {uid}: {e}") from e

    def _open_device(self, uid):
        vol = self._vol(uid)
        try:
            if vol.GetMasterVolumeLevelScalar() == 0.0:
                vol.SetMasterVolumeLevelScalar(RESTORE_FALLBACK_VOLUME, None)
            vol.SetMute(0, None)
        except COMError as e:
            raise BackendError(f"unmute {uid}: {e}") from e
