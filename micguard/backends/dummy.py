"""In-memory backend for UI development and tests: MICGUARD_BACKEND=dummy."""
from .base import RESTORE_FALLBACK_VOLUME, Backend, InputDevice


class DummyBackend(Backend):
    name = "dummy"

    def __init__(self):
        super().__init__()
        self.saved = {}
        self.devs = {
            "builtin": {"name": "Built-in Microphone", "mute": False, "volume": 0.6},
            "usb": {"name": "USB Webcam", "mute": False, "volume": 0.8},
        }

    def _save(self):
        pass

    def devices(self):
        return [
            InputDevice(uid=u, name=d["name"], live=not (d["mute"] or d["volume"] == 0.0))
            for u, d in self.devs.items()
        ]

    def _mute_device(self, uid):
        d = self.devs[uid]
        before = {"mute": d["mute"], "volume": d["volume"]}
        touched = not d["mute"] or d["volume"] != 0.0
        d["mute"], d["volume"] = True, 0.0
        return before, touched

    def _restore_device(self, uid, saved):
        self.devs[uid].update(saved)

    def _open_device(self, uid):
        d = self.devs[uid]
        d["mute"] = False
        if d["volume"] == 0.0:
            d["volume"] = RESTORE_FALLBACK_VOLUME

    # test hook: simulate another app raising the level
    def external_unmute(self, uid, volume=0.5):
        self.devs[uid].update(mute=False, volume=volume)
