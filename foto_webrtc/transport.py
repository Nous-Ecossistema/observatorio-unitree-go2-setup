"""Adapter WebRTC. Somente a thread RTC acessa o driver e seu event loop.

As respostas são observadas por um listener adicional do canal. Move não
cria Futures no resolver do driver: nosso registro limitado expira pedidos
sem cancelar Futures que o SDK tentaria completar tardiamente.
"""

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from itertools import count
from typing import Protocol

from loguru import logger

from .channels import put_latest


class RobotTransport(Protocol):
    async def connect(self): ...
    def send(self, command, wait_response=False): ...
    def tick(self): ...
    async def disconnect(self): ...


@dataclass
class Pending:
    command: object
    topic: str
    api_id: int
    started: float
    future: object = None


class WebRTCTransport:
    MAX_PENDING = 256

    def __init__(self, config, tracks, on_failure):
        self.config = config
        self.tracks = tracks
        self.on_failure = on_failure
        self.connection = None
        self.pub_sub = None
        self.pending = OrderedDict()
        self.expired = OrderedDict()
        self.ids = count(int(time.time() * 1000) % 2147483648)
        self.log = logger.bind(component="rtc")
        self.seen_tracks = set()

    async def connect(self):
        from importlib.metadata import version
        from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod
        from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD
        from .signaling import exchange_local_sdp

        request_timeout = self.config.request_timeout

        class LocalConnection(UnitreeWebRTCConnection):
            async def get_answer_from_local_peer(self, pc, ip):
                description = pc.localDescription
                payload = json.dumps({"id": "", "sdp": description.sdp,
                                      "type": description.type, "token": self.token})
                return await asyncio.to_thread(exchange_local_sdp, ip, payload,
                                               self.aes_128_key, min(5.0, request_timeout))

        self.topics, self.apis = RTC_TOPIC, SPORT_CMD
        import os

        self.connection = LocalConnection(WebRTCConnectionMethod.LocalAP,
            aes_128_key=os.environ.get("UNITREE_AES_128_KEY"))
        started = time.monotonic()
        self.log.bind(event="connecting", driver_version=version("unitree-webrtc-connect"),
                      connection_method="LocalAP").info("Conectando ao Go2 via Wi-Fi AP")
        await asyncio.wait_for(self.connection.connect(), self.config.connect_timeout)
        self.pub_sub = self.connection.datachannel.pub_sub
        self.channel = self.pub_sub.channel
        if self.channel.readyState != "open":
            raise ConnectionError("Canal WebRTC não abriu")
        self.channel.on("message", self._on_message)
        self.connection.video.add_track_callback(self._on_track)
        # O driver pode receber a trilha durante connect(), antes do registro
        # do callback. Recolhe também os receivers já negociados.
        for receiver in self.connection.pc.getReceivers():
            if receiver.track is not None:
                await self._on_track(receiver.track)
        self.connection.video.switchVideoChannel(True)
        self.log.bind(event="connected", duration_ms=(time.monotonic() - started) * 1000,
                      channel_state=self.channel.readyState,
                      apis={name: self.apis.get(name) for name in
                            ("Move", "StandUp", "StandDown", "RecoveryStand", "Damp", "Hello")}
                      ).info("WebRTC conectado; U: levantar, M: consultar modo")

    async def _on_track(self, track):
        if track.kind == "video" and track.id not in self.seen_tracks:
            self.seen_tracks.add(track.id)
            put_latest(self.tracks, track)
            self.log.bind(event="video_track_received").info("Trilha de vídeo recebida")

    def _route(self, command):
        if command.name == "Consultar":
            return self.topics["MOTION_SWITCHER"], 1001
        if command.name == "ModoNormal":
            return self.topics["MOTION_SWITCHER"], 1002
        return self.topics["SPORT_MOD"], self.apis[command.name]

    def _log(self, pending, request_id, **extra):
        return self.log.bind(command_id=pending.command.command_id, request_id=request_id,
            parent_command_id=pending.command.parent_command_id,
            command=pending.command.name, parameters=pending.command.parameter,
            topic=pending.topic, api_id=pending.api_id, **extra)

    def send(self, command, wait_response=False):
        if self.channel.readyState != "open":
            raise ConnectionError(f"Canal WebRTC está {self.channel.readyState}")
        # Evita enfileirar indefinidamente no SCTP durante degradação de rede.
        is_stop = command.name == "Move" and not any(command.parameter.values())
        if getattr(self.channel, "bufferedAmount", 0) > 256 * 1024 and not is_stop:
            raise BufferError("Backpressure: buffer WebRTC acima de 256 KiB")
        if len(self.pending) >= self.MAX_PENDING:
            if not is_stop:
                raise BufferError("Limite de pedidos WebRTC sem resposta atingido")
            request_id, pending = self.pending.popitem(last=False)
            self._finish(request_id, pending, "superseded", None)
        topic, api_id = self._route(command)
        request_id = next(self.ids)
        started = time.monotonic()
        future = asyncio.get_running_loop().create_future() if wait_response else None
        pending = Pending(command, topic, api_id, started, future)
        self.pending[request_id] = pending
        payload = {"header": {"identity": {"id": request_id, "api_id": api_id}},
                   "parameter": json.dumps(command.parameter) if command.parameters else ""}
        log = self._log(pending, request_id, event="command_sent",
                        queue_ms=(started - command.created_at) * 1000)
        try:
            self.pub_sub.publish_without_callback(topic, payload, msg_type="req")
        except Exception:
            self.pending.pop(request_id)
            if future is not None:
                future.cancel()
            log.bind(event="send_failed").exception("Falha no envio de {}", command.name)
            raise
        log.log("DEBUG" if command.name == "Move" else "INFO", "Comando enviado: {}", command.name)
        return future

    def _on_message(self, raw):
        try:
            response = json.loads(raw) if isinstance(raw, str) else self.connection.datachannel.deal_array_buffer(raw)
            if not isinstance(response, dict):
                return
            if response.get("type") in ("errors", "add_error", "rm_error"):
                # O SDK só imprime estes avisos; preserva-os também em JSONL.
                self.log.bind(event="robot_error", response=response).log(
                    "WARNING" if response.get("data") and response["type"] != "rm_error" else "INFO",
                    "Diagnóstico do robô: {}", response["type"])
                return
            if response.get("type") != "res":
                return
            identity = response.get("data", {}).get("header", {}).get("identity", {})
            request_id = identity.get("id")
            pending = self.pending.get(request_id)
            if pending is None:
                pending = self.expired.pop(request_id, None)
                if pending is not None:
                    self._log(pending, request_id, event="late_response", response=response,
                              duration_ms=(time.monotonic() - pending.started) * 1000
                              ).warning("Resposta tardia; estado de locomoção permanece suspenso")
                return
            if response.get("topic") != pending.topic:
                return  # outro tópico não pode completar este pedido
            self.pending.pop(request_id)
            code = response.get("data", {}).get("header", {}).get("status", {}).get("code")
            if identity.get("api_id") != pending.api_id or type(code) is not int:
                status = "malformed"
            else:
                status = "accepted" if code == 0 else "rejected"
            self._finish(request_id, pending, status, response)
        except (TypeError, ValueError, KeyError, AttributeError) as error:
            self.log.bind(event="invalid_message", error=str(error)).warning("Mensagem RTC inválida")
        except Exception:
            self.log.bind(event="response_handler_failed").exception("Falha ao tratar resposta RTC")

    def _finish(self, request_id, pending, status, response):
        duration = (time.monotonic() - pending.started) * 1000
        log = self._log(pending, request_id, event="command_result", status=status,
                        response=response, duration_ms=duration)
        log.log("DEBUG" if status == "accepted" and pending.command.name == "Move" else
                "INFO" if status == "accepted" else "WARNING",
                "Resultado de {}: {} ({:.1f} ms)", pending.command.name, status, duration)
        if pending.future is not None and not pending.future.done():
            pending.future.set_result(response if status == "accepted" else None)
        if status in ("timeout", "rejected", "malformed"):
            self.on_failure(pending.command, status)

    def tick(self):
        if self.pub_sub is not None and self.channel.readyState != "open":
            raise ConnectionError(f"Canal WebRTC está {self.channel.readyState}")
        now = time.monotonic()
        for request_id, pending in list(self.pending.items()):
            if now - pending.started >= self.config.request_timeout:
                self.pending.pop(request_id)
                self.expired[request_id] = pending
                self._finish(request_id, pending, "timeout", None)
        while len(self.expired) > 64:
            self.expired.popitem(last=False)

    async def disconnect(self):
        if self.connection is not None:
            try:
                if self.pub_sub is not None:
                    try:
                        self.connection.video.switchVideoChannel(False)
                    except Exception:
                        self.log.bind(event="video_disable_failed").exception("Falha ao desativar vídeo; desconectando")
                await asyncio.wait_for(self.connection.disconnect(), self.config.disconnect_timeout)
            finally:
                if self.pub_sub is not None:
                    try:
                        self.channel.remove_listener("message", self._on_message)
                    except (KeyError, ValueError):
                        pass
                for request_id, pending in list(self.pending.items()):
                    self._finish(request_id, pending, "cancelled", None)
                self.pending.clear()
                self.expired.clear()
                self.log.bind(event="disconnected").info("WebRTC desconectado")
