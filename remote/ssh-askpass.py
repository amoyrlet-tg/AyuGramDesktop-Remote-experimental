#!/usr/bin/env python3
import os
import sys
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox

app = QApplication([])
prompt = sys.argv[1] if len(sys.argv) > 1 else "SSH"
if os.environ.get("SSH_ASKPASS_PROMPT") == "confirm" or "continue connecting" in prompt:
    answer = QMessageBox.question(None, "SSH: проверка сервера",
        prompt + "\n\nСверь отпечаток с панелью своего VPS перед подтверждением.")
    if answer != QMessageBox.Yes:
        raise SystemExit(1)
    print("yes")
else:
    answer, accepted = QInputDialog.getText(None, "SSH", prompt, QLineEdit.Password)
    if not accepted:
        raise SystemExit(1)
    print(answer)
