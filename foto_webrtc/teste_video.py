"""Unitree Go2 EDU: teclado, fotos e gravação de vídeo via WebRTC LocalAP.

Execute no MESMO ambiente Python em que seu código original funciona:
    python foto_webrtc/teste_video.py
    python foto_webrtc/teste_video.py --testar-janela  # sem conectar ao robô

Clique na janela da câmera para dar foco ao teclado.
W/S: frente/trás; A/D: deslocamento lateral; Q/E: giro; ESPAÇO: parar.
U: levantar; J: deitar; R: recuperar; H: aceno; K: amortecer juntas.
M: consultar modo de movimento (diagnóstico).
P: foto; V: iniciar/parar vídeo; ESC: parar e encerrar.

Saída: pasta 'capturas_go2', ao lado deste script, com subpastas fotos/videos.
MP4 somente com vídeo, sem áudio. A resolução vem do stream recebido;
não há aumento de resolução nem tratamento artificial da imagem.
Cada gravação começa em tempo zero. Os intervalos dos frames recebidos
são preservados com precisão de 1/30 s; entrada acima de 30 fps é amostrada.
O MP4 é finalizado ao apertar V novamente ou ao sair normalmente com ESC.

Requisitos: OpenCV, aiortc, unitree_webrtc_connect e Tkinter (python3-tk).
PyAV ('av') é uma dependência do aiortc. A janela não usa Qt nem Pillow.
O encoder libx264 deve estar disponível na instalação do PyAV.
Não instale/reinstale bibliotecas se seu ambiente atual já funciona.

Teste de campo: com o robô parado, grave 10 segundos, pare com V e abra
o arquivo. Repita encerrando com ESC durante a gravação. Depois teste
uma foto durante um vídeo. Guarde prints da janela, terminal e arquivos.

Limites: usa a qualidade do stream WebRTC e recomprime em H.264. A taxa
nominal de 30 fps não implica que a câmera forneça 30 imagens novas/s.
Internet não é necessária para a captura local; a conexão Wi-Fi ao Go2
deve permanecer ativa. Encerrar à força pode deixar um MP4 incompleto.
Soltar as teclas ou tirar o foco da janela zera a locomoção. A parada
depende do programa e da conexão; não é uma parada de emergência física.

Revisão do controle de postura:
- Pedidos WebRTC têm espera limitada e não bloqueiam a janela/teclado.
- Depois de J/K, os comandos de locomoção ficam suspensos até U/R aceito.
- Não se troca o modo automaticamente logo depois de deitar.
- U e R continuam sendo comandos diferentes; R só é enviado por você.
- Código 0 significa resposta favorável da API, não confirmação visual
  de que o robô concluiu o movimento. Observe a postura antes de andar.
- O terminal registra pedido, API, resposta, timeout e consulta de modo.
- A fonte revisada foi unitree_webrtc_connect 2.2.0. A versão instalada
  no seu notebook é mostrada no início; não é necessário atualizá-la.
"""

import asyncio
import json
import argparse
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from fractions import Fraction
from importlib.metadata import PackageNotFoundError, version
from itertools import count
from pathlib import Path

import av
import cv2
from aiortc import MediaStreamTrack
from aiortc.contrib.media import MediaRecorder, MediaRelay
from aiortc.mediastreams import MediaStreamError

GANHO_LINEAR = 0.3  # m/s na API Move
GANHO_ANGULAR = 0.5  # rad/s na API Move
PUBLICAR_HZ = 20.0
TIMEOUT_PEDIDO_S = 5.0
# Margens de teste, não medições da conclusão física de cada ação.
TEMPO_POSTURA_S = {"StandUp": 3.0, "StandDown": 3.0,
                   "RecoveryStand": 5.0, "Hello": 2.0, "Damp": 0.5}
LIMITE_FRAME_ANTIGO_S = 2.0
JANELA = "Camera Go2 - Teleop"
PASTA_SAIDA = Path(__file__).resolve().parent / "capturas_go2"


class JanelaCamera:
    """GUI na thread principal; dispensa o backend Qt do OpenCV."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(JANELA)
        self.root.geometry("960x640")
        self.fechada = False
        self.pressionadas = set()
        self.acoes = deque()
        self.solturas = {}
        self.imagem = None
        self.frame_ts = None
        self.status = tk.StringVar(value="Conectando ao Go2...")
        tk.Label(self.root, textvariable=self.status).pack()
        self.preview = tk.Label(self.root, bg="#202020", fg="white",
                                text="Aguardando imagens da câmera...")
        self.preview.pack(fill="both", expand=True)
        tk.Label(self.root, text="Segure W/S: frente/trás | A/D: lateral | Q/E: giro | Espaço: parar\n"
                 "U: levantar | J: deitar | R: recuperar | M: modo | P: foto | V: vídeo | Esc: sair").pack()
        self.root.bind("<KeyPress>", self._pressionar)
        self.root.bind("<KeyRelease>", self._soltar)
        self.root.bind("<FocusOut>", self._perder_foco)
        self.root.protocol("WM_DELETE_WINDOW", self.fechar)
        self.root.update()
        self.root.focus_force()

    def _pressionar(self, evento):
        tecla = evento.keysym.lower()
        # X11 envia release/press durante auto-repetição. Cancela o release
        # pendente para manter a tecla ativa sem repetir ações como foto/V.
        pendente = self.solturas.pop(tecla, None)
        if pendente is not None:
            self.root.after_cancel(pendente)
        if tecla == "escape":
            self.fechar()
        elif tecla == "space":
            self.pressionadas.clear()
            self.acoes.append(32)
        elif tecla not in self.pressionadas:
            self.pressionadas.add(tecla)
            if tecla in "ujrhkmvp":
                self.acoes.append(ord(tecla))

    def _soltar(self, evento):
        tecla = evento.keysym.lower()
        if tecla not in self.solturas:
            self.solturas[tecla] = self.root.after_idle(self._confirmar_soltura, tecla)

    def _confirmar_soltura(self, tecla):
        self.solturas.pop(tecla, None)
        self.pressionadas.discard(tecla)

    def _perder_foco(self, evento):
        self.pressionadas.clear()
        self.acoes.append(32)

    def fechar(self):
        self.pressionadas.clear()
        self.fechada = True
        self.root.quit()

    def atualizar(self, camera=None):
        if self.fechada:
            return
        self.root.update()
        if self.fechada or camera is None:
            return
        if camera.ultimo_frame is None or camera.ultimo_frame_ts == self.frame_ts:
            return
        imagem = camera.imagem_janela()
        altura, largura = imagem.shape[:2]
        escala = min(max(self.preview.winfo_width(), 1) / largura,
                     max(self.preview.winfo_height(), 1) / altura, 1.0)
        if escala < 1:
            imagem = cv2.resize(imagem, (max(1, int(largura * escala)),
                                        max(1, int(altura * escala))))
        rgb = cv2.cvtColor(imagem, cv2.COLOR_BGR2RGB)
        altura, largura = rgb.shape[:2]
        ppm = f"P6\n{largura} {altura}\n255\n".encode() + rgb.tobytes()
        self.imagem = tk.PhotoImage(data=ppm, format="PPM")
        self.preview.configure(image=self.imagem, text="")
        self.frame_ts = camera.ultimo_frame_ts

    def destruir(self):
        self.root.destroy()


def nome_unico(prefixo, extensao):
    return f"{prefixo}_{datetime.now():%Y%m%d_%H%M%S_%f}.{extensao}"


class TrilhaParaGravar(MediaStreamTrack):
    """Assinatura própria: não altera os frames compartilhados pelo relay."""

    kind = "video"

    def __init__(self, origem):
        super().__init__()
        self.origem = origem
        self.inicio_timestamp = None
        self.ultimo_slot = -1
        self.inicio_relogio = time.monotonic()
        self.tamanho = None
        self.frames = 0

    async def recv(self):
        while True:
            frame = await self.origem.recv()
            if frame.pts is not None and frame.time_base is not None:
                timestamp = frame.pts * frame.time_base
            else:
                timestamp = Fraction(time.monotonic() - self.inicio_relogio)
            if self.inicio_timestamp is None:
                self.inicio_timestamp = timestamp
            slot = round((timestamp - self.inicio_timestamp) * 30)
            if slot <= self.ultimo_slot:
                continue
            tamanho = (frame.width, frame.height)
            if self.tamanho is None:
                self.tamanho = tamanho
            elif tamanho != self.tamanho:
                raise MediaStreamError("A resolução mudou durante a gravação.")
            # Uma cópia evita mudar pts/time_base do frame mostrado na janela.
            copia = av.VideoFrame.from_ndarray(
                frame.to_ndarray(format="bgr24"), format="bgr24"
            )
            copia.pts = slot
            copia.time_base = Fraction(1, 30)
            self.ultimo_slot = slot
            self.frames += 1
            return copia

    def stop(self):
        super().stop()
        self.origem.stop()  # encerra somente esta assinatura do relay


class CameraCampo:
    def __init__(self, pasta):
        self.pasta = Path(pasta)
        self.relay = MediaRelay()
        self.origem = None
        self.ultimo_frame = None
        self.ultimo_frame_ts = 0.0
        self.recorder = None
        self.trilha_gravacao = None
        self.arquivo_video = None
        self.inicio_video = 0.0

    async def receber(self, track):
        if track.kind != "video":
            return
        self.origem = track
        preview = self.relay.subscribe(track, buffered=False)
        try:
            while True:
                frame = await preview.recv()
                self.ultimo_frame = frame.to_ndarray(format="bgr24")
                self.ultimo_frame_ts = time.monotonic()
        except (asyncio.CancelledError, MediaStreamError):
            pass
        except Exception as erro:
            print(f"\n[Câmera] Fluxo encerrado: {erro}")
        finally:
            preview.stop()
            self.origem = None
            self.ultimo_frame = None

    def frame_recente(self):
        return (
            self.ultimo_frame is not None
            and time.monotonic() - self.ultimo_frame_ts < LIMITE_FRAME_ANTIGO_S
        )

    async def iniciar_video(self):
        if self.recorder is not None:
            return
        if self.origem is None or not self.frame_recente():
            print("\n[Vídeo] Aguarde a câmera transmitir imagens antes de gravar.")
            return
        altura, largura = self.ultimo_frame.shape[:2]
        if largura % 2 or altura % 2:
            print("\n[Vídeo] H.264 nesta versão requer largura e altura pares.")
            return
        trilha = None
        recorder = None
        try:
            av.codec.Codec("libx264", "w")
            pasta = self.pasta / "videos"
            pasta.mkdir(parents=True, exist_ok=True)
            arquivo = pasta / nome_unico("video_campo", "mp4")
            trilha = TrilhaParaGravar(
                self.relay.subscribe(self.origem, buffered=False)
            )
            recorder = MediaRecorder(str(arquivo), format="mp4")
            recorder.addTrack(trilha)
            await recorder.start()
            self.recorder = recorder
            self.trilha_gravacao = trilha
            self.arquivo_video = arquivo
            self.inicio_video = time.monotonic()
            print(f"\n[REC] Gravando: {arquivo}\n[V] para finalizar; [P] para foto.")
        except Exception as erro:
            if trilha is not None:
                trilha.stop()
            if recorder is not None:
                try:
                    await recorder.stop()
                except Exception:
                    pass
            print(f"\n[Vídeo] Não foi possível iniciar: {erro}")

    async def parar_video(self):
        if self.recorder is None:
            return
        recorder = self.recorder
        trilha = self.trilha_gravacao
        arquivo = self.arquivo_video
        self.recorder = None
        self.trilha_gravacao = None
        try:
            await recorder.stop()  # flush do encoder e fechamento do MP4
            if trilha.frames == 0 or not arquivo.exists() or arquivo.stat().st_size == 0:
                print("\n[Vídeo] Nenhum frame foi gravado. Repita a captura.")
            else:
                print(f"\n[Vídeo] Arquivo finalizado: {arquivo}")
        except Exception as erro:
            print(f"\n[Vídeo] Falha na finalização; confira o arquivo: {erro}")
        finally:
            trilha.stop()

    async def salvar_foto(self):
        if not self.frame_recente():
            print("\n[Foto] Câmera sem frame recente. Aguarde a transmissão.")
            return
        pasta = self.pasta / "fotos"
        arquivo = pasta / nome_unico("foto_campo", "jpg")
        frame = self.ultimo_frame.copy()
        try:
            pasta.mkdir(parents=True, exist_ok=True)
            ok = await asyncio.to_thread(cv2.imwrite, str(arquivo), frame)
            print(f"\n[Foto] {'Salva' if ok else 'Falha ao salvar'}: {arquivo}")
        except Exception as erro:
            print(f"\n[Foto] Falha ao salvar: {erro}")

    def imagem_janela(self):
        if self.ultimo_frame is None:
            return None
        imagem = self.ultimo_frame.copy()
        if self.recorder is not None:
            segundos = int(time.monotonic() - self.inicio_video)
            texto = f"REC {segundos // 60:02d}:{segundos % 60:02d} | V: parar"
            cv2.putText(imagem, texto, (15, 35), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (0, 0, 255), 2)
        return imagem  # o indicador é mostrado, não é embutido no arquivo


def publicar_movimento(pub_sub, topics, comandos, lx=0.0, ly=0.0, rx=0.0):
    canal = getattr(pub_sub, "channel", None)
    if canal is not None and canal.readyState != "open":
        raise ConnectionError("O canal WebRTC não está aberto.")
    # Mesmo envelope de publish_request_new, sem criar uma Future a cada
    # 50 ms: respostas de movimento não precisam ficar na fila de pedidos.
    pub_sub.publish_without_callback(topics["SPORT_MOD"], {
        "header": {"identity": {"id": next(IDS_MOVIMENTO), "api_id": comandos["Move"]}},
        "parameter": json.dumps({"x": ly, "y": -lx, "z": rx}),
    }, msg_type="req")


IDS_MOVIMENTO = count(int(time.time() * 1000) % 2147483648)


class PedidosRTC:
    """Espera limitada sem cancelar a Future que o driver ainda pode resolver.

    Na fonte 2.2.0, o resolver chama future.set_result sem testar done().
    Cancelar um pedido com wait_for poderia quebrar uma resposta atrasada.
    asyncio.wait limita a espera sem cancelar o pedido original. No máximo
    oito pedidos ficam pendentes; cancelamento só após desconectar.
    """

    def __init__(self, pub_sub, timeout=TIMEOUT_PEDIDO_S):
        self.pub_sub = pub_sub
        self.timeout = timeout
        self.pendentes = set()
        self.ids = IDS_MOVIMENTO  # IDs únicos também em relação aos comandos Move

    async def solicitar(self, topic, api_id, descricao, parametro=None):
        if len(self.pendentes) >= 8:
            print("\n[RTC] Muitos pedidos sem resposta. Encerre e reconecte.")
            return None
        identidade = next(self.ids)
        payload = {"api_id": api_id, "id": identidade}
        if parametro is not None:
            payload["parameter"] = parametro
        print(f"\n[ENVIO] {descricao} | API={api_id} | pedido={identidade}")
        task = asyncio.create_task(self.pub_sub.publish_request_new(topic, payload))
        self.pendentes.add(task)

        def concluido(futuro):
            self.pendentes.discard(futuro)
            if futuro.cancelled():
                return
            erro = futuro.exception()
            if erro is not None:
                print(f"\n[RTC] Pedido {identidade} falhou: {erro}")

        task.add_done_callback(concluido)
        feitos, _ = await asyncio.wait({task}, timeout=self.timeout)
        if not feitos:
            print(f"\n[TIMEOUT] {descricao}: sem resposta em {self.timeout:g} s. "
                  "A ação pode ter sido executada; o teclado continua ativo.")
            return None
        try:
            resposta = task.result()
            codigo = resposta["data"]["header"]["status"]["code"]
            print(f"[RESPOSTA] {descricao} | pedido={identidade} | código={codigo}")
            return resposta
        except Exception as erro:
            print(f"[RTC] Resposta inválida/falha em {descricao}: {erro}")
            return None

    async def fechar(self):
        tarefas = list(self.pendentes)
        for task in tarefas:
            task.cancel()
        if tarefas:
            await asyncio.gather(*tarefas, return_exceptions=True)


class ControlePostura:
    def __init__(self, pedidos, topics, comandos):
        self.pedidos = pedidos
        self.topics = topics
        self.comandos = comandos
        self.modo = None
        self.ocupado = False
        self.locomocao_liberada = False

    async def consultar_modo(self):
        resposta = await self.pedidos.solicitar(
            self.topics["MOTION_SWITCHER"], 1001, "Consultar modo"
        )
        if resposta is None or resposta["data"]["header"]["status"]["code"] != 0:
            self.modo = None
            return None
        try:
            dados = resposta["data"]["data"]
            dados = json.loads(dados) if isinstance(dados, str) else dados
            self.modo = dados["name"]
            print(f"[Modo] Atual: {self.modo}")
            return self.modo
        except (KeyError, TypeError, ValueError) as erro:
            self.modo = None
            print(f"[Modo] Dados inesperados: {erro}")
            return None

    async def garantir_modo_normal(self):
        modo = await self.consultar_modo()
        if modo == "normal":
            return True
        if modo is None:
            print("[Modo] Não confirmado. Use U/R para tentar novamente ou M para diagnosticar.")
            return False
        resposta = await self.pedidos.solicitar(
            self.topics["MOTION_SWITCHER"], 1002,
            "Trocar modo para normal", {"name": "normal"}
        )
        if resposta is None or resposta["data"]["header"]["status"]["code"] != 0:
            self.modo = None
            return False
        await asyncio.sleep(3.0)
        return await self.consultar_modo() == "normal"

    async def executar(self, nome):
        self.ocupado = True
        liberada_antes = self.locomocao_liberada
        self.locomocao_liberada = False
        try:
            if nome == "Inicializar":
                await self.consultar_modo()
                print("[Controle] Pressione U para levantar antes de locomover.")
                return
            if nome == "Consultar":
                await self.consultar_modo()
                # Consulta não levanta o robô nem comprova sua postura.
                self.locomocao_liberada = liberada_antes and self.modo == "normal"
                return
            if self.modo != "normal" and not await self.garantir_modo_normal():
                return
            resposta = await self.pedidos.solicitar(
                self.topics["SPORT_MOD"], self.comandos[nome], nome
            )
            if resposta is None:
                print("[Postura] Resultado incerto; locomoção suspensa. Use U/R quando apropriado.")
                return
            codigo = resposta["data"]["header"]["status"]["code"]
            if codigo != 0:
                print(f"[Postura] {nome} rejeitado: código {codigo}; não foi reenviado.")
                return
            await asyncio.sleep(TEMPO_POSTURA_S[nome])
            if nome in ("StandUp", "RecoveryStand"):
                self.locomocao_liberada = True
                print("[Postura] Pedido aceito. Confira visualmente a estabilidade antes de andar.")
            elif nome == "Hello":
                self.locomocao_liberada = liberada_antes
            else:
                print("[Postura] Locomoção suspensa após deitar/amortecer. U: levantar; R: recuperar.")
            # Sem consulta/troca de modo imediatamente depois de StandDown.
        except Exception as erro:
            print(f"[Postura] Falha em {nome}: {erro}; locomoção suspensa.")
        finally:
            self.ocupado = False


async def main(janela):
    # Imports aqui permitem testar o gravador sem driver e sem robô.
    from unitree_webrtc_connect.webrtc_driver import (
        UnitreeWebRTCConnection, WebRTCConnectionMethod,
    )
    from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

    camera = CameraCampo(PASTA_SAIDA)
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP)
    pub_sub = None
    pedidos = None
    controle = None
    tarefa_postura = None
    movimento = {
        ord("w"): (0.0, GANHO_LINEAR, 0.0),
        ord("s"): (0.0, -GANHO_LINEAR, 0.0),
        ord("a"): (-GANHO_LINEAR, 0.0, 0.0),
        ord("d"): (GANHO_LINEAR, 0.0, 0.0),
        ord("q"): (0.0, 0.0, GANHO_ANGULAR),
        ord("e"): (0.0, 0.0, -GANHO_ANGULAR),
    }
    postura = {ord("u"): "StandUp", ord("j"): "StandDown", ord("r"): "RecoveryStand"}
    outros = {ord("h"): "Hello", ord("k"): "Damp"}
    lx, ly, rx = 0.0, 0.0, 0.0
    ultima_publicacao = 0.0
    tarefa_conexao = None

    try:
        print("Conectando ao Go2 pela rede Wi-Fi local (AP)...")
        tarefa_conexao = asyncio.create_task(conn.connect())
        while not tarefa_conexao.done():
            janela.atualizar()
            if janela.fechada:
                return
            await asyncio.sleep(0.01)
        await tarefa_conexao
        pub_sub = conn.datachannel.pub_sub
        pedidos = PedidosRTC(pub_sub)
        controle = ControlePostura(pedidos, RTC_TOPIC, SPORT_CMD)
        try:
            print(f"[Versão] unitree_webrtc_connect {version('unitree_webrtc_connect')}")
        except PackageNotFoundError:
            print("[Versão] Instalação local do driver sem versão registrada.")
        print("[APIs]", {nome: SPORT_CMD.get(nome) for nome in postura.values()})
        conn.video.add_track_callback(camera.receber)
        conn.video.switchVideoChannel(True)
        janela.status.set("Conectado. Aguardando câmera. U: levantar para locomover.")
        print("Clique na janela. W/A/S/D: mover | Q/E: girar | ESPAÇO: parar")
        print("U/J/R: postura | H: aceno | K: amortecer | M: consultar modo")
        print("P: foto | V: vídeo | ESC: sair. Dê toques breves nas teclas de ação.")
        print(f"Arquivos: {PASTA_SAIDA}")
        tarefa_postura = asyncio.create_task(controle.executar("Inicializar"))

        inicio_camera = time.monotonic()
        aviso_camera = False
        while not janela.fechada:
            janela.atualizar(camera)
            key = janela.acoes.popleft() if janela.acoes else 255
            agora = time.monotonic()
            if janela.fechada:
                break
            lx, ly, rx = 0.0, 0.0, 0.0
            if controle.locomocao_liberada and not controle.ocupado:
                for tecla in janela.pressionadas:
                    if len(tecla) == 1 and ord(tecla) in movimento:
                        dx, dy, dr = movimento[ord(tecla)]
                        lx, ly, rx = lx + dx, ly + dy, rx + dr
            if key == 32:
                lx, ly, rx = 0.0, 0.0, 0.0
                publicar_movimento(pub_sub, RTC_TOPIC, SPORT_CMD)
            elif key in postura or key in outros or key == ord("m"):
                # Zera antes de iniciar a ação, sem bloquear o teclado.
                lx, ly, rx = 0.0, 0.0, 0.0
                janela.pressionadas.clear()
                publicar_movimento(pub_sub, RTC_TOPIC, SPORT_CMD)
                if tarefa_postura is not None and not tarefa_postura.done():
                    print("\n[Postura] Ação pendente. Aguarde o resultado ou timeout antes de outra.")
                else:
                    nome = postura.get(key, outros.get(key, "Consultar"))
                    tarefa_postura = asyncio.create_task(controle.executar(nome))
            elif key == ord("p"):
                await camera.salvar_foto()
            elif key == ord("v"):
                if camera.recorder is None:
                    await camera.iniciar_video()
                else:
                    await camera.parar_video()

            agora = time.monotonic()
            if not camera.frame_recente() and agora - inicio_camera > 10 and not aviso_camera:
                print("[Câmera] Sem imagens recentes. Confira se outro aplicativo está usando a câmera do Go2.")
                aviso_camera = True
            estado = "Postura em andamento" if controle.ocupado else (
                "Locomoção liberada" if controle.locomocao_liberada else "U: levantar para locomover")
            janela.status.set(f"{estado} | Câmera: {'OK' if camera.frame_recente() else 'aguardando imagens'}")
            if camera.recorder is not None and not camera.frame_recente():
                print("\n[Vídeo] Sem frames recentes; finalizando a gravação.")
                await camera.parar_video()
            if agora - ultima_publicacao >= 1.0 / PUBLICAR_HZ:
                # Não concorre com postura/deitado publicando Move a 20 Hz.
                # ESPAÇO e ESC ainda enviam zero explicitamente.
                if controle.locomocao_liberada and not controle.ocupado:
                    publicar_movimento(pub_sub, RTC_TOPIC, SPORT_CMD, lx, ly, rx)
                ultima_publicacao = agora
            await asyncio.sleep(0.005)
    finally:
        print("\nEncerrando e finalizando arquivos...")
        if tarefa_conexao is not None and not tarefa_conexao.done():
            tarefa_conexao.cancel()
            await asyncio.gather(tarefa_conexao, return_exceptions=True)
        if tarefa_postura is not None:
            tarefa_postura.cancel()
            await asyncio.gather(tarefa_postura, return_exceptions=True)
        if pub_sub is not None:
            try:
                publicar_movimento(pub_sub, RTC_TOPIC, SPORT_CMD)
            except Exception as erro:
                print(f"[Conexão] Não foi possível enviar parada: {erro}")
        await camera.parar_video()
        if conn.pc is not None:
            try:
                conn.video.switchVideoChannel(False)
            except Exception as erro:
                print(f"[Conexão] Falha ao desativar vídeo: {erro}")
            try:
                await conn.disconnect()
            except Exception as erro:
                print(f"[Conexão] Falha ao desconectar: {erro}")
        if pedidos is not None:
            await pedidos.fechar()


def cli():
    """Entrada de terminal; só abre a janela quando chamada explicitamente."""
    parser = argparse.ArgumentParser(description="Câmera e teleoperação do Go2 via Wi-Fi AP")
    parser.add_argument("--testar-janela", action="store_true", help="abre apenas a janela, sem conectar nem enviar comandos")
    args = parser.parse_args()
    janela = None
    try:
        janela = JanelaCamera()
        if args.testar_janela:
            janela.status.set("Teste da janela — sem conexão com o robô. Esc: sair.")
            janela.root.mainloop()
        else:
            asyncio.run(main(janela))
    except KeyboardInterrupt:
        print("Sessão interrompida pelo terminal.")
    except tk.TclError as erro:
        print(f"[Janela] Não foi possível abrir a interface: {erro}")
        raise SystemExit(1)
    finally:
        if janela is not None:
            janela.destruir()


if __name__ == "__main__":
    cli()
