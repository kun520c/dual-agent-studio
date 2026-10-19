"""项目注册对话框。保存时仍走 register_project / update_project。"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dual_agent.models import ConfigError

SaveCallback = Callable[[dict[str, Any], str | None], None]

_EXECUTORS = (("mock", "模拟"), ("codex", "Codex"), ("cursor", "Cursor"))
_ROLES = (
    ("developer", "开发者"),
    ("reviewer", "审查者"),
    ("diagnoser", "诊断者"),
    ("acceptor", "验收者"),
)


def default_project() -> dict[str, Any]:
    return {
        "project_id": "",
        "name": "",
        "workspace": "",
        "allowed_roots": [],
        "enabled": True,
        "participants": [
            {"id": "codex", "executor": "mock", "role": "developer"},
            {"id": "cursor", "executor": "mock", "role": "reviewer"},
        ],
        "verification": [],
        "max_repair_rounds": 1,
        "max_model_calls": 8,
        "max_run_seconds": 900,
        "timeout_seconds": 120,
        "max_rounds": 6,
        "resource_locks": [],
        "worktree": {"enabled": False, "base_ref": "HEAD", "root": "", "path": ""},
        "description": "",
        "concurrency": {"max_write_tasks": 1, "allow_shared_context": False},
    }


class ProjectDialog(QDialog):
    def __init__(
        self,
        existing: dict[str, Any] | None,
        on_save: SaveCallback,
        *,
        interactive: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_save = on_save
        self._interactive = interactive
        self._baseline = dict(existing) if existing else default_project()
        self.existing_id = str(self._baseline.get("project_id") or "") or None
        self.error = ""
        self.setWindowTitle("修改项目" if self.existing_id else "添加项目")
        self.resize(680, 640)
        self._build()
        self._load(self._baseline)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.id_edit = QLineEdit()
        self.name_edit = QLineEdit()
        self.workspace_edit = QLineEdit()
        browse = QPushButton("选择目录")
        browse.clicked.connect(self._browse)
        workspace_row = QHBoxLayout()
        workspace_row.addWidget(self.workspace_edit, 1)
        workspace_row.addWidget(browse)
        workspace_host = QWidget()
        workspace_host.setLayout(workspace_row)
        self.description_edit = QLineEdit()
        self.enabled_box = QCheckBox("启用项目")
        form.addRow("项目编号", self.id_edit)
        form.addRow("名称", self.name_edit)
        form.addRow("工作区", workspace_host)
        form.addRow("说明", self.description_edit)
        form.addRow("", self.enabled_box)
        layout.addLayout(form)

        layout.addWidget(QLabel("角色分工"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["标识", "执行器", "角色"])
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        add_button = QPushButton("添加角色")
        remove_button = QPushButton("移除角色")
        add_button.clicked.connect(lambda: self._add_participant("agent", "mock", "reviewer"))
        remove_button.clicked.connect(self._remove_participant)
        row.addWidget(add_button)
        row.addWidget(remove_button)
        row.addStretch(1)
        layout.addLayout(row)

        numbers = QFormLayout()
        self.timeout_spin = self._spin(1, 3600, 120)
        self.rounds_spin = self._spin(1, 100, 6)
        self.calls_spin = self._spin(1, 30, 8)
        self.repair_spin = self._spin(0, 5, 1)
        self.runtime_spin = self._spin(1, 3600, 900)
        self.write_tasks = self._spin(1, 2, 1)
        numbers.addRow("单轮超时（秒）", self.timeout_spin)
        numbers.addRow("最大轮次", self.rounds_spin)
        numbers.addRow("模型调用上限", self.calls_spin)
        numbers.addRow("修复轮数", self.repair_spin)
        numbers.addRow("总时长（秒）", self.runtime_spin)
        numbers.addRow("同时写入任务", self.write_tasks)
        layout.addLayout(numbers)

        self.worktree_box = QCheckBox("启用 Worktree")
        self.worktree_hint = QLabel("Worktree 只是 Git 隔离工作树，不是操作系统沙箱。没有 worktree.root 时不能启用。")
        self.worktree_hint.setWordWrap(True)
        self.worktree_hint.setObjectName("mutedLabel")
        layout.addWidget(self.worktree_box)
        layout.addWidget(self.worktree_hint)
        self.verify_label = QLabel()
        self.verify_label.setWordWrap(True)
        self.verify_label.setObjectName("mutedLabel")
        layout.addWidget(self.verify_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.try_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _spin(self, low: int, high: int, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        return spin

    def _load(self, data: dict[str, Any]) -> None:
        self.id_edit.setText(str(data.get("project_id") or ""))
        self.id_edit.setReadOnly(self.existing_id is not None)
        self.name_edit.setText(str(data.get("name") or ""))
        self.workspace_edit.setText(str(data.get("workspace") or ""))
        self.description_edit.setText(str(data.get("description") or ""))
        self.enabled_box.setChecked(bool(data.get("enabled", True)))
        self.timeout_spin.setValue(int(float(data.get("timeout_seconds") or 120)))
        self.rounds_spin.setValue(int(data.get("max_rounds") or 6))
        self.calls_spin.setValue(int(data.get("max_model_calls") or 8))
        self.repair_spin.setValue(int(data.get("max_repair_rounds") or 0))
        self.runtime_spin.setValue(int(float(data.get("max_run_seconds") or 900)))
        concurrency = data.get("concurrency") or {}
        self.write_tasks.setValue(int(concurrency.get("max_write_tasks") or 1))
        worktree = data.get("worktree") or {}
        self.worktree_box.setChecked(bool(worktree.get("enabled")))
        checks = data.get("verification") or []
        if checks:
            self.verify_label.setText(f"已保留 {len(checks)} 条可信验证。保存项目时不会改写这些规则。")
        else:
            self.verify_label.setText("新项目还没有可信验证。没有独立验证时，协调器不会把任务标成通过。")
        self.table.setRowCount(0)
        for item in data.get("participants") or []:
            self._add_participant(str(item.get("id") or ""), str(item.get("executor") or "mock"), str(item.get("role") or "reviewer"))

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择工作区", self.workspace_edit.text())
        if path:
            self.workspace_edit.setText(path)

    def _add_participant(self, participant_id: str, executor: str, role: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(participant_id))
        self.table.setCellWidget(row, 1, self._combo(_EXECUTORS, executor))
        self.table.setCellWidget(row, 2, self._combo(_ROLES, role))

    def _combo(self, options: tuple[tuple[str, str], ...], current: str) -> QComboBox:
        box = QComboBox()
        for key, label in options:
            box.addItem(label, key)
        index = box.findData(current)
        if index < 0:
            index = 0
        box.setCurrentIndex(index)
        return box

    def _remove_participant(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def payload(self) -> dict[str, Any]:
        project_id = self.id_edit.text().strip()
        name = self.name_edit.text().strip()
        workspace = self.workspace_edit.text().strip()
        if not project_id or not name or not workspace:
            raise ConfigError("项目编号、名称和工作区都不能为空")
        participants: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            participant_id = item.text().strip() if item else ""
            if not participant_id:
                continue
            if participant_id in seen:
                raise ConfigError(f"参与者 id 重复：{participant_id}")
            seen.add(participant_id)
            executor = self.table.cellWidget(row, 1)
            role = self.table.cellWidget(row, 2)
            participants.append(
                {
                    "id": participant_id,
                    "executor": str(executor.currentData()),
                    "role": str(role.currentData()),
                }
            )
        if not participants:
            raise ConfigError("至少需要一个角色")
        data = {
            "project_id": project_id,
            "name": name,
            "workspace": workspace,
            "enabled": self.enabled_box.isChecked(),
            "description": self.description_edit.text().strip(),
            "participants": participants,
            "verification": list(self._baseline.get("verification") or []),
            "resource_locks": list(self._baseline.get("resource_locks") or []),
            "max_repair_rounds": int(self.repair_spin.value()),
            "max_model_calls": int(self.calls_spin.value()),
            "max_run_seconds": int(self.runtime_spin.value()),
            "timeout_seconds": int(self.timeout_spin.value()),
            "max_rounds": int(self.rounds_spin.value()),
            "concurrency": {
                "max_write_tasks": int(self.write_tasks.value()),
                "allow_shared_context": bool((self._baseline.get("concurrency") or {}).get("allow_shared_context", False)),
            },
        }
        old_workspace = str(self._baseline.get("workspace") or "")
        if workspace != old_workspace:
            data["allowed_roots"] = [workspace]
        else:
            roots = [str(item) for item in (self._baseline.get("allowed_roots") or [])]
            if workspace not in roots:
                roots.insert(0, workspace)
            data["allowed_roots"] = roots or [workspace]
        worktree = dict(self._baseline.get("worktree") or {"enabled": False, "base_ref": "HEAD", "root": "", "path": ""})
        worktree["enabled"] = self.worktree_box.isChecked()
        if worktree["enabled"] and not str(worktree.get("root") or "").strip():
            raise ConfigError("启用 Worktree 需要项目里已经有 worktree.root。它只是 Git 隔离工作树，不是操作系统沙箱。")
        data["worktree"] = worktree
        if self.existing_id and project_id != self.existing_id:
            raise ConfigError("不能在更新时更改 project_id")
        return data

    def try_save(self) -> bool:
        try:
            data = self.payload()
            self._on_save(data, self.existing_id)
        except Exception as exc:
            self.error = str(exc)
            if self._interactive:
                QMessageBox.warning(self, "无法保存", str(exc))
            return False
        self.error = ""
        self.accept()
        return True
