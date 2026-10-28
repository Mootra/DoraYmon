"""Reuse connections within a thread without sharing mutable requests sessions."""
from __future__ import annotations

import threading

import requests


class ThreadLocalSessions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[threading.Thread, requests.Session] = {}

    def get(self) -> requests.Session:
        current = threading.current_thread()
        with self._lock:
            for thread in tuple(self._sessions):
                if thread is not current and not thread.is_alive():
                    self._sessions.pop(thread).close()
            if current not in self._sessions:
                self._sessions[current] = requests.Session()
            return self._sessions[current]

    def reset(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                session.close()
            self._sessions.clear()
