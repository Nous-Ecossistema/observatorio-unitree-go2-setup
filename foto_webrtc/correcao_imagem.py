import asyncio
import cv2
import numpy as np
import time
from pathlib import Path
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from aiortc import MediaStreamTrack

# Variável global para armazenar o frame bruto mais recente
ultimo_frame = None

def corrigir_e_melhorar_fisheye(img_bruta):
    """
    Aplica a correção geométrica usando estritamente o módulo cv2.fisheye (Kannala-Brandt),
    seguido de melhorias de nitidez e iluminação.
    """
    h, w = img_bruta.shape[:2]

    # 1. DEFINIÇÃO DOS PARÂMETROS DA CÂMERA (MODELO FISHEYE)
    # Matriz intrínseca K (fx, fy representam a distância focal; cx, cy representam o centro óptico) [cite: 300]
    K = np.array([[360.0,   0.0, w / 2.0],
                  [  0.0, 360.0, h / 2.0],
                  [  0.0,   0.0,     1.0]], dtype=np.float32)

    # Coeficientes polinomiais radiais [k1, k2, k3, k4] do modelo Kannala-Brandt [cite: 300]
    # k1 negativo compensa o arqueamento das bordas da lente olho de peixe [cite: 294]
    D = np.array([-0.05, 0.01, -0.005, 0.0002], dtype=np.float32)

    # Estima a nova matriz de câmera ideal para ajustar o enquadramento após "desentortar" a lente
    nova_matriz = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
        K, D, (w, h), np.eye(3), balance=1.0
    )

    # 2. CORREÇÃO GEOMÉTRICA DIRETA DA LENTE (SEM REMAP)
    # Executa a retificação fisheye diretamente na imagem em tempo real [cite: 300]
    img_corrigida = cv2.fisheye.undistortImage(img_bruta, K, D, Knew=nova_matriz)

    # 3. FILTRO DE NITIDEZ ADAPTATIVO (SHARPENING)
    # Destaca os detalhes finos da floresta e pelos do animal no momento do clique [cite: 292]
    kernel_nitidez = np.array([[ 0,  -0.5,  0],
                               [-0.5,  3.0, -0.5],
                               [ 0,  -0.5,  0]], dtype=np.float32)
    img_nitida = cv2.filter2D(img_corrigida, -1, kernel_nitidez)

    # 4. BALANÇO DE CONTRASTE ADAPTATIVO (CLAHE)
    # Equilibra as luzes e sombras para o ambiente da floresta [cite: 292]
    lab = cv2.cvtColor(img_nitida, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
    cl = clahe.apply(l)
    img_final = cv2.cvtColor(cv2.merge((cl, a, b)), cv2.COLOR_LAB2BGR)

    return img_final

# Callback assíncrono para receber frames WebRTC
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

async def main():
    print("Estabelecendo conexão WebRTC direta no Wi-Fi do robô (Modo AP)...")
    connection = UnitreeWebRTCConnection(
        WebRTCConnectionMethod.LocalAP
    )

    await connection.connect()
    print("Conectado com sucesso!")

    # Ativa a transmissão de vídeo no cão-robô
    connection.video.switchVideoChannel(True)
    connection.video.add_track_callback(receber_frames_camera)

    print("\n -> Clique na janela de vídeo para focar os comandos do teclado.")
    print(" -> Pressione a barra de ESPAÇO para tirar uma foto.")
    print(" -> Pressione ESC para fechar.")

    photo_count = 0
    try:
        while True:
            if ultimo_frame is not None:
                # Corrige a imagem de barril diretamente usando cv2.fisheye [cite: 300]
                frame_corrigido = corrigir_e_melhorar_fisheye(ultimo_frame)

                # Exibe o fluxo de vídeo retificado na tela do seu notebook
                cv2.imshow("Camera Go2 - Video Fisheye Corrigido", frame_corrigido)

                key = cv2.waitKey(1) & 0xFF
                if key == 27: # Tecla ESC
                    break
                elif key == 32: # Barra de ESPAÇO
                    filename = f"foto_campo_fisheye_{photo_count}_{int(time.time())}.jpg"
                    path = Path(__file__).resolve().parent.parent / "fotos" / "corrigidas" / filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    # Salva em qualidade JPEG máxima (100)
                    cv2.imwrite(str(path), frame_corrigido, [int(cv2.IMWRITE_JPEG_QUALITY), 100])
                    filename = str(path)
                    print(f"Foto corrigida salva com sucesso! Arquivo: {filename}")
                    photo_count += 1

            await asyncio.sleep(0.01)

    except KeyboardInterrupt:
        print("\nSessão interrompida pelo terminal.")
    finally:
        print("Fechando conexão e limpando recursos...")
        connection.video.switchVideoChannel(False)
        await connection.disconnect()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    asyncio.run(main())
