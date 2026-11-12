import asyncio
import cv2
import time
from pathlib import Path
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from aiortc import MediaStreamTrack

# Variável global para armazenar o frame mais recente recebido da câmera
ultimo_frame = None

# Função assíncrona (callback) que o driver chamará para receber o fluxo de vídeo
async def receber_frames_camera(track: MediaStreamTrack):
    global ultimo_frame
    try:
        while True:
            # Aguarda e recebe o frame via WebRTC
            frame = await track.recv()
            # Converte o frame recebido em matriz BGR compatível com OpenCV
            ultimo_frame = frame.to_ndarray(format="bgr24")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"\n[Aviso] Fluxo de vídeo encerrado: {e}")

async def main():
    print("Estabelecendo conexão WebRTC direta no Wi-Fi do robô (Modo AP)...")

    # Inicializa a conexão no Modo AP direto
    connection = UnitreeWebRTCConnection(
        WebRTCConnectionMethod.LocalAP
    )

    # Efetua a conexão WebRTC
    await connection.connect()
    print("Conectado com sucesso!")

    # Ativa o canal de vídeo no robô
    connection.video.switchVideoChannel(True)

    # Registra o callback para capturar os frames de vídeo em tempo real
    connection.video.add_track_callback(receber_frames_camera)

    print("\n -> Clique na janela de vídeo para focar os comandos do teclado.")
    print(" -> Pressione a barra de ESPAÇO para tirar uma foto.")
    print(" -> Pressione ESC para fechar.")

    photo_count = 0
    try:
        while True:
            if ultimo_frame is not None:
                # Exibe a imagem na tela do seu notebook
                cv2.imshow("Camera Go2 - Floresta Stream", ultimo_frame)

                # Aguarda tecla física do notebook por 1ms
                key = cv2.waitKey(1) & 0xFF
                if key == 27: # Tecla ESC
                    break
                elif key == 32: # Barra de ESPAÇO
                    filename = f"foto_campo_{photo_count}_{int(time.time())}.jpg"
                    path = Path(__file__).resolve().parent.parent / "fotos" / "originais" / filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(path), ultimo_frame)
                    filename = str(path)
                    print(f"Foto salva com sucesso! Arquivo: {filename}")
                    photo_count += 1

            # Pequena pausa para liberar o processador para as tarefas de rede do asyncio
            await asyncio.sleep(0.01)

    except KeyboardInterrupt:
        print("\nSessão interrompida pelo terminal.")
    finally:
        print("Fechando conexão e limpando recursos...")
        # Desativa o canal de vídeo no robô
        connection.video.switchVideoChannel(False)
        # Desconecta do WebRTC de forma ordenada
        await connection.disconnect()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    asyncio.run(main())
