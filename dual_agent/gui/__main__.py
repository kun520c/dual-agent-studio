"""启动协作控制台。命令行协调器仍然是 python -m dual_agent。"""

from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("未安装 PySide6。请在项目虚拟环境中执行：")
        print(r".venv\Scripts\python.exe -m pip install -r requirements.txt")
        return 1
    if "--self-check" in args:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("协作控制台")
    app.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI", 10)
    app.setFont(font)
    from dual_agent.gui.adapter import ConsoleAdapter
    from dual_agent.gui.window import MainWindow

    window = MainWindow(ConsoleAdapter())
    if "--self-check" in args:
        window.interactive = False
        window.show()
        app.processEvents()
        ok = window.windowTitle() == "协作控制台" and window.start_button.isEnabled()
        window.close()
        app.processEvents()
        return 0 if ok else 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
