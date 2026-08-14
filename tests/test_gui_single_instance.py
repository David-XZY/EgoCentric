from __future__ import annotations

import os
import uuid

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalServer

from egocentric_capture.gui import _activate_existing_instance


def test_existing_gui_instance_can_be_activated() -> None:
    application = QCoreApplication.instance() or QCoreApplication([])
    name = f"egocentric-test-{os.getpid()}-{uuid.uuid4().hex}"
    server = QLocalServer()
    QLocalServer.removeServer(name)
    try:
        assert server.listen(name)
        assert _activate_existing_instance(name)
        application.processEvents()
        assert server.hasPendingConnections()
    finally:
        server.close()
        QLocalServer.removeServer(name)
