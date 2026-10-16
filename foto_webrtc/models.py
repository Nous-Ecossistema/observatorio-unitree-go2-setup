"""Mensagens do padrão Command e snapshots de estado entre threads."""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from .faults import RobotFault


@dataclass(frozen=True)
class Command:
    name: str
    parameters: tuple[tuple[str, Any], ...] = ()
    command_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: float = field(default_factory=time.monotonic)
    generation: int = 0
    valid_until: float | None = None
    parent_command_id: str | None = None

    @property
    def parameter(self):
        return dict(self.parameters)

    @classmethod
    def move(cls, x=0.0, y=0.0, z=0.0, **kwargs):
        return cls("Move", (("x", x), ("y", y), ("z", z)), **kwargs)


@dataclass(frozen=True)
class InputSnapshot:
    keys: frozenset[str] = frozenset()
    timestamp: float = field(default_factory=time.monotonic)
    generation: int = 0
    angular_gain: float | None = None

    def __post_init__(self):
        if self.angular_gain is not None and not 0 < self.angular_gain <= 1:
            raise ValueError("A velocidade de giro deve estar entre 0 (exclusivo) e 1")


class ConnectionState(str, Enum):
    CONNECTING = "Conectando"
    CONNECTED = "Conectado"
    DISCONNECTED = "Desconectado"
    ERROR = "Erro"


@dataclass(frozen=True)
class RobotState:
    connected: bool = False
    busy: bool = False
    allowed: bool = False
    mode: str | None = None
    message: str = "Desconectado. Modo offline"
    connection_state: ConnectionState = ConnectionState.DISCONNECTED
    faults: tuple[RobotFault, ...] = ()

    @property
    def blocking_faults(self):
        return tuple(fault for fault in self.faults if fault.blocks_motion)


@dataclass(frozen=True)
class VideoFrame:
    frame: Any
    received_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class Preview:
    ppm: bytes
    received_at: float


@dataclass(frozen=True)
class VideoState:
    recording: bool = False
    message: str = "Aguardando câmera"
