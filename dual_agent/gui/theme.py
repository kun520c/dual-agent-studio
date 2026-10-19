"""深色与浅色主题。配色独立于任何第三方产品标识。"""

from __future__ import annotations

THEMES = {
    "dark": {
        "window": "#141618",
        "top": "#1a1d21",
        "sidebar": "#191c20",
        "center": "#121416",
        "card": "#22262c",
        "input": "#1b1f24",
        "border": "#31373f",
        "text": "#e8e4db",
        "muted": "#9aa3ad",
        "accent": "#6f9784",
        "accent_text": "#f3f7f4",
        "danger": "#c48b7e",
        "ok": "#8fbfa3",
        "warn": "#d2b48c",
        "select": "#2c3833",
        "scroll": "#3a414a",
    },
    "light": {
        "window": "#f3f1ec",
        "top": "#ebe7e0",
        "sidebar": "#e7e2da",
        "center": "#f7f6f3",
        "card": "#fffcf8",
        "input": "#ffffff",
        "border": "#ddd4c8",
        "text": "#1e2428",
        "muted": "#5c6770",
        "accent": "#3e6b58",
        "accent_text": "#f7fbf8",
        "danger": "#9a4e42",
        "ok": "#2f6b4f",
        "warn": "#8a5a12",
        "select": "#dce8e2",
        "scroll": "#c9c1b6",
    },
}


def stylesheet(name: str) -> str:
    color = THEMES[name]
    return f"""
    QWidget {{
        color: {color["text"]};
        font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
        font-size: 13px;
    }}
    QMainWindow, QWidget#windowRoot {{
        background: {color["window"]};
    }}
    QWidget#topBar {{
        background: {color["top"]};
        border-bottom: 1px solid {color["border"]};
    }}
    QWidget#sidebar, QWidget#inspector {{
        background: {color["sidebar"]};
    }}
    QWidget#centerPane {{
        background: {color["center"]};
    }}
    QWidget#composer {{
        background: {color["card"]};
        border: 1px solid {color["border"]};
        border-radius: 12px;
    }}
    QLabel#appTitle {{
        font-size: 18px;
        font-weight: 650;
        background: transparent;
    }}
    QLabel#appHint, QLabel#sectionLabel, QLabel#mutedLabel {{
        color: {color["muted"]};
        background: transparent;
    }}
    QLabel#phaseLabel {{
        font-size: 15px;
        font-weight: 650;
        background: transparent;
    }}
    QLabel#banner {{
        background: transparent;
        color: {color["muted"]};
        padding: 2px 2px 6px 2px;
    }}
    QLabel#banner[kind="error"] {{
        color: {color["danger"]};
    }}
    QLabel#banner[kind="ok"] {{
        color: {color["ok"]};
    }}
    QLabel#welcome {{
        color: {color["muted"]};
        font-size: 14px;
        background: transparent;
        padding: 12px;
    }}
    QFrame[card="true"] {{
        background: {color["card"]};
        border: 1px solid {color["border"]};
        border-radius: 10px;
    }}
    QListWidget, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QTableWidget {{
        background: {color["input"]};
        border: 1px solid {color["border"]};
        border-radius: 8px;
        padding: 4px;
        selection-background-color: {color["select"]};
        selection-color: {color["text"]};
    }}
    QListWidget::item {{
        padding: 6px 8px;
        border-radius: 6px;
    }}
    QListWidget::item:selected {{
        background: {color["select"]};
    }}
    QPushButton {{
        background: transparent;
        border: 1px solid {color["border"]};
        border-radius: 8px;
        padding: 6px 12px;
    }}
    QPushButton:hover {{
        background: {color["select"]};
    }}
    QPushButton:disabled {{
        color: {color["muted"]};
    }}
    QPushButton#primaryButton {{
        background: {color["accent"]};
        color: {color["accent_text"]};
        border: none;
        font-weight: 650;
        padding: 8px 16px;
    }}
    QPushButton#primaryButton:hover {{
        background: {color["accent"]};
    }}
    QPushButton#dangerButton {{
        color: {color["danger"]};
    }}
    QTabWidget::pane {{
        border: 1px solid {color["border"]};
        border-radius: 8px;
        background: {color["center"]};
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        padding: 7px 12px;
        margin-right: 2px;
        color: {color["muted"]};
    }}
    QTabBar::tab:selected {{
        color: {color["text"]};
        border-bottom: 2px solid {color["accent"]};
    }}
    QSplitter::handle {{
        background: {color["border"]};
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {color["scroll"]};
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        height: 0;
    }}
    QCheckBox, QRadioButton {{
        background: transparent;
        spacing: 6px;
    }}
    QToolTip {{
        background: {color["card"]};
        color: {color["text"]};
        border: 1px solid {color["border"]};
        padding: 4px;
    }}
    """
