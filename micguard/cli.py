"""Headless commands, same backends as the GUI.

  micguard-cli status    # every input and whether it's live   (exit 1 if any live)
  micguard-cli mute      # mute all inputs, save levels         (exit 2 if any uncontrollable)
  micguard-cli restore   # put saved levels back
  micguard-cli watch     # mute now, re-mute on every change until Ctrl-C
"""
import argparse
import sys
import time

from .backends import BackendError, load_backend


def cmd_status(b):
    live = False
    for d in b.devices():
        tag = "LIVE?" if not d.controllable else ("LIVE" if d.live else "muted")
        live |= d.live or not d.controllable
        print(f"  {tag:<6}  {d.name}" + (f" [{d.detail}]" if d.detail else ""))
    return 1 if live else 0


def cmd_mute(b):
    _, uncontrollable = b.mute_all()
    for d in b.devices():
        if d.controllable:
            print(f"  muted   {d.name}")
    for name in uncontrollable:
        print(f"  CANNOT  {name}: no software control, unplug it", file=sys.stderr)
    return 2 if uncontrollable else 0


def cmd_restore(b):
    for name in b.restore_all():
        print(f"  opened  {name}")
    return 0


def cmd_watch(b, interval):
    cmd_mute(b)
    print(f"watching every {interval}s, Ctrl-C to stop (inputs stay muted; run 'restore' to undo)")
    try:
        while True:
            time.sleep(interval)
            changed, _ = b.mute_all()
            for name in changed:
                print(f"  {time.strftime('%H:%M:%S')}  re-muted {name}")
    except KeyboardInterrupt:
        print()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="micguard-cli", description="Mute every audio input at the device level.")
    p.add_argument("command", choices=["status", "mute", "restore", "watch"])
    p.add_argument("--interval", type=float, default=1.0, help="watch interval in seconds")
    args = p.parse_args(argv)
    try:
        b = load_backend()
        print(f"backend: {b.name}")
        if args.command == "status":
            return cmd_status(b)
        if args.command == "mute":
            return cmd_mute(b)
        if args.command == "restore":
            return cmd_restore(b)
        return cmd_watch(b, args.interval)
    except BackendError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
