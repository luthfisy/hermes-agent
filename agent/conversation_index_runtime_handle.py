"""Runtime handle for one profile-scoped conversation-index bootstrapper."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent.conversation_index_consumer import (
    ConversationIndexConsumer,
    ConversationIndexConsumerStatus,
)


@dataclass
class ConversationIndexRuntimeHandle:
    provider_name: str
    db_path: Path
    hermes_home: Path
    profile_name: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    consumer: Optional[ConversationIndexConsumer] = None
    bootstrap_status: ConversationIndexConsumerStatus = field(init=False)

    def __post_init__(self):
        self.bootstrap_status = ConversationIndexConsumerStatus(
            index_name=self.provider_name, state="starting",
        )

    def status(self) -> ConversationIndexConsumerStatus:
        consumer = self.consumer
        return consumer.status() if consumer is not None else self.bootstrap_status

    def stop(self) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
