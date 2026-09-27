"""python -m micguard            -> GUI
python -m micguard status     -> CLI (status | mute | restore | watch)"""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("status", "mute", "restore", "watch"):
        from .cli import main as cli_main
        sys.exit(cli_main())
    from .app import main
    main()
