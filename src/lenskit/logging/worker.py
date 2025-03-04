# This file is part of LensKit.
# Copyright (C) 2018-2023 Boise State University
# Copyright (C) 2023-2025 Drexel University
# Licensed under the MIT license, see LICENSE.md for details.
# SPDX-License-Identifier: MIT

"""
Support for logging from worker processes.

This is internal support code, clients and LensKit implementers can usually
ignore it.
"""

from __future__ import annotations

import copy
import logging
import multiprocessing as mp
import pickle
import warnings
from dataclasses import dataclass
from hashlib import blake2b
from logging import Handler, LogRecord, getLogger
from threading import Lock
from typing import Self, overload

import structlog
import zmq
from structlog.typing import EventDict

from ._proxy import get_logger
from .config import CORE_PROCESSORS, active_logging_config, log_warning
from .monitor import get_monitor
from .processors import add_process_info
from .tasks import Task
from .tracing import lenskit_filtering_logger

_active_context: WorkerContext | None = None
_log = get_logger(__name__)


@dataclass
class WorkerLogConfig:
    """
    Configuration for worker logging.
    """

    address: str
    level: int
    authkey: bytes | None = None

    @classmethod
    @overload
    def current(cls) -> Self: ...
    @classmethod
    @overload
    def current(cls, *, from_monitor: bool = True) -> Self | None: ...
    @classmethod
    def current(cls, *, from_monitor: bool = True):
        """
        Get the current worker logging configuration.
        """

        if _active_context is not None:
            return _active_context.config
        elif from_monitor:
            mon = get_monitor()
            if mon.log_address is None:
                raise RuntimeError("monitor has no log address")
            cfg = active_logging_config()
            level = cfg.effective_level if cfg is not None else logging.INFO
            return cls(
                address=mon.log_address, level=level, authkey=bytes(mp.current_process().authkey)
            )
        else:
            return None


class WorkerContext:
    """
    Activate (and deactivate) a worker context.  This handles setup and teardown
    of logging, etc.

    Only one worker context can be active, regardless of how many threads are active.

    Stability:
        internal
    """

    config: WorkerLogConfig
    zmq: zmq.Context[zmq.Socket[bytes]]
    _log_handler: ZMQLogHandler

    def __init__(self, config: WorkerLogConfig):
        self.config = config
        if self.config.authkey is None:
            self.config.authkey = mp.current_process().authkey

    def start(self):
        """
        Start the logging context.
        """
        global _active_context
        if _active_context is not None:
            raise RuntimeError("worker context already active")
        _active_context = self

        self.zmq = zmq.Context()
        self._log_handler = ZMQLogHandler(self.zmq, self.config)

        root = getLogger()
        root.addHandler(self._log_handler)
        root.setLevel(self.config.level)

        structlog.configure(
            [add_process_info]
            + CORE_PROCESSORS
            + [structlog.processors.ExceptionPrettyPrinter(), self._log_handler.send_structlog],
            wrapper_class=lenskit_filtering_logger(self.config.level),
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=False,
        )
        warnings.showwarning = log_warning
        _log.debug("log context activated")

    def shutdown(self):
        global _active_context
        root = getLogger()
        root.removeHandler(self._log_handler)

        self._log_handler.shutdown()
        self.zmq.term()
        _active_context = None

    def send_task(self, task: Task):
        self._log_handler.send_task(task)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.shutdown()


class ZMQLogHandler(Handler):
    _lock: Lock
    socket: zmq.Socket[bytes]
    key: bytes
    _render = structlog.processors.JSONRenderer()

    def __init__(self, zmq_context: zmq.Context, config: WorkerLogConfig):
        super().__init__()
        self.config = config
        self._lock = Lock()
        self.socket = zmq_context.socket(zmq.PUSH)
        self.socket.connect(config.address)

    def handle(self, record: LogRecord) -> LogRecord | bool:  # type: ignore
        # copy so other handlers don't have a problem
        record = copy.copy(record)

        # update messages for copyability
        if not hasattr(record, "message"):
            record.message = record.msg % record.args

        record.exc_info = None
        record.exc_text = None
        record.stack_info = None

        self._send_message(
            b"stdlib", record.name.encode(), pickle.dumps(record, pickle.HIGHEST_PROTOCOL)
        )

        return record

    def shutdown(self):
        self.socket.close()

    def send_structlog(self, logger, method, event_dict: EventDict):
        x = self._render(logger, method, {"method": method, "event": event_dict})
        if isinstance(x, str):
            x = x.encode()
        self._send_message(b"structlog", logger.name.encode(), x)

        raise structlog.DropEvent()

    def send_task(self, task: Task):
        _log.debug("sending updated task", task_id=task.task_id)
        self._send_message(
            b"lenskit.logging.tasks", str(task.task_id).encode(), task.model_dump_json().encode()
        )

    def _send_message(self, engine: bytes, name: bytes, data: bytes):
        key = self.config.authkey
        assert key is not None
        mb = blake2b(key=key)
        mb.update(engine)
        mb.update(name)
        mb.update(data)

        with self._lock:
            self.socket.send_multipart([engine, name, data, mb.digest()])


def send_task(task: Task):
    assert _active_context is not None
    _active_context.send_task(task)
