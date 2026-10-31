"""Native desktop entry point. Importing this module does not load Qt/models."""

from __future__ import annotations

import sys


def main() -> int:
    from .paths import load_environment

    load_environment()
    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
        from .desktop_client import DesktopController
        from .paths import asset_path
    except ImportError as exc:
        print(
            "桌面依赖未安装，请运行 python -m pip install -r requirements-desktop.txt。\n"
            + str(exc),
            file=sys.stderr,
        )
        return 1
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Open JARVIS")
    app.setOrganizationName("ecojust")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(QIcon(str(asset_path("assets/branding/open-jarvis-sheep.png"))))
    controller = DesktopController(app)
    app.jarvis_controller = controller
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
