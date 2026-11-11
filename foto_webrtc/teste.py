"""
Controle do Unitree Go2 EDU por teclado, com stream de câmera em janela OpenCV.

Movimento:
    W / S       -> andar para frente / trás
    A / D       -> deslocar (strafe) esquerda / direita
    Q / E       -> girar (yaw) esquerda / direita
    ESPAÇO      -> parar (stop imediato)

Comandos de postura (SPORT_MOD, via api_id):
    U -> StandUp (levantar)
    J -> StandDown (deitar)
    R -> RecoveryStand (recuperar postura/controle total após deitar ou cair)
    H -> Hello (aceno)
    K -> Damp  (amortecimento / "desliga" as juntas -> robô fica mole, cuidado)

Outros:
    P   -> tirar foto do frame atual
    ESC -> encerrar

Observação:
    Depois de qualquer comando de postura (U, J, R), o script reconfere e
    reforça o modo de movimento "normal", porque o robô às vezes volta para
    um modo que não aceita comandos de joystick simulado (WIRELESS_CONTROLLER)
    depois de deitar/levantar.
"""

import asyncio
import json
import time
from pathlib import Path

import cv2
from aiortc import MediaStreamTrack

from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

# ----------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------
GANHO_LINEAR = 0.9   # intensidade de lx/ly (frente/trás, strafe) -> -1.0 a 1.0
GANHO_ANGULAR = 0.9  # intensidade de rx (giro/yaw)
WATCHDOG_S = 0.15    # se nenhuma tecla de movimento chegar dentro desse tempo, para o robô
PUBLICAR_HZ = 20.0   # taxa de publicação do wireless controller enquanto uma tecla está segurada

ultimo_frame = None


async def receber_frames_camera(track: MediaStreamTrack):
    global ultimo_frame
    try:
        while True:
            frame = await track.recv()
            ultimo_frame = frame.to_ndarray(format="bgr24")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"\n[Aviso] Fluxo de vídeo encerrado: {e}")


def publicar_joystick(pub_sub, lx=0.0, ly=0.0, rx=0.0, ry=0.0):
    """Publica um estado do 'controle sem fio' simulado (rt/wirelesscontroller).
    É isso que move o robô continuamente enquanto uma tecla está pressionada."""
    pub_sub.publish_without_callback(
        RTC_TOPIC["WIRELESS_CONTROLLER"],
        {"lx": lx, "ly": ly, "rx": rx, "ry": ry, "keys": 0},
    )


async def enviar_comando_sport(conn, nome_comando: str, parametro: dict | None = None):
    """Envia um comando único do modo esporte (StandUp, StandDown, Hello, etc)
    e imprime o código de retorno, para facilitar diagnóstico caso o robô
    pare de obedecer depois de algum comando."""
    payload = {"api_id": SPORT_CMD[nome_comando]}
    if parametro is not None:
        payload["parameter"] = parametro
    try:
        resp = await conn.datachannel.pub_sub.publish_request_new(RTC_TOPIC["SPORT_MOD"], payload)
        codigo = resp["data"]["header"]["status"]["code"]
        if codigo == 0:
            print(f"[Sport] Comando '{nome_comando}' enviado com sucesso (código 0).")
        else:
            print(f"[Sport] Comando '{nome_comando}' retornou código {codigo} (possível rejeição).")
    except Exception as e:
        print(f"[Sport] Falha ao enviar '{nome_comando}': {e}")


async def garantir_modo_normal(conn):
    """Confere o modo de movimento atual e troca para 'normal' se necessário
    (necessário para os comandos SPORT_MOD e o joystick simulado funcionarem
    corretamente). Chamado no início e também depois de qualquer comando de
    postura, porque o modo pode mudar sozinho após deitar/levantar."""
    resp = await conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["MOTION_SWITCHER"], {"api_id": 1001}
    )
    if resp["data"]["header"]["status"]["code"] == 0:
        modo_atual = json.loads(resp["data"]["data"])["name"]
        print(f"[Modo] Modo de movimento atual: {modo_atual}")
        if modo_atual != "normal":
            print("[Modo] Trocando para o modo 'normal'...")
            await conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["MOTION_SWITCHER"],
                {"api_id": 1002, "parameter": {"name": "normal"}},
            )
            await asyncio.sleep(5)  # tempo para o robô ficar em pé/estabilizar
    else:
        print(f"[Modo] Falha ao consultar modo atual (código {resp['data']['header']['status']['code']}).")


async def main():
    print("Estabelecendo conexão WebRTC direta no Wi-Fi do robô (Modo AP)...")
    connection = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP)
    await connection.connect()
    print("Conectado com sucesso!")

    await garantir_modo_normal(connection)

    connection.video.switchVideoChannel(True)
    connection.video.add_track_callback(receber_frames_camera)

    pub_sub = connection.datachannel.pub_sub

    print("\n -> Clique na janela de vídeo para focar os comandos do teclado.")
    print(" -> W/A/S/D: mover   Q/E: girar   ESPAÇO: parar")
    print(" -> U: levantar  J: deitar  R: recuperar postura  H: aceno  K: amortecer")
    print(" -> P: foto   ESC: sair\n")

    # Mapa de teclas de movimento -> (lx, ly, rx)
    MAPA_MOVIMENTO = {
        ord("w"): (0.0, GANHO_LINEAR, 0.0),
        ord("s"): (0.0, -GANHO_LINEAR, 0.0),
        ord("a"): (-GANHO_LINEAR, 0.0, 0.0),
        ord("d"): (GANHO_LINEAR, 0.0, 0.0),
        ord("q"): (0.0, 0.0, GANHO_ANGULAR),
        ord("e"): (0.0, 0.0, -GANHO_ANGULAR),
    }

    # Comandos de postura que exigem reconferir o modo de movimento depois
    COMANDOS_POSTURA_RECONFERE_MODO = {
        ord("u"): "StandUp",
        ord("j"): "StandDown",
        ord("r"): "RecoveryStand",
    }

    photo_count = 0
    ultimo_comando_movimento_ts = 0.0
    ultima_publicacao_ts = 0.0
    lx_atual, ly_atual, rx_atual = 0.0, 0.0, 0.0
    intervalo_publicacao = 1.0 / PUBLICAR_HZ

    try:
        while True:
            if ultimo_frame is not None:
                cv2.imshow("Camera Go2 - Teleop", ultimo_frame)

            key = cv2.waitKey(1) & 0xFF
            agora = time.time()

            if key == 27:  # ESC
                break

            elif key == 32:  # ESPAÇO -> parada imediata
                lx_atual, ly_atual, rx_atual = 0.0, 0.0, 0.0
                publicar_joystick(pub_sub)
                ultimo_comando_movimento_ts = 0.0
                print("\r  [STOP]                                   ", end="", flush=True)

            elif key in MAPA_MOVIMENTO:
                lx_atual, ly_atual, rx_atual = MAPA_MOVIMENTO[key]
                ultimo_comando_movimento_ts = agora

            elif key in COMANDOS_POSTURA_RECONFERE_MODO:
                nome_comando = COMANDOS_POSTURA_RECONFERE_MODO[key]
                await enviar_comando_sport(connection, nome_comando)
                # Zera qualquer velocidade pendente antes de reconferir o modo,
                # para não misturar comando de postura com comando de movimento
                lx_atual, ly_atual, rx_atual = 0.0, 0.0, 0.0
                ultimo_comando_movimento_ts = 0.0
                await garantir_modo_normal(connection)

            elif key == ord("h"):
                await enviar_comando_sport(connection, "Hello")
            elif key == ord("k"):
                await enviar_comando_sport(connection, "Damp")

            elif key == ord("p") and ultimo_frame is not None:
                filename = f"foto_campo_{photo_count}_{int(time.time())}.jpg"
                path = Path(__file__).resolve().parent.parent / "fotos" / "originais" / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(path), ultimo_frame)
                filename = str(path)
                print(f"\nFoto salva com sucesso! Arquivo: {filename}")
                photo_count += 1

            # Watchdog: se a última tecla de movimento foi há mais de WATCHDOG_S, zera velocidade
            if ultimo_comando_movimento_ts and (agora - ultimo_comando_movimento_ts) > WATCHDOG_S:
                lx_atual, ly_atual, rx_atual = 0.0, 0.0, 0.0
                ultimo_comando_movimento_ts = 0.0

            # Publica a velocidade atual (mesmo que zero, para manter o robô parado) a PUBLICAR_HZ
            if (agora - ultima_publicacao_ts) >= intervalo_publicacao:
                publicar_joystick(pub_sub, lx=lx_atual, ly=ly_atual, rx=rx_atual)
                ultima_publicacao_ts = agora

            await asyncio.sleep(0.005)

    except KeyboardInterrupt:
        print("\nSessão interrompida pelo terminal.")
    finally:
        print("Fechando conexão e limpando recursos...")
        publicar_joystick(pub_sub)  # garante que o robô pare antes de desconectar
        connection.video.switchVideoChannel(False)
        await connection.disconnect()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    asyncio.run(main())
