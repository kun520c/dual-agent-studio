"""Windows 图形控制台。只调用协调器已有入口，不重写运行逻辑。"""

__all__ = ["ConsoleAdapter", "MainWindow"]


def __getattr__(name: str):
    if name == "ConsoleAdapter":
        from dual_agent.gui.adapter import ConsoleAdapter

        return ConsoleAdapter
    if name == "MainWindow":
        from dual_agent.gui.window import MainWindow

        return MainWindow
    raise AttributeError(name)
