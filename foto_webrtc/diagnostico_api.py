"""Consultas somente leitura das APIs de modo e estado do Go2."""

import asyncio
import json
import os
import sys

from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD, SPORT_CMD_MCF

from .faults import FaultTracker


class DiagnosticConnection(UnitreeWebRTCConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fault_tracker = FaultTracker()

    def _observe_faults(self, raw):
        try:
            message = json.loads(raw) if isinstance(raw, str) else self.datachannel.deal_array_buffer(raw)
            if isinstance(message, dict):
                self.fault_tracker.update(message)
        except (TypeError, ValueError, KeyError):
            pass

    async def get_answer_from_local_peer(self, pc, ip):
        self.datachannel.pub_sub.channel.on("message", self._observe_faults)
        return await super().get_answer_from_local_peer(pc, ip)


async def query_lowstate(pub_sub, timeout=3):
    future = asyncio.get_running_loop().create_future()

    def receive(message):
        data = message.get("data")
        if not future.done() and isinstance(data, dict):
            future.set_result(data)

    print("\nTelemetria dos motores (LowState):")
    pub_sub.subscribe(RTC_TOPIC["LOW_STATE"], receive)
    try:
        data = await asyncio.wait_for(future, timeout)
        motors = data.get("motor_state", [])
        report = {"bateria_percentual": data.get("bms_state", {}).get("soc"),
                  "motores": [{"indice": index, "temperatura": motor.get("temperature"),
                               "lost": motor.get("lost"), "reserve": motor.get("reserve")}
                              for index, motor in enumerate(motors) if isinstance(motor, dict)]}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    except TimeoutError:
        print("LowState não recebido no prazo; temperaturas não confirmadas.")
        return None
    finally:
        pub_sub.unsubscribe(RTC_TOPIC["LOW_STATE"])


def mode_from_response(response):
    if response["data"]["header"]["status"]["code"] != 0:
        return None
    data = response["data"]["data"]
    return (json.loads(data) if isinstance(data, str) else data)["name"]


async def query_diagnostics(pub_sub):
    async def query(label, topic, payload):
        print(f"\n{label}:")
        response = await asyncio.wait_for(pub_sub.publish_request_new(topic, payload), timeout=8)
        print(json.dumps(response, ensure_ascii=False, indent=2, default=str))
        return response

    response = await query("Modo atual", RTC_TOPIC["MOTION_SWITCHER"], {"api_id": 1001})
    try:
        mode = mode_from_response(response)
    except (KeyError, TypeError, ValueError):
        mode = None
    if mode not in ("normal", "mcf"):
        print("Estado não consultado: modo não reconhecido ou consulta rejeitada.")
        return
    apis = SPORT_CMD_MCF if mode == "mcf" else SPORT_CMD
    response = await query("Estado informado pelo robô", RTC_TOPIC["SPORT_MOD"],
                           {"api_id": apis["GetState"], "parameter": ["state", "bodyHeight", "speedLevel", "gait"]})
    if not response.get("data", {}).get("data"):
        print("A consulta não retornou dados de estado; a postura física não foi confirmada.")


async def main():
    connection = DiagnosticConnection(
        WebRTCConnectionMethod.LocalAP,
        aes_128_key=os.environ.get("UNITREE_AES_128_KEY"),
    )
    try:
        await asyncio.wait_for(connection.connect(), timeout=35)
        await query_diagnostics(connection.datachannel.pub_sub)
        await query_lowstate(connection.datachannel.pub_sub)
        print("\nFalhas ativas observadas no canal:")
        for fault in connection.fault_tracker.faults:
            print(f"  {fault.label}; timestamp informado pelo robô: {fault.timestamp}")
        if not connection.fault_tracker.faults:
            print("  Snapshot sem falhas." if connection.fault_tracker.seen else
                  "  Nenhum snapshot recebido; ausência de falhas não confirmada.")
    except TimeoutError as error:
        print(f"TIMEOUT: o Go2 não respondeu a uma das consultas de diagnóstico: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    finally:
        await connection.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
