"""Producer/Consumer: converte snapshots do teclado em comandos a 20 Hz."""

import time

from loguru import logger

from .channels import get_latest
from .models import Command, InputSnapshot, RobotState
from .workers import Worker


MOVEMENT_KEYS = {key: key for key in "wsadqe"} | {
    "up": "w", "down": "s", "left": "q", "right": "e",
    "turn_left": "q", "turn_right": "e",
}


class LocomotionWorker(Worker):
    def __init__(self, config, stop_event, inputs, states, mailbox):
        super().__init__("go2-locomotion", stop_event)
        self.config, self.inputs, self.states, self.mailbox = config, inputs, states, mailbox

    def work(self):
        snapshot, state = InputSnapshot(), RobotState()
        was_moving = False
        denied = False
        period = 1 / self.config.publish_hz
        deadline = time.monotonic()
        while not self.stop_event.is_set():
            snapshot = get_latest(self.inputs, snapshot)
            state = get_latest(self.states, state)
            now = time.monotonic()
            generation = self.mailbox.generation
            fresh = now - snapshot.timestamp < self.config.input_timeout
            permitted = state.connected and state.allowed and not state.busy and not state.blocking_faults
            current = snapshot.generation == generation
            keys = {MOVEMENT_KEYS[key] for key in snapshot.keys if key in MOVEMENT_KEYS}
            if keys and not permitted and not denied:
                logger.bind(component="commands", event="movement_inhibited",
                            mode=state.mode, busy=state.busy, connected=state.connected,
                            keys=sorted(keys)).warning("Locomoção suspensa: {}", state.message)
            denied = bool(keys) and not permitted
            x = self.config.linear_gain * (("w" in keys) - ("s" in keys))
            y = self.config.linear_gain * (("a" in keys) - ("d" in keys))
            angular_gain = self.config.angular_gain if snapshot.angular_gain is None else snapshot.angular_gain
            z = angular_gain * (("q" in keys) - ("e" in keys))
            moving = permitted and fresh and current and bool(x or y or z)
            if moving:
                self.mailbox.move(Command.move(x, y, z, generation=generation,
                    valid_until=snapshot.timestamp + self.config.input_timeout))
                if not was_moving:
                    logger.bind(component="commands", event="movement_started", keys=sorted(keys),
                                parameters={"x": x, "y": y, "z": z}, generation=generation
                                ).info("Locomoção solicitada pelo teclado: x={}, y={}, z={}", x, y, z)
            elif was_moving:
                # Soltar tecla / perder heartbeat zera uma vez; não envia Move
                # continuamente durante uma postura, deitado ou amortecido.
                self.mailbox.halt("heartbeat expirou" if not fresh else "teclas liberadas")
            was_moving = moving
            deadline += period
            if deadline < time.monotonic():
                deadline = time.monotonic() + period
            self.stop_event.wait(max(0, deadline - time.monotonic()))
