"""Executable entry point and offline bundle verification for release builds."""
import sys


if __name__ == '__main__':
    if '--self-test' in sys.argv:
        from portable_check import main
        raise SystemExit(main())
    from app import main
    raise SystemExit(main())
