"""Custom item delegates for the manual entry tables."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QStyledItemDelegate, QWidget


class ComboBoxDelegate(QStyledItemDelegate):
    """Editable combo box delegate offering a fixed list of suggestions.

    The combo box stays editable so auditors can type values that are not in
    the predefined list (for example an unusual carrier code).
    """

    def __init__(self, options: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.options = options

    def createEditor(self, parent, option, index):  # noqa: N802 (Qt naming)
        combo = QComboBox(parent)
        combo.addItems(self.options)
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        return combo

    def setEditorData(self, editor: QComboBox, index):  # noqa: N802
        value = index.data(Qt.EditRole) or index.data(Qt.DisplayRole) or ""
        pos = editor.findText(value)
        if pos >= 0:
            editor.setCurrentIndex(pos)
        else:
            editor.setEditText(value)

    def setModelData(self, editor: QComboBox, model, index):  # noqa: N802
        model.setData(index, editor.currentText().strip(), Qt.EditRole)
