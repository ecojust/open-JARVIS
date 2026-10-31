"""Frozen entry point; subprocess roles use the console worker executable."""

import multiprocessing


if __name__ == "__main__":
    multiprocessing.freeze_support()
    from jarvis.__main__ import main

    raise SystemExit(main())
