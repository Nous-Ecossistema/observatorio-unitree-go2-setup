"""Captura e processamento independentes. OpenCV/PyAV ficam fora do Tk."""

import time
from concurrent.futures import TimeoutError
from datetime import datetime
from fractions import Fraction
from queue import Empty

from loguru import logger

from .channels import get_latest, put_latest
from .models import Preview, VideoFrame, VideoState
from .workers import Worker


def nome_unico(prefixo, extensao):
    return f"{prefixo}_{datetime.now():%Y%m%d_%H%M%S_%f}.{extensao}"


class CaptureWorker(Worker):
    def __init__(self, stop_event, robot, tracks, frames):
        super().__init__("go2-capture", stop_event)
        self.robot, self.tracks, self.frames = robot, tracks, frames

    def work(self):
        from aiortc.mediastreams import MediaStreamError

        dropped = 0
        while not self.stop_event.is_set():
            try:
                track = self.tracks.get(timeout=0.1)
            except Empty:
                continue
            future = None
            try:
                while not self.stop_event.is_set():
                    if not self.robot.ready.wait(0.1):
                        continue
                    future = self.robot.receive_frame(track)
                    while not self.stop_event.is_set():
                        try:
                            frame = future.result(timeout=0.1)
                            break
                        except TimeoutError:
                            if future.done():
                                raise
                            continue
                    if self.stop_event.is_set():
                        break
                    if self.frames.full():
                        dropped += 1
                        if dropped % 30 == 1:
                            logger.bind(component="video", event="frames_dropped", count=dropped
                                        ).debug("Frames antigos descartados para manter baixa latência")
                    put_latest(self.frames, VideoFrame(frame))
            except MediaStreamError:
                logger.bind(component="video", event="stream_ended").warning("Fluxo de vídeo encerrado")
            except Exception:
                if not self.stop_event.is_set():
                    raise
            finally:
                if future is not None and not future.done():
                    future.cancel()
                put_latest(self.frames, VideoFrame(None))


class VideoProcessor(Worker):
    def __init__(self, config, stop_event, frames, previews, actions, states, sizes):
        super().__init__("go2-video", stop_event)
        self.config, self.frames, self.previews = config, frames, previews
        self.actions, self.states, self.sizes = actions, states, sizes
        self.image = None
        self.last_received = 0.0
        self.size = (960, 540)
        self.container = None
        self.log = logger.bind(component="video")
        self.state = VideoState()

    def _status(self, message):
        self.state = VideoState(self.container is not None, message)
        put_latest(self.states, self.state)

    def _fresh(self):
        return self.image is not None and time.monotonic() - self.last_received < self.config.frame_timeout

    def _photo(self, command):
        import cv2

        log = self.log.bind(event="photo_result", command_id=command.command_id)
        started = time.monotonic()
        if not self._fresh():
            log.warning("Foto recusada: câmera sem frame recente")
            self._status("Foto: aguarde imagens recentes")
            return
        path = self.config.output_dir / "fotos" / nome_unico("foto_campo", "jpg")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(path), self.image):
                raise OSError("OpenCV não conseguiu gravar o JPG")
            log.bind(path=str(path), duration_ms=(time.monotonic() - started) * 1000).info("Foto salva: {}", path)
            self._status(f"Foto salva: {path.name}")
        except Exception:
            log.exception("Falha ao salvar foto")
            self._status("Falha ao salvar foto; confira os logs")

    def _start_video(self, command):
        import av

        if not self._fresh():
            self.log.bind(event="recording_rejected", command_id=command.command_id).warning("Vídeo recusado: sem frame recente")
            self._status("Vídeo: aguarde imagens recentes")
            return
        height, width = self.image.shape[:2]
        if width % 2 or height % 2:
            self.log.bind(event="recording_rejected").warning("H.264 exige largura e altura pares")
            self._status("H.264 exige resolução par")
            return
        container = None
        try:
            av.codec.Codec("libx264", "w")
            path = self.config.output_dir / "videos" / nome_unico("video_campo", "mp4")
            path.parent.mkdir(parents=True, exist_ok=True)
            container = av.open(str(path), mode="w", format="mp4")
            stream = container.add_stream("libx264", rate=30)
            stream.width, stream.height = width, height
            stream.pix_fmt = "yuv420p"
            stream.time_base = Fraction(1, 30)
            self.container, self.stream, self.video_path = container, stream, path
            self.video_start = time.monotonic()
            self.first_timestamp, self.last_slot, self.recorded_frames = None, -1, 0
            self.log.bind(event="recording_started", command_id=command.command_id,
                          path=str(path)).info("Gravando: {}", path)
            self._status(f"Gravando: {path.name}")
        except Exception:
            if container is not None:
                container.close()
            self.container = None
            self.log.bind(event="recording_start_failed").exception("Falha ao iniciar MP4")
            self._status("Falha ao iniciar vídeo")

    def _record(self, packet):
        import av

        if self.container is None:
            return
        try:
            height, width = self.image.shape[:2]
            if (width, height) != (self.stream.width, self.stream.height):
                raise ValueError("A resolução mudou durante a gravação")
            source = packet.frame
            timestamp = source.pts * source.time_base if source.pts is not None and source.time_base is not None else Fraction(packet.received_at)
            if self.first_timestamp is None:
                self.first_timestamp = timestamp
            slot = round((timestamp - self.first_timestamp) * 30)
            if slot <= self.last_slot:
                return
            # Novo frame: não muda pts/time_base do objeto recebido do RTC.
            frame = av.VideoFrame.from_ndarray(self.image, format="bgr24")
            frame.pts, frame.time_base = slot, Fraction(1, 30)
            for encoded in self.stream.encode(frame):
                self.container.mux(encoded)
            self.last_slot = slot
            self.recorded_frames += 1
        except Exception:
            self.log.bind(event="recording_encode_failed").exception("Falha ao codificar MP4")
            self._stop_video()

    def _stop_video(self):
        if self.container is None:
            return
        container, self.container = self.container, None
        started = time.monotonic()
        try:
            try:
                for encoded in self.stream.encode(None):
                    container.mux(encoded)
            finally:
                container.close()
            self.log.bind(event="recording_finished", path=str(self.video_path),
                          frames=self.recorded_frames, duration_ms=(time.monotonic() - started) * 1000
                          ).info("MP4 finalizado: {} ({} frames)", self.video_path, self.recorded_frames)
            self._status(f"Vídeo salvo: {self.video_path.name}" if self.recorded_frames else "Vídeo sem frames; repita a gravação")
        except Exception:
            self.log.bind(event="recording_finish_failed", path=str(self.video_path)
                          ).exception("Falha ao finalizar MP4; confira o arquivo")
            self._status("Falha ao finalizar MP4")

    def _preview(self):
        import cv2

        image = self.image.copy()
        height, width = image.shape[:2]
        scale = min(max(self.size[0], 1) / width, max(self.size[1], 1) / height, 1.0)
        if scale < 1:
            image = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))))
        if self.container is not None:
            seconds = int(time.monotonic() - self.video_start)
            cv2.putText(image, f"REC {seconds // 60:02d}:{seconds % 60:02d} | V: parar",
                        (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        ppm = f"P6\n{width} {height}\n255\n".encode() + rgb.tobytes()
        put_latest(self.previews, Preview(ppm, self.last_received))

    def work(self):
        self._status("Aguardando câmera")
        try:
            while not self.stop_event.is_set():
                self.size = get_latest(self.sizes, self.size)
                for _ in range(8):
                    try:
                        command = self.actions.get_nowait()
                    except Empty:
                        break
                    if command.name == "Foto":
                        self._photo(command)
                    elif command.name == "Video":
                        self._start_video(command) if self.container is None else self._stop_video()
                try:
                    packet = self.frames.get(timeout=0.02)
                except Empty:
                    if self.container is not None and not self._fresh():
                        self.log.bind(event="recording_stale_frame").warning("Sem frames recentes; finalizando MP4")
                        self._stop_video()
                    continue
                packet = get_latest(self.frames, packet)
                if packet.frame is None:
                    self.image = None
                    self._stop_video()
                    self._status("Fluxo da câmera encerrado")
                    continue
                started = time.monotonic()
                self.image = packet.frame.to_ndarray(format="bgr24")
                self.last_received = packet.received_at
                if self.state.message in ("Aguardando câmera", "Fluxo da câmera encerrado"):
                    self._status("Câmera transmitindo")
                self._record(packet)
                self._preview()
                self.log.bind(event="frame_processed", duration_ms=(time.monotonic() - started) * 1000,
                              frame_age_ms=(time.monotonic() - packet.received_at) * 1000
                              ).trace("Frame processado")
        finally:
            self._stop_video()
