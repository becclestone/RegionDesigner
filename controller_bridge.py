"""Qt-signal bridge over the existing IsMsgPy/RabbitMQ-STOMP layer.

IsMsgPy (rmq_setup.py, IsListener.py, IsMsg.py) has no FreeSimpleGUI dependency,
so it's reused as-is (as its own git submodule here, same as in DOVER_UI). The
only thing rebuilt is the dispatch step: instead of DOVER_UI/MessageHandler.py's
habit of stuffing live sg.Window references into a queue the GUI-thread event loop
polls, a background thread reads the same thread-safe queue.Queue and re-emits the
small set of message types this tool cares about as Qt signals. Qt automatically
marshals a signal emitted from a non-GUI thread to the receiving QObject's (GUI)
thread, so no extra locking is needed here.
"""
from queue import Queue, Empty
from threading import Thread
from time import sleep

from PySide6.QtCore import QObject, Signal

from Constants import implementation_constants as ic, rmq_credentials
import IsMsgPy.shared_rmq_constants as src
from IsMsgPy.rmq_setup import LoginCredentials, rmq_setup, rmg_log_hb_setup
from Utilities import safemon_msg_senders as sm
from Utilities.controller_commands import move_to_snap_position


class ControllerBridge(QObject):
    imageReady = Signal(str)
    commandError = Signal(str)

    def __init__(self):
        super().__init__()
        self._rmq_queue: Queue = Queue(maxsize=100)
        self._running = False
        self._listener_thread: Thread | None = None

    def connect_to_controller(self):
        credentials = LoginCredentials(
            login=rmq_credentials.USER_NAME,
            passcode=rmq_credentials.USER_PASSCODE,
            host=rmq_credentials.HOST,
            vhost=rmq_credentials.VHOST_NAME,
            port=rmq_credentials.PORT,
        )
        # Reuses GUI_TARGET ("dover_ui", == rmq_credentials.USER_NAME) as the sender
        # signature: the controller addresses its image-ready reply to that fixed
        # target rather than to a per-requester id, so this tool must answer to the
        # same name to receive it (a second, independent subscriber - this does not
        # take messages away from the existing FreeSimpleGUI app if it's also running).
        rmq_setup(msg_signature=rmq_credentials.USER_NAME, q=self._rmq_queue, credentials=credentials)
        rmg_log_hb_setup(credentials)

        self._running = True
        self._listener_thread = Thread(target=self._dispatch_loop, daemon=True)
        self._listener_thread.start()

    def _dispatch_loop(self):
        while self._running:
            try:
                msg = self._rmq_queue.get(True, 0.05)
            except Empty:
                continue
            self._handle_message(msg)

    def _handle_message(self, msg):
        if msg.get_msg_class() != src.cCOMM_MSG_CLASS:
            return
        msg_type = msg.get_msg_type()
        if msg_type == ic.cIMAGE_READY_MSG:
            payload = msg.get_msg_payload()
            self.imageReady.emit(payload[ic.cIMAGE_PATH])
        # Other message types are out of scope for this phase (no path/scan/focus
        # traffic is sent or expected yet).

    def request_snap(self):
        """Mirrors DOVER_UI/Windows/demo_control_a.py's Snap button sequence exactly."""
        sm.safemon_action_snap()
        sleep(0.3)
        move_to_snap_position()

    def shutdown(self):
        self._running = False
