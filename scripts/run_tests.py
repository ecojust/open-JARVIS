"""Run the suite and expose actionable failures in GitHub Checks annotations."""

from pathlib import Path
import os
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def annotation(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if os.getenv("GITHUB_ACTIONS") == "true":
        for test, detail in [*result.errors, *result.failures]:
            print(
                "::error title="
                + annotation(test.id())
                + "::"
                + annotation(detail[-6000:]),
                flush=True,
            )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
