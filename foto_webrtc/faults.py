"""Estado das falhas recebidas em errors/add_error/rm_error pelo WebRTC."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RobotFault:
    timestamp: int | float
    source: int
    code: int

    @property
    def blocks_motion(self):
        return 300 <= self.source < 400 or self.source == 600

    @property
    def label(self):
        return f"{self.source}-{self.code:X} (0x{self.code:X})"


class FaultTracker:
    def __init__(self):
        self.active = {}
        self.seen = False

    @property
    def faults(self):
        return tuple(self.active[key] for key in sorted(self.active))

    def update(self, message):
        kind = message.get("type")
        if kind not in ("errors", "add_error", "rm_error"):
            return False
        data = message.get("data")
        if not isinstance(data, (list, tuple)):
            raise ValueError("Lista de falhas inválida")
        rows = data if not data or isinstance(data[0], (list, tuple)) else [data]
        faults = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 3:
                raise ValueError("Falha deve conter timestamp, fonte e código")
            timestamp, source, code = row
            if type(timestamp) not in (int, float) or type(source) is not int or type(code) is not int or code <= 0:
                raise ValueError("Valores de falha inválidos")
            faults.append(RobotFault(timestamp, source, code))
        if kind == "errors":
            self.active = {(fault.source, fault.code): fault for fault in faults}
        else:
            for fault in faults:
                key = fault.source, fault.code
                if kind == "add_error":
                    self.active.setdefault(key, fault)
                else:
                    self.active.pop(key, None)
        self.seen = True
        return True
