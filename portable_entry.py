"""Executable entry point and offline bundle verification for release builds."""
import sys


if __name__ == '__main__':
    if '--activate-instance' in sys.argv:
        from portable_check import activate_instance_check
        raise SystemExit(activate_instance_check())
    if '--startup-check' in sys.argv:
        from portable_check import startup_check
        raise SystemExit(startup_check())
    if '--self-test' in sys.argv:
        from portable_check import main
        raise SystemExit(main())
    from app import main
    raise SystemExit(main())
