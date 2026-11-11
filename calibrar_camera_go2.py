#!/usr/bin/env python3
"""Calibração independente da câmera Go2; não importa código deste projeto.

Exemplos (execute na pasta deste arquivo):
  .venv/bin/python calibrar_camera_go2.py
  .venv/bin/python calibrar_camera_go2.py --verificar-dependencias
  .venv/bin/python calibrar_camera_go2.py --ip IP_DO_ROBO --modelo fisheye
  .venv/bin/python calibrar_camera_go2.py --conexao ap --modelo pinhole
  .venv/bin/python calibrar_camera_go2.py --imagens "fotos/fotos tabuleiro" --prefixo calibracao_SESSAO_ --modelo pinhole

Edite o bloco CONFIGURAÇÃO abaixo antes de executar: modelo, medida REAL de
um quadrado em mm, tipo de conexão e IP. Não há perguntas no terminal.
As opções de linha de comando, quando usadas, substituem essa configuração.
O padrão tem 5 colunas x 8 linhas de cantos INTERNOS (40 cantos; 6 x 9 quadrados).
Mova manualmente somente o tabuleiro: varie posição, distância e inclinação,
incluindo as bordas da imagem, com boa luz, foco e tabuleiro plano e inteiro.
Colete 20–30 vistas variadas; o mínimo operacional é 10, sem garantia de qualidade.

Janela: ESPAÇO salva a foto e verifica os cantos; C calibra; U desfaz a última foto;
Q/ESC encerra sem calibrar. Clique na janela para receber as teclas.
Todas as fotos ficam em fotos/fotos tabuleiro/, mesmo sem cantos ou com coleta interrompida.
Somente vistas com todos os cantos detectados e sem repetição entram no ajuste.
--imagens permite reutilizá-las, inclusive em terminal sem interface gráfica.

Os PNGs contêm o quadro WebRTC decodificado em BGR, na resolução recebida,
sem undistort, remap, recorte, resize ou sobreposição. Não são RAW do sensor:
o firmware/encoder do robô pode processar o vídeo antes de transmiti-lo.
Apenas a cópia mostrada na janela recebe desenhos. A conexão usa sinalização,
autenticação e heartbeat do SDK e ativa/desativa o vídeo; não envia comandos
de locomoção, postura ou modo esportivo. Nenhuma conexão ocorre ao importar.

Saída: uma pasta nova por execução, com calibracao.npz e calibracao.json.
K e D usam o modelo explicitado em model; image_size = [largura, altura].
O erro RMS é sqrt(sum(||p_observado - p_projetado||²) / total_de_cantos), em
pixels. É medido nas próprias vistas de ajuste e não garante erro em novas
imagens. D pinhole: [k1,k2,p1,p2,k3]; D fisheye: [k1,k2,k3,k4].
Os arquivos NPZ/JSON são próprios deste script, não arquivos INI/YAML do ROS.
NPZ pode ser aberto com numpy.load(..., allow_pickle=False).

Dependências: numpy, opencv-python com GUI para coleta, aiortc, av,
unitree-webrtc-connect. Nenhuma instalação é feita automaticamente.
Se o firmware exigir chave AES, forneça GO2_AES_128_KEY no ambiente;
a chave não é impressa nem salva por este script.

Referências:
  https://docs.opencv.org/4.x/dc/dbb/tutorial_py_calibration.html
  https://docs.opencv.org/4.x/db/d58/group__calib3d__fisheye.html
  https://github.com/legion1581/unitree_webrtc_connect
  https://wiki.ros.org/camera_calibration_parsers
  https://github.com/clydemcqueen/opencv_cam
"""

import argparse
import asyncio
from datetime import datetime, timezone
import importlib
import importlib.metadata
import ipaddress
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time


# ================= CONFIGURAÇÃO: =================
MODELO = "pinhole"              # "fisheye" ou "pinhole"; não é seleção automática.
QUADRADO_MM = 30.0               # Lado real de um quadrado, em milímetros.
CONEXAO = "ap"                  # LocalAP, como em foto_webrtc/foto_webrtc.py.
IP_ROBO = "192.168.12.1"         # Endereço fixo usado pelo SDK em modo AP.
PASTA_SAIDA = "calibracoes_go2"   # Cada execução cria uma subpasta nova.
PASTA_FOTOS = Path(__file__).resolve().parent / "fotos" / "fotos tabuleiro"
# ===========================================================

BOARD = (5, 8)  # (colunas, linhas) de cantos internos; 6 x 9 quadrados.
CORNER_COUNT = BOARD[0] * BOARD[1]
MIN_VIEWS = 10
WINDOW = f"Go2 - calibracao {BOARD[0]}x{BOARD[1]} - video original"
cv2 = None
np = None


def positive_number(value):
    number = float(str(value).strip().replace(",", "."))
    if not math.isfinite(number) or number <= 0:
        raise ValueError("informe um número positivo e finito")
    return number


def positive_argument(value):
    try:
        return positive_number(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def dependencies(report=False, live=True):
    """Verifica imports reais; importar o SDK não estabelece uma conexão."""
    global cv2, np
    packages = [("numpy", "numpy"), ("cv2", "opencv-python")]
    if live:
        packages += [("aiortc", "aiortc"), ("av", "av"),
                     ("unitree_webrtc_connect.webrtc_driver", "unitree-webrtc-connect")]
    failures = []
    for module, distribution in packages:
        try:
            loaded = importlib.import_module(module)
            if module == "numpy":
                np = loaded
            elif module == "cv2":
                cv2 = loaded
            try:
                version = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                version = getattr(loaded, "__version__", "versão não identificada")
            if report:
                print(f"OK: {distribution} {version}")
        except Exception as exc:
            failures.append(f"{distribution}: {type(exc).__name__}: {exc}")
    if failures:
        raise RuntimeError(
            "Dependências indisponíveis:\n  " + "\n  ".join(failures)
            + "\nUse o Python da .venv. Se necessário, instale nesse ambiente:"
            "\n  .venv/bin/python -m pip install numpy opencv-python unitree-webrtc-connect"
            "\nEvite instalar variantes OpenCV headless e com GUI juntas."
        )
    required = ("findChessboardCornersSB", "cornerSubPix", "calibrateCamera", "fisheye")
    if not all(hasattr(cv2, name) for name in required):
        raise RuntimeError("OpenCV sem as APIs de calibração necessárias.")
    fisheye_flags = (cv2.fisheye if hasattr(cv2.fisheye, "CALIB_FIX_SKEW") else cv2)
    if not all(hasattr(fisheye_flags, name) for name in (
        "CALIB_RECOMPUTE_EXTRINSIC", "CALIB_CHECK_COND", "CALIB_FIX_SKEW"
    )) or not all(hasattr(cv2.fisheye, name) for name in ("calibrate", "projectPoints")):
        raise RuntimeError("OpenCV sem funções ou flags fisheye compatíveis.")
    if report:
        gui = [line.strip() for line in cv2.getBuildInformation().splitlines()
               if "GUI:" in line]
        print("OpenCV: pinhole, fisheye e detector SB disponíveis.")
        print("; ".join(gui))
        print("DISPLAY/WAYLAND_DISPLAY:",
              "definido" if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
              else "ausente (a coleta precisa de uma sessão gráfica)")
        print("Verificação local concluída; nenhuma conexão com o robô.")


def detect_corners(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    found, corners = cv2.findChessboardCornersSB(
        gray, BOARD, flags=cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_EXHAUSTIVE
    )
    if not found:
        found, corners = cv2.findChessboardCorners(
            gray, BOARD, cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
        )
        if found:
            corners = cv2.cornerSubPix(
                gray, corners, (5, 5), (-1, -1),
                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 40, 0.001),
            )
    if not found or corners is None or len(corners) != CORNER_COUNT:
        return None
    corners = np.asarray(corners, dtype=np.float32).reshape(-1, 1, 2)
    return corners if np.isfinite(corners).all() else None


def repeated_view(corners, accepted, image_size):
    """Evita capturas quase iguais, inclusive ordem invertida em 180 graus."""
    diagonal = math.hypot(*image_size)
    for previous in accepted:
        distance = min(float(np.sqrt(np.mean(np.sum((corners - p) ** 2, axis=2))))
                       for p in (previous, previous[::-1]))
        if distance < 0.005 * diagonal:
            return True
    return False


class OriginalVideo:
    """Recepção asyncio separada da GUI e da detecção, sem fila de quadros antigos."""

    def __init__(self, connection_mode, ip):
        self.connection_mode = connection_mode
        self.ip = ip
        self.lock = threading.Lock()
        self.frame = None
        self.received_at = 0.0
        self.error = None
        self.loop = None
        self.task = None
        self.done = threading.Event()
        self.stop_requested = threading.Event()
        self.thread = threading.Thread(target=self._worker, daemon=True)

    def start(self):
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return (None if self.frame is None else self.frame.copy(), self.received_at)

    def _worker(self):
        try:
            asyncio.run(self._receive())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.done.set()

    async def _receive(self):
        from unitree_webrtc_connect.webrtc_driver import (
            UnitreeWebRTCConnection, WebRTCConnectionMethod,
        )

        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        if self.stop_requested.is_set():
            return
        method = (WebRTCConnectionMethod.LocalAP if self.connection_mode == "ap"
                  else WebRTCConnectionMethod.LocalSTA)
        options = {"ip": self.ip} if self.connection_mode == "sta" else {}
        key = os.environ.get("GO2_AES_128_KEY")
        if key:
            if len(key) != 32 or any(c not in "0123456789abcdefABCDEF" for c in key):
                raise ValueError("GO2_AES_128_KEY deve conter 32 caracteres hexadecimais.")
            options["aes_128_key"] = key
        conn = UnitreeWebRTCConnection(method, **options)
        video_enabled = False
        ended = asyncio.Event()

        async def on_video(track):
            try:
                while True:
                    frame = await track.recv()
                    original = frame.to_ndarray(format="bgr24")
                    with self.lock:
                        self.frame = original
                        self.received_at = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = f"Vídeo interrompido ({type(exc).__name__})."
            finally:
                ended.set()

        try:
            await asyncio.wait_for(conn.connect(), timeout=45)
            if self.stop_requested.is_set():
                return
            conn.video.add_track_callback(on_video)
            conn.video.switchVideoChannel(True)
            video_enabled = True
            # O sinal local também é consultado periodicamente para encerrar
            # mesmo quando não há mais eventos de rede chegando ao loop.
            while not ended.is_set() and not self.stop_requested.is_set():
                await asyncio.sleep(0.1)
        finally:
            if video_enabled:
                try:
                    conn.video.switchVideoChannel(False)
                except Exception:
                    pass  # O canal pode já ter fechado.
            try:
                await asyncio.wait_for(conn.disconnect(), timeout=5)
            except Exception:
                pass

    def close(self):
        self.stop_requested.set()
        if self.loop is not None and not self.done.is_set():
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass  # Loop já encerrado.
        self.thread.join(timeout=8)
        if self.thread.is_alive():
            print("Aviso: a conexão ainda está encerrando; não foram enviados comandos de movimento.")


def collect_live(args, session):
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        raise RuntimeError("Sem sessão gráfica. Execute localmente com GUI para coletar,"
                           " ou use --imagens para recalibrar imagens já capturadas.")
    if any("GUI:" in line and "NONE" in line
           for line in cv2.getBuildInformation().splitlines()):
        raise RuntimeError("OpenCV headless: a coleta precisa de opencv-python com GUI.")
    raw_dir = args.pasta_fotos.expanduser().resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    corners_list, files = [], []
    captures = []  # (caminho, entrou na calibração); inclui fotos sem detecção.
    sequence = 0
    status = "Clique nesta janela e pressione ESPACO para salvar uma foto."
    last_preview, preview_until = None, 0.0
    image_size = None
    video = OriginalVideo(args.conexao, args.ip)
    print("\nMova o TABULEIRO manualmente; mantenha o robô parado.")
    print("ESPAÇO: salvar foto | C: calibrar | U: desfazer última foto | Q/ESC: sair sem calibrar")
    print(f"Tabuleiro: {BOARD[0]}x{BOARD[1]} cantos internos ({CORNER_COUNT} pontos).")
    print(f"Mínimo: {MIN_VIEWS}; procure coletar 20–30 vistas variadas.")
    print(f"Imagens preservadas em: {raw_dir}")
    try:
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW, 960, 640)  # Só a janela; dados permanecem originais.
        video.start()
        start = time.monotonic()
        while True:
            if video.error:
                raise RuntimeError(f"Falha WebRTC: {video.error}")
            if video.done.is_set():
                raise RuntimeError("A conexão WebRTC foi encerrada.")
            frame, received_at = video.snapshot()
            now = time.monotonic()
            if frame is None:
                if now - start > 60:
                    raise RuntimeError("Nenhum quadro recebido em 60 s. Verifique IP, rede,"
                                       " chave AES e outra sessão usando a câmera.")
            else:
                if now - received_at > 10:
                    raise RuntimeError("Vídeo sem novos quadros há mais de 10 s.")
                current_size = (frame.shape[1], frame.shape[0])
                if image_size is not None and current_size != image_size:
                    raise RuntimeError("A resolução mudou durante a coleta. As imagens"
                                       " anteriores foram preservadas; inicie outra sessão.")
                preview = (last_preview if now < preview_until else frame).copy()
                cv2.putText(preview, f"{len(captures)} fotos | {len(files)} validas | ESPACO: foto | C: calibrar | U: desfazer",
                            (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
                cv2.putText(preview, f"{current_size[0]}x{current_size[1]} | {args.modelo} | Q: sair",
                            (12, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
                cv2.putText(preview, status, (12, 82), cv2.FONT_HERSHEY_SIMPLEX,
                            0.5, (0, 255, 255), 2)
                cv2.imshow(WINDOW, preview)
            key = cv2.waitKey(15) & 0xFF
            if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                return None
            if key in (27, ord("q"), ord("Q")):
                return None
            if key in (ord("u"), ord("U")) and captures:
                removed, accepted = captures[-1]
                removed.unlink()  # Apenas o PNG gerado por esta coleta.
                captures.pop()
                if accepted:
                    files.pop()
                    corners_list.pop()
                preview_until = 0.0
                status = f"Ultima foto removida | {len(captures)} fotos | {len(files)} validas"
                print(f"Última foto removida; restam {len(captures)} fotos e {len(files)} vistas válidas.")
            elif key in (ord("c"), ord("C")):
                if len(files) < MIN_VIEWS:
                    status = f"Faltam vistas validas: {len(files)}/{MIN_VIEWS} | varie o tabuleiro"
                    print(f"Colete ao menos {MIN_VIEWS} vistas válidas; há {len(files)} em {len(captures)} fotos.")
                else:
                    return corners_list, image_size, files
            elif key == 32:
                if frame is None:
                    print("Ainda não há imagem da câmera; aguarde o vídeo aparecer.")
                    continue
                if now - received_at > 1:
                    status = "Video sem quadro recente; aguarde atualizar."
                    print("Quadro antigo; aguarde o vídeo atualizar.")
                    continue
                # Salva ANTES da detecção: não encontrar o tabuleiro não
                # pode descartar a foto que a pessoa pediu para capturar.
                sequence += 1
                path = raw_dir / f"calibracao_{session.name}_vista_{sequence:03d}.png"
                if not cv2.imwrite(str(path), frame):
                    raise OSError(f"Não foi possível salvar {path}")
                image_size = current_size
                captures.append((path, False))
                print(f"Foto salva: {path.resolve()}", flush=True)
                last_preview = frame.copy()
                corners = detect_corners(frame)
                preview_until = time.monotonic() + 1.0
                if corners is None:
                    status = f"Foto {sequence:03d} SALVA | {CORNER_COUNT} cantos nao detectados"
                    print(f"Foto preservada, mas fora da calibração: {CORNER_COUNT} cantos não detectados. "
                          f"Mostre todo o tabuleiro {BOARD[0]}x{BOARD[1]}, com boa luz.")
                    continue
                cv2.drawChessboardCorners(last_preview, BOARD, corners, True)
                if repeated_view(corners, corners_list, current_size):
                    status = f"Foto {sequence:03d} SALVA | vista quase repetida"
                    print("Foto preservada, mas fora da calibração: vista quase repetida. "
                          "Mude a posição, distância ou inclinação do tabuleiro.")
                    continue
                captures[-1] = (path, True)
                corners_list.append(corners.copy())
                files.append(str(path.resolve()))
                status = f"Foto {sequence:03d} SALVA | {CORNER_COUNT} cantos OK | valida para calibrar"
                print(f"Vista {len(files)} aceita: {path.name} ({image_size[0]}x{image_size[1]}).")
    finally:
        if video.thread.ident is not None:
            video.close()
        cv2.destroyAllWindows()


def collect_files(folder, prefix=""):
    """Reutiliza imagens sem alterar os arquivos fornecidos, sem conectar ao robô."""
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"Pasta de imagens inexistente: {folder}")
    images = sorted(p for p in folder.iterdir()
                    if p.is_file() and p.name.startswith(prefix)
                    and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"))
    corners_list, files = [], []
    image_size = None
    for path in images:
        frame = cv2.imread(str(path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if frame is None:
            print(f"Ignorada (não foi possível ler): {path.name}")
            continue
        size = (frame.shape[1], frame.shape[0])
        if image_size is not None and size != image_size:
            raise ValueError(f"Resoluções diferentes: {path.name} tem {size}, esperado {image_size}.")
        corners = detect_corners(frame)
        if corners is None:
            print(f"Ignorada ({CORNER_COUNT} cantos não detectados): {path.name}")
            continue
        if repeated_view(corners, corners_list, size):
            print(f"Ignorada (vista quase repetida): {path.name}")
            continue
        image_size = size
        corners_list.append(corners)
        files.append(str(path))
        print(f"Aceita: {path.name}")
    if len(files) < MIN_VIEWS:
        raise ValueError(f"Apenas {len(files)} vistas válidas; são necessárias ao menos {MIN_VIEWS}.")
    return corners_list, image_size, files


def calibrate(corners_list, image_size, square_mm, model):
    if model not in ("pinhole", "fisheye"):
        raise ValueError("Modelo deve ser pinhole ou fisheye.")
    square_mm = positive_number(square_mm)
    if len(corners_list) < MIN_VIEWS:
        raise ValueError(f"São necessárias ao menos {MIN_VIEWS} vistas.")
    if image_size is None or len(image_size) != 2 or min(image_size) <= 0:
        raise ValueError("Resolução inválida.")
    for corners in corners_list:
        if np.asarray(corners).shape != (CORNER_COUNT, 1, 2) or not np.isfinite(corners).all():
            raise ValueError(f"Cada vista deve conter {CORNER_COUNT} cantos finitos "
                             f"com shape ({CORNER_COUNT},1,2).")
    obj = np.zeros((CORNER_COUNT, 3), np.float64)
    obj[:, :2] = np.mgrid[0:BOARD[0], 0:BOARD[1]].T.reshape(-1, 2) * square_mm
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 200, 1e-8)
    if model == "pinhole":
        objects = [obj.astype(np.float32) for _ in corners_list]
        observations = [np.asarray(p, dtype=np.float32) for p in corners_list]
        flags = 0
        rms, K, D, rvecs, tvecs = cv2.calibrateCamera(
            objects, observations, image_size, None, None, flags=flags, criteria=criteria
        )
        project = cv2.projectPoints
        coefficient_names = ["k1", "k2", "p1", "p2", "k3"]
    else:
        objects = [obj.reshape(1, -1, 3).copy() for _ in corners_list]
        observations = [np.asarray(p, dtype=np.float64).reshape(1, -1, 2) for p in corners_list]
        # OpenCV 4 expõe estas flags em cv2.fisheye; OpenCV 5, em cv2.
        flag_namespace = (cv2.fisheye if hasattr(cv2.fisheye, "CALIB_FIX_SKEW") else cv2)
        flags = (flag_namespace.CALIB_RECOMPUTE_EXTRINSIC | flag_namespace.CALIB_CHECK_COND
                 | flag_namespace.CALIB_FIX_SKEW)
        rms, K, D, rvecs, tvecs = cv2.fisheye.calibrate(
            objects, observations, image_size, np.zeros((3, 3), np.float64),
            np.zeros((4, 1), np.float64), flags=flags, criteria=criteria
        )
        project = cv2.fisheye.projectPoints
        coefficient_names = ["k1", "k2", "k3", "k4"]
    if (not math.isfinite(rms) or not np.isfinite(K).all() or not np.isfinite(D).all()
            or K[0, 0] <= 0 or K[1, 1] <= 0):
        raise ValueError("Calibração numericamente inválida; parâmetros não serão salvos.")
    per_view = []
    for points, observed, rvec, tvec in zip(objects, observations, rvecs, tvecs):
        projected, _ = project(points, rvec, tvec, K, D)
        difference = observed.reshape(-1, 2).astype(np.float64) - projected.reshape(-1, 2)
        per_view.append(float(np.sqrt(np.mean(np.sum(difference ** 2, axis=1)))))
    measured_rms = float(np.sqrt(np.mean(np.square(per_view))))  # Mesmo número de cantos por vista.
    if not math.isfinite(measured_rms):
        raise ValueError("Erro de reprojeção inválido; parâmetros não serão salvos.")
    return {
        "model": model, "K": K.tolist(), "D": D.reshape(-1).tolist(),
        "distortion_coefficient_names": coefficient_names,
        "image_size": list(image_size), "image_width": int(image_size[0]),
        "image_height": int(image_size[1]), "board_inner_corners": list(BOARD),
        "square_size_mm": square_mm, "num_views": len(corners_list),
        "opencv_rms_px": float(rms), "reprojection_rms_px": measured_rms,
        "per_view_rms_px": per_view,
        "reprojection_error_definition": "sqrt(sum(dx^2+dy^2)/number_of_corners)",
        "rvecs": np.asarray(rvecs).reshape(-1, 3).tolist(),
        "tvecs_mm": np.asarray(tvecs).reshape(-1, 3).tolist(),
        "opencv_calibration_flags": int(flags), "opencv_version": cv2.__version__,
    }


def save_results(session, result, corners_list, files, source):
    result = dict(result, schema_version=1, created_at_utc=datetime.now(timezone.utc).isoformat(),
                  source=source, image_files=files,
                  image_processing="No geometric correction, cropping or resizing by this script",
                  quality_note="Training-view RMS; validate on independent images before use")
    # Sem dtype object: o NPZ não exige pickle para nenhum campo.
    arrays = {key: np.asarray(value) for key, value in result.items()}
    arrays["image_points_px"] = np.asarray(corners_list, dtype=np.float64)
    npz_path, json_path = session / "calibracao.npz", session / "calibracao.json"
    # allow_nan=False impede JSON inválido se algum resultado for não finito.
    json_text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    np.savez_compressed(npz_path, **arrays)
    json_path.write_text(json_text, encoding="utf-8")
    print(f"\nModelo: {result['model']}; {result['num_views']} vistas; "
          f"resolução: {result['image_width']}x{result['image_height']}")
    print(f"RMS de reprojeção: {result['reprojection_rms_px']:.6f} pixels")
    print("K =\n", np.asarray(result["K"]))
    print("D =", result["D"])
    if result["reprojection_rms_px"] > 1.0:
        print("Aviso: RMS acima de 1 pixel. Revise nitidez, medida, modelo e variedade das vistas.")
    print("O RMS nas vistas usadas no ajuste não garante qualidade em novas imagens.")
    print(f"Salvos:\n  {npz_path}\n  {json_path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verificar-dependencias", action="store_true", help="verifica imports sem conectar")
    parser.add_argument("--modelo", choices=("pinhole", "fisheye"), default=MODELO,
                        help="substitui MODELO do bloco de configuração")
    parser.add_argument("--quadrado-mm", type=positive_argument, default=QUADRADO_MM,
                        help="substitui QUADRADO_MM; lado real de um quadrado em mm")
    parser.add_argument("--conexao", choices=("sta", "ap"), default=CONEXAO,
                        help="substitui CONEXAO: Wi-Fi local (sta) ou hotspot (ap)")
    parser.add_argument("--ip", default=IP_ROBO, help="substitui IP_ROBO em sta; ap usa 192.168.12.1")
    parser.add_argument("--imagens", type=Path, help="recalibra uma pasta de imagens originais, sem WebRTC nem GUI")
    parser.add_argument("--prefixo", default="", help="seleciona imagens pelo início do nome, para uma sessão específica")
    parser.add_argument("--pasta-fotos", type=Path, default=PASTA_FOTOS,
                        help="pasta de originais capturados; padrão: fotos/fotos tabuleiro")
    parser.add_argument("--saida", type=Path, default=Path(PASTA_SAIDA), help="pasta base para novas sessões")
    args = parser.parse_args(argv)
    session = None
    try:
        if args.verificar_dependencias:
            dependencies(report=True, live=True)
            return 0
        if args.modelo not in ("pinhole", "fisheye"):
            raise ValueError('Defina MODELO como "pinhole" ou "fisheye" no bloco CONFIGURAÇÃO.')
        if args.quadrado_mm is None:
            raise ValueError("Preencha QUADRADO_MM no bloco CONFIGURAÇÃO com a medida REAL"
                             " de um quadrado em milímetros; não use uma medida presumida.")
        args.quadrado_mm = positive_number(args.quadrado_mm)
        if args.conexao not in ("sta", "ap"):
            raise ValueError('Defina CONEXAO como "sta" ou "ap" no bloco CONFIGURAÇÃO.')
        if args.imagens is None:
            if args.conexao == "sta":
                if not args.ip:
                    raise ValueError("Preencha IP_ROBO no bloco CONFIGURAÇÃO com o IP do Go2"
                                     ' na sua rede; para o hotspot, use CONEXAO = "ap".')
                try:
                    ipaddress.IPv4Address(args.ip)
                except ipaddress.AddressValueError as exc:
                    raise ValueError("IP_ROBO/--ip deve ser um endereço IPv4 válido.") from exc
            elif args.ip and args.ip != "192.168.12.1":
                raise ValueError("Em modo ap o SDK usa 192.168.12.1; remova --ip ou use --conexao sta.")
        dependencies(live=args.imagens is None)
        base = args.saida.expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        prefix = datetime.now().strftime("%Y%m%d_%H%M%S_")
        session = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        # Preserva os dados de entrada mesmo em uma coleta interrompida.
        (session / "sessao.json").write_text(json.dumps({
            "board_inner_corners": list(BOARD), "square_size_mm": args.quadrado_mm,
            "model": args.modelo, "source": "images" if args.imagens else "webrtc",
            "original_images_directory": str(args.pasta_fotos.expanduser().resolve()),
            "image_prefix": args.prefixo if args.imagens else f"calibracao_{session.name}_",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Sessão: {session}")
        collected = collect_files(args.imagens, args.prefixo) if args.imagens else collect_live(args, session)
        if collected is None:
            print("Coleta encerrada sem calibrar. Nenhum parâmetro de calibração foi gerado.")
            return 0
        corners_list, image_size, files = collected
        print("Calculando calibração com as vistas aceitas...")
        result = calibrate(corners_list, image_size, args.quadrado_mm, args.modelo)
        save_results(session, result, corners_list, files, "images" if args.imagens else "webrtc")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nOperação interrompida; nenhuma calibração é presumida.")
        return 130
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        if cv2 is not None and isinstance(exc, cv2.error):
            print("Se a falha ocorreu no ajuste, colete vistas mais variadas, nítidas e com"
                  " o tabuleiro completo; verifique o modelo selecionado.", file=sys.stderr)
        return 1
    finally:
        if session is not None:
            print(f"Arquivos desta sessão: {session}")


if __name__ == "__main__":
    sys.exit(main())
