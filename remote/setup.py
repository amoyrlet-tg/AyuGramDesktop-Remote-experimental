#!/usr/bin/env python3
import os
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ayuremote.launcher import pair_existing, provision, run_client, update_settings


class Worker(QThread):
    finished_work = Signal(str)
    failed = Signal(str)

    def __init__(self, operation):
        super().__init__()
        self.operation = operation

    def run(self):
        try:
            self.finished_work.emit(str(self.operation()))
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class Setup(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AyuGram Remote — сервер")
        self.worker = None
        self.root = Path(__file__).resolve().parent.parent
        self.layout = QVBoxLayout(self)
        intro = QLabel("Экспериментальная сборка. Сначала подключение к VPS, затем вход в Telegram.\n"
                       "Установка поддерживает Debian/Ubuntu и root по SSH.\n"
                       "C++ и настоящий вход в аккаунт для этой версии ещё не проверены.")
        intro.setWordWrap(True)
        self.layout.addWidget(intro)
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItems(["Установить на новый VPS", "Подключить установленный сервер",
                            "Запустить сохранённый профиль", "Изменить лимит кэша сервера"])
        form.addRow("Действие", self.mode)
        self.host = QLineEdit()
        self.host.setPlaceholderText("IP или имя твоего VPS")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.key = QLineEdit()
        self.key.setPlaceholderText("пусто = SSH-agent или пароль через окно OpenSSH")
        self.profile = QLineEdit(str(Path.home() / ".local/share/ayugram-remote/default"))
        self.binary = QLineEdit(str(self.root / "out/bin/AyuGram"))
        self.api_id = QLineEdit()
        self.api_hash = QLineEdit()
        self.api_hash.setEchoMode(QLineEdit.Password)
        self.limit = QDoubleSpinBox()
        self.limit.setRange(0, 1024)
        self.limit.setValue(3)
        self.limit.setSuffix(" GiB")
        self.offline = QCheckBox("Запустить без сервера, используя локальный кэш")
        for label, control in [("Сервер", self.host), ("SSH-порт", self.port), ("SSH-ключ", self.key),
                               ("Папка профиля", self.profile), ("Изменённый клиент", self.binary),
                               ("Telegram api_id", self.api_id), ("Telegram api_hash", self.api_hash),
                               ("Кэш на VPS", self.limit)]:
            form.addRow(label, control)
        self.layout.addLayout(form)
        self.layout.addWidget(self.offline)
        choose = QPushButton("Выбрать собранный AyuGram…")
        choose.clicked.connect(self.choose)
        self.layout.addWidget(choose)
        self.status = QLabel("Для сборки нужны зависимости из docs/building-linux.md. Сборка VPS может занять длительное время.")
        self.status.setWordWrap(True)
        self.layout.addWidget(self.status)
        self.button = QPushButton("Подключить и открыть AyuGram")
        self.button.clicked.connect(self.start)
        self.layout.addWidget(self.button)

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "Изменённый исполняемый файл AyuGram")
        if path:
            self.binary.setText(path)

    def start(self):
        mode = self.mode.currentIndex()
        profile, binary = self.profile.text(), self.binary.text()
        host, port, key = self.host.text().strip(), self.port.value(), self.key.text().strip()
        offline = self.offline.isChecked()
        api_id, api_hash = self.api_id.text().strip(), self.api_hash.text().strip()
        limit = int(self.limit.value() * 1024**3)
        if mode != 3 and not Path(binary).expanduser().is_file():
            QMessageBox.warning(self, "Нужна сборка", "Укажи исполняемый файл из этого изменённого репозитория.")
            return
        if mode == 0:
            answer = QMessageBox.question(self, "Установка на VPS",
                f"Установить Docker и собрать сервер на {host}?\n"
                "Будут созданы /opt/ayugram-remote и контейнер ayugram-remote.\n"
                "Существующие сессии установщик не заменяет.")
            if answer != QMessageBox.Yes:
                return

        def action():
            if mode == 3:
                return update_settings(profile, limit)
            if mode == 0:
                provision(self.root, profile, host, port, key, int(api_id), api_hash, limit)
            elif mode == 1:
                pair_existing(profile, host, port, key)
            code = run_client(profile, binary, offline)
            if code:
                raise RuntimeError(f"Клиент завершился с кодом {code}. Проверь вывод терминала.")
            return "Клиент закрыт. Сервер продолжает работать."

        self.button.setEnabled(False)
        self.status.setText("Подключение, установка и запуск… Подтверди SSH-отпечаток только после сверки с панелью VPS.")
        self.worker = Worker(action)
        self.worker.finished_work.connect(self.complete)
        self.worker.failed.connect(self.complete)
        self.worker.start()

    def complete(self, result):
        self.status.setText(result)
        self.button.setEnabled(True)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Операция выполняется", "Закрой запущенный AyuGram или дождись завершения установки.")
            event.ignore()
        else:
            event.accept()


if __name__ == "__main__":
    os.umask(0o077)
    os.environ["SSH_ASKPASS"] = str(Path(__file__).resolve().parent / "ssh-askpass.py")
    os.environ["SSH_ASKPASS_REQUIRE"] = "force"
    app = QApplication(sys.argv)
    window = Setup()
    window.show()
    raise SystemExit(app.exec())
