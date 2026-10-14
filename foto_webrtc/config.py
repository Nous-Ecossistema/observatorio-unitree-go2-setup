"""Configuração imutável compartilhada pelos componentes."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    output_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "capturas_go2")
    log_dir: Path = field(default_factory=lambda: Path("logs"))
    log_level: str = "INFO"
    linear_gain: float = 0.3
    angular_gain: float = 0.5
    publish_hz: float = 20.0
    request_timeout: float = 5.0
    connect_timeout: float = 30.0
    disconnect_timeout: float = 5.0
    input_timeout: float = 0.3
    frame_timeout: float = 2.0
    shutdown_timeout: float = 10.0
    mode_settle: float = 5.0
    posture_delays: tuple = (("StandUp", 3.0), ("StandDown", 3.0),
                             ("RecoveryStand", 5.0), ("Hello", 2.0), ("Damp", 0.5))

    def __post_init__(self):
        import math

        for name in ("linear_gain", "angular_gain", "publish_hz", "request_timeout",
                     "connect_timeout", "disconnect_timeout", "input_timeout",
                     "frame_timeout", "shutdown_timeout"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} deve ser positivo e finito")
        if self.publish_hz > 100:
            raise ValueError("publish_hz deve ser no máximo 100")
