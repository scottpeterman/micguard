"""macOS backend: CoreAudio device properties via ctypes. No dependencies."""
import ctypes
import struct
from ctypes import POINTER, Structure, byref, c_char_p, c_float, c_int32, c_long, c_ubyte, c_uint32, c_void_p, sizeof

from .base import RESTORE_FALLBACK_VOLUME, Backend, BackendError, InputDevice

CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


def fourcc(s):
    return struct.unpack(">I", s.encode("ascii"))[0]


def fourcc_str(v):
    try:
        return struct.pack(">I", v).decode("ascii").strip()
    except (struct.error, UnicodeDecodeError):
        return str(v)


SYSTEM_OBJECT = 1
SEL_DEVICES = fourcc("dev#")
SEL_STREAMS = fourcc("stm#")
SEL_NAME = fourcc("lnam")
SEL_UID = fourcc("uid ")
SEL_TRANSPORT = fourcc("tran")
SEL_MUTE = fourcc("mute")
SEL_VOLUME = fourcc("volm")
SCOPE_GLOBAL = fourcc("glob")
SCOPE_INPUT = fourcc("inpt")
ELEMENT_MAIN = 0
MAX_ELEMENT = 16
SKIP_TRANSPORTS = {fourcc("virt"), fourcc("grup")}  # virtual + aggregate devices
CF_UTF8 = 0x08000100


class Addr(Structure):
    _fields_ = [("mSelector", c_uint32), ("mScope", c_uint32), ("mElement", c_uint32)]


CA.AudioObjectHasProperty.argtypes = [c_uint32, POINTER(Addr)]
CA.AudioObjectHasProperty.restype = c_ubyte
CA.AudioObjectIsPropertySettable.argtypes = [c_uint32, POINTER(Addr), POINTER(c_ubyte)]
CA.AudioObjectIsPropertySettable.restype = c_int32
CA.AudioObjectGetPropertyDataSize.argtypes = [c_uint32, POINTER(Addr), c_uint32, c_void_p, POINTER(c_uint32)]
CA.AudioObjectGetPropertyDataSize.restype = c_int32
CA.AudioObjectGetPropertyData.argtypes = [c_uint32, POINTER(Addr), c_uint32, c_void_p, POINTER(c_uint32), c_void_p]
CA.AudioObjectGetPropertyData.restype = c_int32
CA.AudioObjectSetPropertyData.argtypes = [c_uint32, POINTER(Addr), c_uint32, c_void_p, c_uint32, c_void_p]
CA.AudioObjectSetPropertyData.restype = c_int32
CF.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
CF.CFStringGetCString.restype = c_ubyte
CF.CFRelease.argtypes = [c_void_p]
CF.CFRelease.restype = None


def addr(sel, scope=SCOPE_GLOBAL, elem=ELEMENT_MAIN):
    return Addr(sel, scope, elem)


def has(obj, a):
    return bool(CA.AudioObjectHasProperty(obj, byref(a)))


def settable(obj, a):
    out = c_ubyte(0)
    return CA.AudioObjectIsPropertySettable(obj, byref(a), byref(out)) == 0 and bool(out.value)


def get_size(obj, a):
    size = c_uint32(0)
    err = CA.AudioObjectGetPropertyDataSize(obj, byref(a), 0, None, byref(size))
    return size.value if err == 0 else 0


def get_scalar(obj, a, ctype):
    val = ctype()
    size = c_uint32(sizeof(val))
    err = CA.AudioObjectGetPropertyData(obj, byref(a), 0, None, byref(size), byref(val))
    if err:
        raise BackendError(f"get '{fourcc_str(a.mSelector)}' elem {a.mElement} on device {obj}: OSStatus {err}")
    return val.value


def set_scalar(obj, a, ctype, value):
    val = ctype(value)
    err = CA.AudioObjectSetPropertyData(obj, byref(a), 0, None, sizeof(val), byref(val))
    if err:
        raise BackendError(f"set '{fourcc_str(a.mSelector)}' elem {a.mElement} on device {obj}: OSStatus {err}")


def get_cfstring(obj, a):
    ref = c_void_p()
    size = c_uint32(sizeof(ref))
    if CA.AudioObjectGetPropertyData(obj, byref(a), 0, None, byref(size), byref(ref)) or not ref.value:
        return "?"
    buf = ctypes.create_string_buffer(1024)
    ok = CF.CFStringGetCString(ref, buf, len(buf), CF_UTF8)
    CF.CFRelease(ref)
    return buf.value.decode("utf-8", "replace") if ok else "?"


def device_ids():
    a = addr(SEL_DEVICES)
    n = get_size(SYSTEM_OBJECT, a) // sizeof(c_uint32)
    if n == 0:
        return []
    arr = (c_uint32 * n)()
    size = c_uint32(sizeof(arr))
    err = CA.AudioObjectGetPropertyData(SYSTEM_OBJECT, byref(a), 0, None, byref(size), arr)
    if err:
        raise BackendError(f"device list: OSStatus {err}")
    return list(arr)[: size.value // sizeof(c_uint32)]


def controls(dev):
    found = []
    for elem in range(MAX_ELEMENT + 1):
        for sel, ctype in ((SEL_MUTE, c_uint32), (SEL_VOLUME, c_float)):
            a = addr(sel, SCOPE_INPUT, elem)
            if has(dev, a) and settable(dev, a):
                found.append((sel, elem, ctype))
    return found


def is_muted(dev, ctl):
    vols = {}
    for sel, elem, ctype in ctl:
        v = get_scalar(dev, addr(sel, SCOPE_INPUT, elem), ctype)
        if sel == SEL_MUTE and v == 1:
            return True
        if sel == SEL_VOLUME:
            vols[elem] = v
    if ELEMENT_MAIN in vols:
        return vols[ELEMENT_MAIN] == 0.0
    return bool(vols) and all(v == 0.0 for v in vols.values())


class CoreAudioBackend(Backend):
    name = "coreaudio"

    def __init__(self):
        super().__init__()
        self._ids = {}       # uid -> AudioObjectID (IDs change across replug; UIDs don't)
        self._controls = {}  # AudioObjectID -> [(sel, elem, ctype)]

    def devices(self):
        out, ids = [], {}
        for dev in device_ids():
            if get_size(dev, addr(SEL_STREAMS, SCOPE_INPUT)) == 0:
                continue
            transport = get_scalar(dev, addr(SEL_TRANSPORT), c_uint32) if has(dev, addr(SEL_TRANSPORT)) else 0
            if transport in SKIP_TRANSPORTS:
                continue
            uid = get_cfstring(dev, addr(SEL_UID))
            ids[uid] = dev
            if dev not in self._controls:
                self._controls[dev] = controls(dev)
            ctl = self._controls[dev]
            out.append(InputDevice(
                uid=uid,
                name=get_cfstring(dev, addr(SEL_NAME)),
                live=bool(ctl) and not is_muted(dev, ctl),
                controllable=bool(ctl),
                detail=fourcc_str(transport) if transport else "?",
            ))
        self._ids = ids
        self._controls = {d: c for d, c in self._controls.items() if d in ids.values()}
        return out

    def _dev(self, uid):
        dev = self._ids.get(uid)
        if dev is None:
            raise BackendError(f"device {uid} disappeared")
        return dev, self._controls[dev]

    def _mute_device(self, uid):
        dev, ctl = self._dev(uid)
        before, touched = {}, False
        for sel, elem, ctype in ctl:
            a = addr(sel, SCOPE_INPUT, elem)
            cur = get_scalar(dev, a, ctype)
            before[f"{sel}:{elem}"] = cur
            target = 1 if sel == SEL_MUTE else 0.0
            if cur != target:
                set_scalar(dev, a, ctype, target)
                touched = True
        return before, touched

    def _restore_device(self, uid, saved):
        dev, _ = self._dev(uid)
        for key, val in saved.items():
            sel, elem = (int(x) for x in key.split(":"))
            ctype = c_uint32 if sel == SEL_MUTE else c_float
            a = addr(sel, SCOPE_INPUT, elem)
            if has(dev, a):
                set_scalar(dev, a, ctype, val)

    def _open_device(self, uid):
        dev, ctl = self._dev(uid)
        for sel, elem, ctype in ctl:
            a = addr(sel, SCOPE_INPUT, elem)
            if sel == SEL_MUTE:
                set_scalar(dev, a, ctype, 0)
            elif get_scalar(dev, a, ctype) == 0.0:
                set_scalar(dev, a, ctype, RESTORE_FALLBACK_VOLUME)
