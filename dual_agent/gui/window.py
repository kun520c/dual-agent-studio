"""协作控制台主窗口。三栏布局，状态只读取协调器已经写下的文件。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dual_agent.gui.adapter import ConsoleAdapter
from dual_agent.gui.dialogs import ProjectDialog
from dual_agent.gui.outcome import presentation
from dual_agent.gui.theme import THEMES, stylesheet
from dual_agent.models import ConfigError
from dual_agent.report import reason_zh
from dual_agent.status import status_from_result_dict

STATE_LABELS = {
    "queued": "排队",
    "running": "运行",
    "verifying": "验证",
    "reviewing": "审查",
    "repairing": "修复",
    "verified": "完成",
    "failed": "失败",
    "cancelled": "已取消",
}
ROLE_ZH = {
    "developer": "开发者",
    "reviewer": "审查者",
    "diagnoser": "诊断者",
    "acceptor": "验收者",
}
EXECUTOR_ZH = {"mock": "模拟", "codex": "Codex", "cursor": "Cursor"}
DECISION_ZH = {"continue": "继续", "accept": "接受", "reject": "拒绝", "blocked": "阻塞"}
ACTIVE_STATES = {"running", "verifying", "reviewing", "repairing"}
SAFETY_TEXT = (
    "默认只读。真实 Codex 写入必须同时满足配置 allow_real_codex 和子进程环境变量 "
    "DUAL_AGENT_ALLOW_CODEX=1。只打开其中一个不会调用 Codex。\n"
    "workspace-write 覆盖整个工作区。write_paths 是调度锁，不能把写入收窄到子目录。\n"
    "Worktree 只是 Git 隔离工作树，不是操作系统沙箱。\n"
    "Cursor Ask 模式也不是操作系统沙箱。真实 Cursor 开发者不会被无人值守启动。\n"
    "任务文本只写入 JSON，不会当作 Shell 命令执行。\n"
    "关闭窗口不会把运行中的任务标成成功。"
)


class MainWindow(QMainWindow):
    def __init__(self, adapter: ConsoleAdapter) -> None:
        super().__init__()
        self.adapter = adapter
        self.interactive = True
        self.theme_name = "dark"
        self.pending: list[dict[str, Any]] = []
        self.latest_schedules: list[dict[str, Any]] = []
        self._projects: dict[str, dict[str, Any]] = {}
        self._task_payloads: dict[str, dict[str, Any]] = {}
        self._histories: dict[str, list[dict[str, Any]]] = {}
        self._process_rows: list[dict[str, Any]] = []
        self._applied_project_id = ""
        self._force_form = False
        self._syncing = False
        self._closing = False
        self._follow_ids: list[str] = []
        self._starting = False
        self._polling = False
        self._load_key: tuple[str, float, str] | None = None
        self._signature: tuple[Any, ...] | None = None
        self._view: dict[str, Any] = {}
        self._pinned_round = ""
        self._transcript = ""
        self._right_width = 340
        self._want_history = True
        self.setWindowTitle("协作控制台")
        self.resize(1280, 820)
        self.setMinimumSize(960, 640)
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self._ticks = 0
        self.timer.timeout.connect(self._on_timer)
        self.timer.start()
        self.apply_theme("dark")
        self._poll(history=True)
        QShortcut(QKeySequence("Ctrl+Return"), self, self.start_current)

    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("windowRoot")
        central.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_top())
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        self.splitter.addWidget(self._build_sidebar())
        self.splitter.addWidget(self._build_center())
        self.splitter.addWidget(self._build_inspector())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setCollapsible(2, True)
        self.splitter.setSizes([300, 680, 340])
        root.addWidget(self.splitter, 1)

    def _build_top(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("topBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 10, 16, 10)
        titles = QVBoxLayout()
        title = QLabel("协作控制台")
        title.setObjectName("appTitle")
        hint = QLabel("本地双 Agent 协调。终端入口仍可使用 启动.ps1。")
        hint.setObjectName("appHint")
        titles.addWidget(title)
        titles.addWidget(hint)
        layout.addLayout(titles, 1)
        self.env_chip = QLabel()
        self.env_chip.setObjectName("mutedLabel")
        layout.addWidget(self.env_chip)
        self.theme_button = QPushButton("浅色")
        self.theme_button.clicked.connect(self.toggle_theme)
        self.right_button = QPushButton("折叠右栏")
        self.right_button.clicked.connect(self.toggle_right)
        layout.addWidget(self.theme_button)
        layout.addWidget(self.right_button)
        self._refresh_env_chip()
        return bar

    def _build_sidebar(self) -> QWidget:
        panel = self._pane("sidebar")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._section("项目"))
        self.project_list = QListWidget()
        self.project_list.setObjectName("projectList")
        self.project_list.currentItemChanged.connect(lambda *_: self._on_project_list())
        layout.addWidget(self.project_list, 2)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("添加")
        self.edit_button = QPushButton("修改")
        self.disable_button = QPushButton("禁用")
        self.add_button.clicked.connect(self.add_project)
        self.edit_button.clicked.connect(self.edit_selected)
        self.disable_button.clicked.connect(self.toggle_enabled)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.disable_button)
        layout.addLayout(buttons)
        layout.addWidget(self._section("待启动"))
        self.pending_list = QListWidget()
        self.pending_list.setObjectName("pendingList")
        self.pending_list.setMaximumHeight(110)
        layout.addWidget(self.pending_list)
        remove = QPushButton("移出队列")
        remove.clicked.connect(self.remove_pending)
        layout.addWidget(remove)
        layout.addWidget(self._section("任务"))
        self.task_list = QListWidget()
        self.task_list.setObjectName("taskList")
        self.task_list.currentItemChanged.connect(lambda *_: self._on_task_changed())
        layout.addWidget(self.task_list, 3)
        return panel

    def _build_center(self) -> QWidget:
        panel = self._pane("centerPane")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        self.task_heading = QLabel("选择项目后描述任务")
        self.task_heading.setObjectName("phaseLabel")
        self.phase_label = QLabel("当前阶段：未选择")
        self.phase_label.setObjectName("phaseLabel")
        self.agent_label = QLabel("Agent 状态会从协调器事件读取。")
        self.agent_label.setObjectName("mutedLabel")
        self.agent_label.setWordWrap(True)
        titles.addWidget(self.task_heading)
        titles.addWidget(self.phase_label)
        titles.addWidget(self.agent_label)
        header.addLayout(titles, 1)
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(self.cancel_selected)
        header.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.conversation_host = QWidget()
        self.conversation_layout = QVBoxLayout(self.conversation_host)
        self.conversation_layout.setContentsMargins(0, 0, 0, 0)
        self.conversation_layout.setSpacing(8)
        self.welcome = QLabel(
            "在下面用自然语言写下任务。执行过程、工具结果和阶段都会出现在这里。\n"
            "进度只反映协调器的真实状态，没有演示用的进度条。"
        )
        self.welcome.setObjectName("welcome")
        self.welcome.setWordWrap(True)
        self.conversation_layout.addWidget(self.welcome)
        self.conversation_layout.addStretch(1)
        self.scroll.setWidget(self.conversation_host)
        layout.addWidget(self.scroll, 1)
        self.banner = QLabel("就绪。默认只读，任务文本不会进入 Shell。")
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)
        layout.addWidget(self._build_composer())
        return panel

    def _build_composer(self) -> QWidget:
        box = QWidget()
        box.setObjectName("composer")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        top = QHBoxLayout()
        self.project_combo = QComboBox()
        self.project_combo.setObjectName("projectCombo")
        self.project_combo.currentIndexChanged.connect(self._on_combo)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("标题，可留空")
        self.title_edit.setObjectName("titleEdit")
        top.addWidget(self.project_combo, 1)
        top.addWidget(self.title_edit, 1)
        layout.addLayout(top)
        self.goal_edit = QPlainTextEdit()
        self.goal_edit.setObjectName("goalEdit")
        self.goal_edit.setPlaceholderText("用自然语言描述要做的事。这里的文字不会被当作命令执行。")
        self.goal_edit.setFixedHeight(78)
        layout.addWidget(self.goal_edit)
        roles = QHBoxLayout()
        self.role_list = QListWidget()
        self.role_list.setObjectName("roleList")
        self.role_list.setMaximumHeight(96)
        self.role_list.itemChanged.connect(lambda *_: self._sync_executor_hint())
        roles.addWidget(self.role_list, 1)
        move = QVBoxLayout()
        up = QPushButton("上移")
        down = QPushButton("下移")
        up.clicked.connect(lambda: self._move_role(-1))
        down.clicked.connect(lambda: self._move_role(1))
        move.addWidget(up)
        move.addWidget(down)
        move.addStretch(1)
        roles.addLayout(move)
        layout.addLayout(roles)
        self.executor_hint = QLabel()
        self.executor_hint.setObjectName("mutedLabel")
        self.executor_hint.setWordWrap(True)
        layout.addWidget(self.executor_hint)
        settings = QHBoxLayout()
        self.write_read = QRadioButton("只读")
        self.write_controlled = QRadioButton("受控写入")
        self.write_read.setChecked(True)
        self.write_group = QButtonGroup(self)
        self.write_group.addButton(self.write_read)
        self.write_group.addButton(self.write_controlled)
        self.write_read.toggled.connect(self._sync_write_panel)
        settings.addWidget(self.write_read)
        settings.addWidget(self.write_controlled)
        self.calls_spin = self._spin(1, 30, 8)
        self.timeout_spin = self._spin(1, 3600, 120)
        self.repair_spin = self._spin(0, 5, 1)
        self.rounds_spin = self._spin(1, 100, 6)
        self.concurrent_spin = self._spin(1, 2, 2)
        self.calls_spin.setObjectName("callsSpin")
        self.timeout_spin.setObjectName("timeoutSpin")
        self.repair_spin.setObjectName("repairSpin")
        self.rounds_spin.setObjectName("roundsSpin")
        self.concurrent_spin.setObjectName("concurrentSpin")
        settings.addLayout(self._labeled("调用上限", self.calls_spin))
        settings.addLayout(self._labeled("超时（秒）", self.timeout_spin))
        settings.addLayout(self._labeled("修复轮数", self.repair_spin))
        settings.addLayout(self._labeled("最大轮次", self.rounds_spin))
        settings.addLayout(self._labeled("同时运行", self.concurrent_spin))
        layout.addLayout(settings)
        self.worktree_box = QCheckBox("本任务使用 Worktree")
        self.worktree_box.setObjectName("worktreeBox")
        self.worktree_hint = QLabel("Worktree 只是 Git 隔离工作树，不是操作系统沙箱。路径和写入检查仍然由协调器执行。")
        self.worktree_hint.setObjectName("mutedLabel")
        self.worktree_hint.setWordWrap(True)
        layout.addWidget(self.worktree_box)
        layout.addWidget(self.worktree_hint)
        self.write_panel = QWidget()
        write_layout = QVBoxLayout(self.write_panel)
        write_layout.setContentsMargins(0, 0, 0, 0)
        self.scope_label = QLabel()
        self.scope_label.setWordWrap(True)
        self.codex_switch = QCheckBox("配置开关：允许真实 Codex（allow_real_codex）")
        self.cursor_switch = QCheckBox("配置开关：允许真实 Cursor（allow_real_cursor）")
        self.env_switch = QCheckBox("环境开关：仅本次子进程设置 DUAL_AGENT_ALLOW_CODEX=1")
        self.codex_switch.setObjectName("codexSwitch")
        self.cursor_switch.setObjectName("cursorSwitch")
        self.env_switch.setObjectName("envSwitch")
        for widget in (self.scope_label, self.codex_switch, self.cursor_switch, self.env_switch):
            write_layout.addWidget(widget)
        self.write_panel.setVisible(False)
        layout.addWidget(self.write_panel)
        self.mock_label = QLabel("模拟场景（可选，仅模拟执行器，不会调用真实模型）")
        self.mock_label.setObjectName("mutedLabel")
        self.mock_edit = QLineEdit()
        self.mock_edit.setObjectName("mockEdit")
        self.mock_edit.setPlaceholderText("例如 ok,ok,accept。留空则每轮使用 ok。")
        layout.addWidget(self.mock_label)
        layout.addWidget(self.mock_edit)
        actions = QHBoxLayout()
        self.queue_button = QPushButton("加入队列")
        self.queue_button.setObjectName("queueButton")
        self.queue_button.clicked.connect(self.queue_current)
        self.start_button = QPushButton("开始协作")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_current)
        actions.addWidget(self.queue_button)
        actions.addStretch(1)
        actions.addWidget(self.start_button)
        layout.addLayout(actions)
        return box

    def _build_inspector(self) -> QWidget:
        panel = self._pane("inspector")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        self.tabs = QTabWidget()
        self.diff_view = self._reader("diffView")
        self.verify_view = self._reader("verifyView")
        self.log_view = self._reader("logView")
        self.report_view = self._reader("reportView")
        self.permission_view = self._reader("permissionView")
        self.tabs.addTab(self.diff_view, "差异")
        self.tabs.addTab(self.verify_view, "验证")
        self.tabs.addTab(self.log_view, "日志")
        self.tabs.addTab(self.report_view, "验收")
        self.tabs.addTab(self.permission_view, "权限")
        layout.addWidget(self.tabs)
        self._set_text(self.permission_view, SAFETY_TEXT)
        return panel

    def _pane(self, name: str) -> QWidget:
        panel = QWidget()
        panel.setObjectName(name)
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        return panel

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        return label

    def _reader(self, name: str) -> QPlainTextEdit:
        view = QPlainTextEdit()
        view.setObjectName(name)
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        return view

    def _spin(self, low: int, high: int, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        return spin

    def _labeled(self, title: str, widget: QWidget) -> QVBoxLayout:
        column = QVBoxLayout()
        label = QLabel(title)
        label.setObjectName("mutedLabel")
        column.addWidget(label)
        column.addWidget(widget)
        return column

    def apply_theme(self, name: str) -> None:
        self.theme_name = name
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(stylesheet(name))
        self.theme_button.setText("浅色" if name == "dark" else "深色")

    def toggle_theme(self) -> None:
        self.apply_theme("light" if self.theme_name == "dark" else "dark")

    def toggle_right(self) -> None:
        sizes = self.splitter.sizes()
        if len(sizes) < 3:
            return
        if sizes[2] == 0:
            width = self._right_width
            center = max(sizes[1] - width, 420)
            self.splitter.setSizes([sizes[0], center, width])
            self.right_button.setText("折叠右栏")
        else:
            self._right_width = max(sizes[2], 280)
            self.splitter.setSizes([sizes[0], sizes[1] + sizes[2], 0])
            self.right_button.setText("展开右栏")

    def _on_timer(self) -> None:
        self._ticks += 1
        self._poll(history=self._ticks % 8 == 0)

    def kick(self, *, history: bool | None = None) -> None:
        self._poll(history=True if history is None else history)

    def _poll(self, *, history: bool) -> None:
        if self._closing or self._polling:
            return
        self._polling = True
        try:
            payload = self.adapter.overview(history=history)
            self._projects = {item["project_id"]: item for item in payload.get("projects") or []}
            self.latest_schedules = list(payload.get("schedules") or [])
            if history:
                self._histories = dict(payload.get("histories") or {})
            self._process_rows = list(payload.get("processes") or [])
            self._fill_projects(list(payload.get("projects") or []))
            self._fill_tasks()
            self._refresh_env_chip()
            self._sync_running_button()
        except Exception as exc:
            self.show_error(f"{type(exc).__name__}: {exc}")
        finally:
            self._polling = False

    def _fill_projects(self, projects: list[dict[str, Any]]) -> None:
        current = self.project_combo.currentData()
        self.project_combo.blockSignals(True)
        self.project_list.blockSignals(True)
        self.project_combo.clear()
        self.project_list.clear()
        muted = QColor(THEMES[self.theme_name]["muted"])
        for project in projects:
            label = str(project.get("name") or project.get("project_id"))
            if not project.get("enabled", True):
                label += "  ·  已禁用"
            self.project_combo.addItem(label, project.get("project_id"))
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, project.get("project_id"))
            if not project.get("enabled", True):
                item.setForeground(muted)
            self.project_list.addItem(item)
        restored = False
        if current:
            for index in range(self.project_combo.count()):
                if self.project_combo.itemData(index) == current:
                    self.project_combo.setCurrentIndex(index)
                    restored = True
                    break
        self.project_combo.blockSignals(False)
        self.project_list.blockSignals(False)
        if restored:
            self._select_list_project(current)
        if self._force_form or (not self._applied_project_id and self.project_combo.count()):
            self._on_project_changed()
        self._sync_disable_button()

    def _fill_tasks(self) -> None:
        previous = ""
        current = self.task_list.currentItem()
        if current is not None:
            previous = str(current.data(Qt.ItemDataRole.UserRole) or "")
        self.task_list.blockSignals(True)
        self.task_list.clear()
        self._task_payloads = {}
        running = [row for row in self.latest_schedules if row.get("state") in ACTIVE_STATES]
        queued = [row for row in self.latest_schedules if row.get("state") == "queued"]
        finished = [row for row in self.latest_schedules if row.get("state") not in ACTIVE_STATES | {"queued"}]
        self._add_task_header("正在运行")
        self._add_schedule_rows(running)
        self._add_task_header("排队")
        self._add_schedule_rows(queued)
        self._add_task_header("最近结果")
        seen_runs = {str(row.get("run_dir") or "") for row in finished if row.get("run_dir")}
        self._add_schedule_rows(finished[:20])
        history_rows: list[dict[str, Any]] = []
        for rows in self._histories.values():
            history_rows.extend(rows)
        history_rows.sort(key=lambda item: str(item.get("finished_at") or ""), reverse=True)
        added = 0
        for row in history_rows:
            run_dir = str(row.get("run_dir") or "")
            if not run_dir or run_dir in seen_runs:
                continue
            seen_runs.add(run_dir)
            self._add_history_row(row)
            added += 1
            if added >= 20:
                break
        if self.task_list.count() <= 3:
            empty = QListWidgetItem("还没有任务")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.task_list.addItem(empty)
        restored = False
        if previous:
            restored = self._select_task_key(previous)
        if not restored and self._follow_ids:
            for task_id in self._follow_ids:
                if self._select_task_id(task_id):
                    restored = True
                    break
        self.task_list.blockSignals(False)
        payload = self.current_payload()
        if payload:
            run_dir = str(payload.get("run_dir") or "")
            if not run_dir or self._view.get("run_dir") != run_dir:
                self._show_payload(payload)
            if run_dir:
                self._maybe_load(run_dir, str(payload.get("state") or ""))

    def _add_task_header(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setData(Qt.ItemDataRole.UserRole, "")
        self.task_list.addItem(item)

    def _add_schedule_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            item = QListWidgetItem("无")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.task_list.addItem(item)
            return
        for row in rows:
            state = str(row.get("state") or "failed")
            if state not in STATE_LABELS:
                state = "failed"
            task_id = str(row.get("task_id") or "")
            title = str(row.get("title") or task_id or "未命名任务")
            key = "sch:" + str(row.get("schedule_id") or task_id)
            payload = {
                "kind": "schedule",
                "key": key,
                "schedule_id": row.get("schedule_id") or "",
                "task_id": task_id,
                "run_dir": row.get("run_dir") or "",
                "state": state,
                "stop_reason": str(row.get("stop_reason") or ""),
                "success": row.get("success"),
                "project_id": row.get("project_id") or "",
                "title": title,
                "detail": row.get("detail") or "",
                "batch_id": row.get("batch_id") or "",
            }
            self._task_payloads[key] = payload
            shown = presentation(state, str(row.get("stop_reason") or ""), success=row.get("success"))
            item = QListWidgetItem(f"{title}\n{shown['label']}  ·  {task_id}")
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setSizeHint(QSize(0, 72))
            self.task_list.addItem(item)

    def _add_history_row(self, row: dict[str, Any]) -> None:
        state = str(row.get("status") or "failed")
        if state not in STATE_LABELS:
            state = "failed"
        task_id = str(row.get("task_id") or "")
        run_dir = str(row.get("run_dir") or "")
        key = "run:" + run_dir
        payload = {
            "kind": "history",
            "key": key,
            "schedule_id": "",
            "task_id": task_id,
            "run_dir": run_dir,
            "state": state,
            "stop_reason": str(row.get("stop_reason") or ""),
            "success": row.get("success"),
            "project_id": row.get("project_id") or "",
            "title": task_id,
            "detail": row.get("error_message") or row.get("stop_reason") or "",
            "report_path": row.get("report_path") or "",
            "batch_id": "",
        }
        self._task_payloads[key] = payload
        shown = presentation(state, str(row.get("stop_reason") or ""), success=row.get("success"))
        item = QListWidgetItem(f"{task_id}\n{shown['label']}")
        item.setData(Qt.ItemDataRole.UserRole, key)
        self.task_list.addItem(item)

    def _select_task_key(self, key: str) -> bool:
        for index in range(self.task_list.count()):
            item = self.task_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == key:
                self.task_list.setCurrentItem(item)
                return True
        return False

    def _select_task_id(self, task_id: str) -> bool:
        for key, payload in self._task_payloads.items():
            if payload.get("task_id") == task_id:
                return self._select_task_key(key)
        return False

    def select_task_id(self, task_id: str) -> bool:
        return self._select_task_id(task_id)

    def current_payload(self) -> dict[str, Any] | None:
        item = self.task_list.currentItem()
        if item is None:
            return None
        key = item.data(Qt.ItemDataRole.UserRole)
        if not key:
            return None
        return self._task_payloads.get(str(key))

    def current_project(self) -> dict[str, Any] | None:
        project_id = self.project_combo.currentData()
        if not project_id:
            return None
        return self._projects.get(str(project_id))

    def select_project(self, project_id: str) -> bool:
        found = False
        self.project_combo.blockSignals(True)
        for index in range(self.project_combo.count()):
            if self.project_combo.itemData(index) == project_id:
                self.project_combo.setCurrentIndex(index)
                found = True
                break
        self.project_combo.blockSignals(False)
        self._select_list_project(project_id)
        if found:
            self._on_project_changed()
        return found

    def _select_list_project(self, project_id: object) -> None:
        self.project_list.blockSignals(True)
        for index in range(self.project_list.count()):
            item = self.project_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == project_id:
                self.project_list.setCurrentItem(item)
                break
        self.project_list.blockSignals(False)

    def _on_combo(self, _index: int) -> None:
        if self._syncing:
            return
        self._select_list_project(self.project_combo.currentData())
        self._on_project_changed()

    def _on_project_list(self) -> None:
        if self._syncing:
            return
        item = self.project_list.currentItem()
        if item is None:
            return
        project_id = item.data(Qt.ItemDataRole.UserRole)
        self._syncing = True
        self.project_combo.blockSignals(True)
        for index in range(self.project_combo.count()):
            if self.project_combo.itemData(index) == project_id:
                self.project_combo.setCurrentIndex(index)
                break
        self.project_combo.blockSignals(False)
        self._syncing = False
        self._on_project_changed()

    def _on_project_changed(self) -> None:
        project = self.current_project()
        if not project:
            return
        if project.get("project_id") == self._applied_project_id and not self._force_form:
            self._sync_disable_button()
            self._sync_executor_hint()
            return
        self._force_form = False
        self._apply_project_form(project)

    def _apply_project_form(self, project: dict[str, Any]) -> None:
        self._applied_project_id = str(project.get("project_id") or "")
        self.timeout_spin.setValue(int(float(project.get("timeout_seconds") or 120)))
        self.calls_spin.setValue(int(project.get("max_model_calls") or 8))
        self.repair_spin.setValue(int(project.get("max_repair_rounds") or 0))
        self.rounds_spin.setValue(int(project.get("max_rounds") or 6))
        self.worktree_box.setChecked(bool((project.get("worktree") or {}).get("enabled")))
        workspace = str(project.get("workspace") or "")
        self.scope_label.setText(
            "受控写入时，真实 Codex 的 workspace-write 覆盖整个工作区：\n"
            f"{workspace}\n"
            "这不是子目录白名单。配置开关和环境开关必须同时打开，协调器才会调用 Codex。"
        )
        self.role_list.clear()
        for item in project.get("participants") or []:
            executor = EXECUTOR_ZH.get(str(item.get("executor")), str(item.get("executor")))
            role = ROLE_ZH.get(str(item.get("role")), str(item.get("role")))
            row = QListWidgetItem(f"{item.get('id')}  ·  {executor}  ·  {role}")
            row.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(Qt.CheckState.Checked)
            row.setData(Qt.ItemDataRole.UserRole, item.get("id"))
            self.role_list.addItem(row)
        self._sync_executor_hint()
        self._sync_disable_button()
        self._refresh_permission_preview()

    def _sync_executor_hint(self) -> None:
        project = self.current_project() or {}
        participants = {item.get("id"): item for item in project.get("participants") or []}
        flow = self.checked_flow() or list(participants)
        selected = [participants[item] for item in flow if item in participants]
        all_mock = bool(selected) and all(item.get("executor") == "mock" for item in selected)
        self.mock_label.setVisible(all_mock)
        self.mock_edit.setVisible(all_mock)
        if not all_mock:
            self.mock_edit.clear()
        if not selected:
            self.executor_hint.setText("请选择本次要运行的角色。")
        elif all_mock:
            self.executor_hint.setText("当前分工使用模拟执行器，不会调用真实模型。")
        else:
            self.executor_hint.setText("分工里包含真实 Codex 或 Cursor。默认只读，协调器不会发出模型请求。")
        workspace = str(project.get("workspace") or "")
        has_developer = any(str(item.get("role")) == "developer" for item in selected)
        if has_developer:
            self.scope_label.setText(
                "受控写入时，真实 Codex 开发者的 workspace-write 覆盖整个工作区：\n"
                f"{workspace}\n"
                "这不是子目录白名单。配置开关和环境开关必须同时打开，协调器才会调用 Codex。"
            )
        else:
            self.scope_label.setText(
                "当前分工没有开发者。真实 Codex 使用 read-only，Cursor 使用 Ask。\n"
                f"工作区：{workspace}\n"
                "这不是操作系统沙箱。要调用模型仍需打开配置开关；Codex 还要打开环境开关。"
                "这种任务结束时不会显示成功能验证通过。"
            )
        self._refresh_permission_preview()

    def _sync_write_panel(self) -> None:
        self.write_panel.setVisible(self.write_controlled.isChecked())
        self._refresh_permission_preview()

    def _sync_disable_button(self) -> None:
        project = self.current_project()
        if project and not project.get("enabled", True):
            self.disable_button.setText("启用")
        else:
            self.disable_button.setText("禁用")

    def _sync_running_button(self) -> None:
        if self.adapter.has_live_process():
            self.start_button.setEnabled(False)
            self.start_button.setToolTip("已有协调器进程在运行")
        else:
            self.start_button.setEnabled(True)
            self.start_button.setToolTip("队列为空时启动输入框中的任务；有队列时只启动队列，最多两个。")

    def _refresh_env_chip(self) -> None:
        state = "已打开" if self.adapter.codex_env_enabled() else "关闭"
        self.env_chip.setText(f"当前窗口环境的 Codex 开关：{state}")

    def checked_flow(self) -> list[str]:
        found: list[str] = []
        for index in range(self.role_list.count()):
            item = self.role_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                found.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return found

    def _move_role(self, delta: int) -> None:
        row = self.role_list.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self.role_list.count():
            return
        item = self.role_list.takeItem(row)
        self.role_list.insertItem(target, item)
        self.role_list.setCurrentRow(target)

    def compose_spec(self) -> dict[str, Any]:
        project = self.current_project()
        if not project:
            raise ConfigError("请先选择项目")
        if not project.get("enabled", True):
            raise ConfigError("项目已禁用，协调器会拒绝新任务")
        goal = self.goal_edit.toPlainText().strip()
        if not goal:
            raise ConfigError("请输入自然语言任务")
        title = self.title_edit.text().strip() or goal.splitlines()[0][:40].strip()
        flow = self.checked_flow()
        if not flow:
            raise ConfigError("请至少选择一个角色")
        if int(self.calls_spin.value()) < len(flow):
            raise ConfigError(
                f"模型调用上限是整次任务的总次数，当前 {int(self.calls_spin.value())} 小于分工步数 {len(flow)}。"
                "后面的角色不会被调用。请提高调用上限，或减少本次角色。"
            )
        spec: dict[str, Any] = {
            "project_id": project["project_id"],
            "task_id": _new_task_id(str(project["project_id"])),
            "title": title,
            "goal": goal,
            "flow": flow,
            "timeout_seconds": int(self.timeout_spin.value()),
            "max_rounds": int(self.rounds_spin.value()),
            "max_repair_rounds": int(self.repair_spin.value()),
            "max_model_calls": int(self.calls_spin.value()),
            "allow_real_codex": False,
            "allow_real_cursor": False,
        }
        participants = {item["id"]: item for item in project.get("participants") or []}
        mock_text = self.mock_edit.text().strip()
        if mock_text:
            if any(participants.get(item, {}).get("executor") != "mock" for item in flow):
                raise ConfigError("模拟场景只能用于模拟执行器，不会拿来调用真实模型")
            spec["mock_plan"] = [part.strip() for part in mock_text.split(",") if part.strip()]
        if self.write_controlled.isChecked():
            allow_codex = self.codex_switch.isChecked()
            allow_cursor = self.cursor_switch.isChecked()
            if not allow_codex and not allow_cursor:
                raise ConfigError("受控写入需要打开真实 Codex 或 Cursor 的配置开关。本次没有启动。")
            if allow_codex and not self.env_switch.isChecked():
                raise ConfigError(
                    "真实 Codex 写入需要同时打开配置开关和环境开关。"
                    f"工作区写入范围是整个目录：{project.get('workspace')}。本次没有启动。"
                )
            spec["allow_real_codex"] = allow_codex
            spec["allow_real_cursor"] = allow_cursor
        wanted = self.worktree_box.isChecked()
        current = bool((project.get("worktree") or {}).get("enabled"))
        if wanted != current:
            worktree = dict(project.get("worktree") or {"enabled": False, "base_ref": "HEAD", "root": "", "path": ""})
            worktree["enabled"] = wanted
            worktree["path"] = ""
            if wanted and not str(worktree.get("root") or "").strip():
                raise ConfigError("当前项目没有 Worktree 根目录。Worktree 只是 Git 隔离工作树，不是操作系统沙箱。")
            spec["worktree"] = worktree
        payload = {key: value for key, value in spec.items() if key != "project_id"}
        self.adapter.build_task(str(project["project_id"]), payload)
        return spec

    def queue_current(self) -> None:
        try:
            spec = self.compose_spec()
        except ConfigError as exc:
            self.show_error(str(exc), dialog=True)
            return
        if len(self.pending) >= 2:
            self.show_error("待启动队列最多两个任务。", dialog=True)
            return
        self.pending.append(spec)
        self._fill_pending()
        self.goal_edit.clear()
        self.title_edit.clear()
        self.show_info(f"已加入队列：{spec['task_id']}")

    def remove_pending(self) -> None:
        row = self.pending_list.currentRow()
        if row < 0 or row >= len(self.pending):
            return
        del self.pending[row]
        self._fill_pending()

    def _fill_pending(self) -> None:
        self.pending_list.clear()
        for spec in self.pending:
            self.pending_list.addItem(f"{spec.get('title') or spec.get('task_id')}  ·  {spec.get('project_id')}")

    def start_current(self) -> None:
        if self._starting:
            self.show_error("正在提交，请不要重复点击。")
            return
        if self.adapter.has_live_process() or self.adapter.blocking_batches():
            self.show_error("已有协调器进程在运行。请等待它结束，或先取消其中的任务。关闭窗口也不会把它标成成功。", dialog=True)
            return
        self._starting = True
        self.start_button.setEnabled(False)
        try:
            specs = list(self.pending) if self.pending else [self.compose_spec()]
            info = self.adapter.start_batch(
                specs,
                max_concurrent=int(self.concurrent_spin.value()),
                allow_codex_env=any(bool(item.get("allow_real_codex")) for item in specs) and self.env_switch.isChecked(),
            )
        except ConfigError as exc:
            self.show_error(str(exc), dialog=True)
            return
        finally:
            self._starting = False
            self._sync_running_button()
        self._follow_ids = [str(item["task_id"]) for item in specs]
        self.pending.clear()
        self._fill_pending()
        self.show_info(f"已提交批次 {info['batch_id']}。状态来自协调器调度文件。")
        self._poll(history=False)

    def cancel_selected(self) -> None:
        payload = self.current_payload()
        schedule_id = str((payload or {}).get("schedule_id") or "")
        if not schedule_id:
            self.show_error("请先选择一个仍在调度文件中的任务。历史记录不会被删除。", dialog=True)
            return
        try:
            text = self.adapter.cancel(schedule_id)
        except ConfigError as exc:
            self.show_error(str(exc), dialog=True)
            return
        self.show_info(text)
        self._poll(history=False)

    def add_project(self) -> None:
        dialog = ProjectDialog(None, self.save_project_payload, interactive=self.interactive, parent=self)
        self._test_dialog = dialog
        if self.interactive:
            dialog.exec()

    def edit_selected(self) -> None:
        project = self.current_project()
        if not project:
            self.show_error("请先选择项目", dialog=True)
            return
        dialog = ProjectDialog(project, self.save_project_payload, interactive=self.interactive, parent=self)
        self._test_dialog = dialog
        if self.interactive:
            dialog.exec()

    def save_project_payload(self, data: dict[str, Any], existing_id: str | None) -> None:
        if existing_id:
            self.adapter.update_from_mapping(existing_id, data)
        else:
            self.adapter.register_from_mapping(data)
        self._force_form = True
        self._applied_project_id = ""
        self._poll(history=False)

    def toggle_enabled(self) -> None:
        project = self.current_project()
        if not project:
            self.show_error("请先选择项目", dialog=True)
            return
        enabled = not bool(project.get("enabled", True))
        try:
            self.adapter.set_enabled(str(project["project_id"]), enabled)
        except ConfigError as exc:
            self.show_error(str(exc), dialog=True)
            return
        self._force_form = True
        self.show_info("已启用项目" if enabled else "已禁用项目")
        self._poll(history=False)

    def _on_task_changed(self) -> None:
        payload = self.current_payload()
        if not payload:
            return
        if self._follow_ids and payload.get("task_id") not in self._follow_ids:
            self._follow_ids = []
        self._pinned_round = ""
        self._load_key = None
        self._signature = None
        self._show_payload(payload)
        run_dir = str(payload.get("run_dir") or "")
        if run_dir:
            self._maybe_load(run_dir, str(payload.get("state") or ""))
        else:
            self._clear_conversation("协调器还没有创建运行目录。这不是任务成功。")

    def _show_payload(self, payload: dict[str, Any]) -> None:
        shown = presentation(
            str(payload.get("state") or ""),
            str(payload.get("stop_reason") or ""),
            success=payload.get("success"),
        )
        self.phase_label.setText(f"当前阶段：{shown['label']}")
        self.task_heading.setText(str(payload.get("title") or payload.get("task_id") or "任务"))
        detail = str(payload.get("detail") or "")
        self.agent_label.setText(detail or "等待协调器事件。")

    def _maybe_load(self, run_dir: str, state: str) -> None:
        path = Path(run_dir)
        stamp = 0.0
        if path.is_dir():
            for name in ("messages.jsonl", "events.jsonl", "result.json", "verification.json", "报告.md"):
                file = path / name
                if file.is_file():
                    try:
                        stamp = max(stamp, file.stat().st_mtime)
                    except OSError:
                        continue
            rounds = path / "rounds"
            if rounds.is_dir():
                try:
                    stamp = max(stamp, rounds.stat().st_mtime)
                except OSError:
                    pass
        key = (run_dir, stamp, state)
        if key == self._load_key and self._view:
            return
        self._load_key = key
        try:
            view = self.adapter.load_run(run_dir)
        except Exception as exc:
            self.show_error(f"{type(exc).__name__}: {exc}")
            return
        self.render_run(view)

    def render_run(self, view: dict[str, Any]) -> None:
        self._view = view
        if view.get("error") and not view.get("messages"):
            self.show_error(str(view["error"]))
        result = view.get("result") or {}
        task = view.get("task") or {}
        if result.get("stop_reason") or result.get("status"):
            state = status_from_result_dict(result)
            shown = presentation(
                state,
                str(result.get("stop_reason") or ""),
                success=result.get("success"),
                task=task if isinstance(task, dict) else None,
            )
            self.phase_label.setText(f"当前阶段：{shown['label']}")
            if task.get("title"):
                self.task_heading.setText(str(task["title"]))
        events = view.get("events") or []
        self.agent_label.setText(_agent_status(events))
        messages = view.get("messages") or []
        signature = (
            view.get("run_dir"),
            len(messages),
            (messages[-1].get("content") if messages else ""),
            result.get("stop_reason"),
            len(events),
            len(view.get("rounds") or []),
            result.get("error_message"),
        )
        if signature != self._signature:
            self._signature = signature
            self._render_messages(messages, view.get("rounds") or [])
        self._render_side(view)

    def _render_messages(self, messages: list[dict[str, Any]], rounds: list[dict[str, Any]]) -> None:
        self._clear_layout(self.conversation_layout)
        parts: list[str] = []
        if not messages:
            self.welcome = QLabel("协调器还没有写下交接消息。")
            self.welcome.setObjectName("welcome")
            self.welcome.setWordWrap(True)
            self.conversation_layout.addWidget(self.welcome)
        round_meta = {item.get("name"): item.get("meta") or {} for item in rounds}
        for message in messages:
            summary, details, decision = _summarize(str(message.get("content") or ""))
            error = str(message.get("error_message") or "")
            parts.extend([summary, details, error, decision])
            card = QFrame()
            card.setProperty("card", True)
            card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            column = QVBoxLayout(card)
            role = ROLE_ZH.get(str(message.get("role")), str(message.get("role") or ""))
            executor = EXECUTOR_ZH.get(str(message.get("executor")), str(message.get("executor") or ""))
            round_index = int(message.get("round_index") or 0)
            heading = QLabel(f"第 {round_index} 轮  ·  {executor}  ·  {role}")
            heading.setObjectName("phaseLabel")
            column.addWidget(heading)
            meta = round_meta.get(f"{round_index:03d}") or {}
            invoked = "已调用" if message.get("invoked") else "未调用"
            exit_code = message.get("exit_code")
            decision_text = DECISION_ZH.get(decision, decision)
            bits = [str(message.get("mode_label") or executor), invoked]
            if exit_code is not None:
                bits.append(f"进程退出码 {exit_code}")
            if decision_text:
                bits.append(str(decision_text))
            if meta.get("error_kind") and meta.get("error_kind") != "ok":
                bits.append(str(meta.get("error_kind")))
            sub = QLabel("  ·  ".join(bit for bit in bits if bit))
            sub.setObjectName("mutedLabel")
            sub.setWordWrap(True)
            column.addWidget(sub)
            body = QLabel(summary or details or "（没有可显示的正文）")
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            column.addWidget(body)
            if details and details != summary:
                extra = QLabel(details[:1200])
                extra.setWordWrap(True)
                extra.setObjectName("mutedLabel")
                extra.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                column.addWidget(extra)
            if error:
                err = QLabel(error)
                err.setWordWrap(True)
                err.setProperty("kind", "error")
                column.addWidget(err)
            button = QPushButton(f"查看第 {round_index} 轮")
            button.clicked.connect(lambda _checked=False, name=f"{round_index:03d}": self.show_round(name))
            column.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
            self.conversation_layout.addWidget(card)
            card.style().unpolish(card)
            card.style().polish(card)
        self.conversation_layout.addStretch(1)
        self._transcript = "\n".join(part for part in parts if part)
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def show_round(self, name: str) -> None:
        self._pinned_round = name
        if self._view:
            self._render_side(self._view)

    def _render_side(self, view: dict[str, Any]) -> None:
        rounds = view.get("rounds") or []
        selected = None
        if self._pinned_round:
            selected = next((item for item in rounds if item.get("name") == self._pinned_round), None)
        if selected is None and rounds:
            selected = rounds[-1]
        diff = ""
        if selected and selected.get("diff"):
            diff = f"第 {selected.get('name')} 轮\n{selected.get('diff')}"
        elif view.get("worktree_diff"):
            diff = "Worktree 差异\n" + str(view.get("worktree_diff"))
        else:
            diff = "这一轮还没有差异文件。没有差异也不代表任务成功。"
        self._set_text(self.diff_view, diff)
        self._set_text(self.verify_view, _verification_text(view))
        self._set_text(self.log_view, self._log_text(view))
        self._set_text(self.report_view, _acceptance_text(view))
        self._set_text(self.permission_view, self._permission_text(view))

    def _log_text(self, view: dict[str, Any]) -> str:
        lines: list[str] = []
        result = view.get("result") or {}
        if result.get("error_message"):
            lines.append("执行错误：" + str(result.get("error_message")))
        for event in view.get("events") or []:
            lines.append(
                " ".join(
                    str(part)
                    for part in (
                        event.get("time") or "",
                        event.get("event") or "",
                        event.get("phase") or "",
                        event.get("agent") or "",
                        event.get("role") or "",
                        f"第{event.get('round')}轮" if event.get("round") else "",
                        event.get("error_kind") or "",
                    )
                    if part
                )
            )
        for item in view.get("rounds") or []:
            stderr = str(item.get("stderr") or "").strip()
            if stderr:
                lines.append(f"第 {item.get('name')} 轮 stderr：")
                lines.append(stderr)
        batch_id = str((view.get("task") or {}).get("batch_id") or "")
        for process in self._process_rows:
            if batch_id and process.get("batch_id") != batch_id:
                continue
            log = str(process.get("log") or "").strip()
            if log:
                lines.append("协调器输出：")
                lines.append(log)
        if not lines:
            lines.append("还没有运行日志。")
        return "\n".join(lines)

    def _permission_text(self, view: dict[str, Any] | None = None) -> str:
        task = (view or {}).get("task") or {}
        result = (view or {}).get("result") or {}
        lines = [SAFETY_TEXT, ""]
        if task:
            lines.append(f"工作区：{task.get('workspace') or ''}")
            lines.append(f"allow_real_codex：{task.get('allow_real_codex')}")
            lines.append(f"allow_real_cursor：{task.get('allow_real_cursor')}")
            worktree = task.get("worktree") or {}
            lines.append(f"Worktree：{'启用' if worktree.get('enabled') else '关闭'}")
            if worktree.get("path"):
                lines.append(f"Worktree 路径：{worktree.get('path')}")
            lines.append(f"超时：{task.get('timeout_seconds')} 秒")
            lines.append(f"修复轮数：{task.get('max_repair_rounds')}")
            lines.append(f"模型调用上限：{task.get('max_model_calls')}")
            lines.append(f"最大轮次：{task.get('max_rounds')}")
        else:
            lines.append(self._preview_permission())
        messages = (view or {}).get("messages") or []
        incoming = sum(int(item.get("input_tokens") or 0) for item in messages)
        outgoing = sum(int(item.get("output_tokens") or 0) for item in messages)
        if result:
            incoming = int(result.get("input_tokens") or incoming)
            outgoing = int(result.get("output_tokens") or outgoing)
            lines.append(f"Codex 调用：{result.get('codex_calls')}")
            lines.append(f"Cursor 调用：{result.get('cursor_calls')}")
            lines.append(f"调用性质：{result.get('mode_label') or ''}")
        lines.append(f"Token 输入：{incoming}")
        lines.append(f"Token 输出：{outgoing}")
        return "\n".join(lines)

    def _preview_permission(self) -> str:
        project = self.current_project() or {}
        mode = "受控写入" if self.write_controlled.isChecked() else "只读"
        return "\n".join(
            [
                "尚未选择运行结果。下面是准备提交的设置，还没有执行。",
                f"项目：{project.get('name') or ''}",
                f"工作区：{project.get('workspace') or ''}",
                f"模式：{mode}",
                f"allow_real_codex：{self.write_controlled.isChecked() and self.codex_switch.isChecked()}",
                f"allow_real_cursor：{self.write_controlled.isChecked() and self.cursor_switch.isChecked()}",
                f"子进程 Codex 环境开关：{self.write_controlled.isChecked() and self.env_switch.isChecked()}",
                f"Worktree：{'启用' if self.worktree_box.isChecked() else '关闭'}",
            ]
        )

    def _refresh_permission_preview(self) -> None:
        if self._view.get("task"):
            return
        self._set_text(self.permission_view, self._permission_text({}))

    def _clear_conversation(self, text: str) -> None:
        self._clear_layout(self.conversation_layout)
        self.welcome = QLabel(text)
        self.welcome.setWordWrap(True)
        self.welcome.setObjectName("welcome")
        self.conversation_layout.addWidget(self.welcome)
        self.conversation_layout.addStretch(1)
        self._transcript = ""
        self._signature = None
        self._view = {}

    def _clear_layout(self, layout: QVBoxLayout) -> None:
        while layout.count():
            child = layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()

    def show_error(self, text: str, *, dialog: bool = False) -> None:
        self.banner.setText(text)
        self._mark(self.banner, "error")
        if dialog and self.interactive:
            QMessageBox.warning(self, "协作控制台", text)

    def show_info(self, text: str) -> None:
        self.banner.setText(text)
        self._mark(self.banner, "ok")

    def _mark(self, label: QLabel, kind: str) -> None:
        label.setProperty("kind", kind)
        label.style().unpolish(label)
        label.style().polish(label)

    def _set_text(self, widget: QPlainTextEdit, text: str) -> None:
        if widget.toPlainText() != text:
            widget.setPlainText(text)

    def transcript(self) -> str:
        return self._transcript

    def task_list_text(self) -> str:
        return "\n".join(self.task_list.item(index).text() for index in range(self.task_list.count()))

    def schedule_state(self, task_id: str) -> str:
        for row in self.latest_schedules:
            if row.get("task_id") == task_id:
                return str(row.get("state") or "")
        return ""

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.adapter.has_live_process() and self.interactive:
            box = QMessageBox(self)
            box.setWindowTitle("任务仍在运行")
            box.setText("协调器在独立进程中继续运行。关闭窗口不会把任务标成成功，也不会取消它。")
            box.setInformativeText("可以稍后重新打开协作控制台查看状态。若要停止，请先取消任务。")
            stay = box.addButton("保持打开", QMessageBox.ButtonRole.RejectRole)
            leave = box.addButton("仍然关闭", QMessageBox.ButtonRole.AcceptRole)
            box.setDefaultButton(stay)
            box.exec()
            if box.clickedButton() is not leave:
                event.ignore()
                return
        self._closing = True
        self.timer.stop()
        event.accept()


def _new_task_id(project_id: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    cleaned = re.sub(r"[^\w\-]+", "", f"{project_id}-{stamp}", flags=re.UNICODE)
    return (cleaned or f"task-{stamp}")[:80]


def _summarize(content: str) -> tuple[str, str, str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content[:800], "", ""
    if not isinstance(data, dict):
        return content[:800], "", ""
    return str(data.get("summary") or ""), str(data.get("details") or ""), str(data.get("decision") or "")


def _agent_status(events: list[dict[str, Any]]) -> str:
    started = None
    finished = None
    for event in events:
        name = event.get("event")
        if name == "step_started":
            started = event
            finished = None
        elif name == "step_finished":
            finished = event
    if started and finished is None:
        role = ROLE_ZH.get(str(started.get("role")), str(started.get("role") or ""))
        return f"正在执行 {started.get('agent')}（{role}）第 {started.get('round')} 轮"
    if finished:
        return f"最近一轮 {finished.get('agent')} 已结束，结果 {finished.get('error_kind') or '无'}"
    return "还没有 Agent 步骤"


def _verification_text(view: dict[str, Any]) -> str:
    lines: list[str] = []
    records = view.get("verification") or []
    if not records:
        lines.append("协调器还没有写下 verification.json。")
    for record in records:
        kind = str(record.get("type") or "验证")
        state = "通过" if record.get("ok") else "未通过"
        lines.append(f"{kind}：{state}")
        if "exit_code" in record:
            lines.append(f"可信验证退出码：{record.get('exit_code')}（预定 {record.get('expect_exit_code')}）")
        if record.get("error"):
            lines.append(str(record.get("error")))
        stdout = str(record.get("stdout") or "").strip()
        stderr = str(record.get("stderr") or "").strip()
        if stdout:
            lines.append("stdout：" + stdout[:1000])
        if stderr:
            lines.append("stderr：" + stderr[:1000])
    lines.append("")
    lines.append("各轮进程退出码：")
    for item in view.get("rounds") or []:
        meta = item.get("meta") or {}
        lines.append(
            f"第 {item.get('name')} 轮 进程退出码：{meta.get('exit_code')}  "
            f"决策：{meta.get('decision') or ''}  调用：{'是' if meta.get('invoked') else '否'}"
        )
    return "\n".join(lines)


def _acceptance_text(view: dict[str, Any]) -> str:
    result = view.get("result") or {}
    if not result:
        return "协调器尚未写下最终结果。当前调度状态不代表成功。\n\n" + str(view.get("report") or "")
    state = status_from_result_dict(result)
    task = view.get("task") if isinstance(view.get("task"), dict) else None
    shown = presentation(
        state,
        str(result.get("stop_reason") or ""),
        success=result.get("success"),
        task=task,
    )
    if shown["passed"]:
        conclusion = "可信验证通过"
    elif shown["kind"] == "flow_complete":
        conclusion = "未进行功能验收"
    elif shown["kind"] == "cancelled":
        conclusion = "已取消"
    else:
        conclusion = "未通过"
    lines = [
        f"状态：{shown['label']}",
        f"结论：{conclusion}",
        f"停止原因：{result.get('stop_reason_zh') or reason_zh(str(result.get('stop_reason') or ''))}",
        f"错误：{result.get('error_message') or '无'}",
        f"调用性质：{result.get('mode_label') or ''}",
        f"报告：{result.get('report_path') or ''}",
        "",
        str(view.get("report") or "报告文件还不存在。"),
    ]
    return "\n".join(lines)
