"""Entrada compatível da câmera/teleoperação Go2; imports não iniciam I/O.

Tkinter permanece na thread principal. Application coordena workers próprios
para comandos, WebRTC, captura e processamento/gravação de vídeo.
"""

import argparse
import signal
from pathlib import Path

from loguru import logger

# Mantém os ajustes e o caminho de saída usados pela aplicação original.
GANHO_LINEAR = 0.3
GANHO_ANGULAR = 0.5
PUBLICAR_HZ = 20.0
TIMEOUT_PEDIDO_S = 5.0
TEMPO_POSTURA_S = {"StandUp": 3.0, "StandDown": 3.0, "RecoveryStand": 5.0, "Hello": 2.0, "Damp": 0.5}
LIMITE_FRAME_ANTIGO_S = 2.0
JANELA = "Camera Go2 - Teleop"
PASTA_SAIDA = Path(__file__).resolve().parent / "capturas_go2"


def JanelaCamera():
    # Tk não é exigido para --help, import ou testes de workers sem display.
    if __package__:
        from .gui import JanelaCamera as Window
    else:
        from foto_webrtc.gui import JanelaCamera as Window
    return Window(title=JANELA)


def cli():
    """A configuração ocorre apenas na execução explícita da aplicação."""
    parser = argparse.ArgumentParser(description="Câmera e teleoperação do Go2 via Wi-Fi AP")
    parser.add_argument("--testar-janela", action="store_true", help="abre apenas a janela, sem conectar nem enviar comandos")
    parser.add_argument("--pasta-saida", type=Path, default=PASTA_SAIDA, help="diretório de fotos e vídeos")
    parser.add_argument("--pasta-logs", type=Path, default=Path("logs"), help="diretório dos logs JSONL")
    parser.add_argument("--nivel-log", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO", help="nível do console (arquivo mantém DEBUG)")
    parser.add_argument("--timeout-pedido", type=float, default=TIMEOUT_PEDIDO_S, help="timeout de resposta da API em segundos")
    parser.add_argument("--publicar-hz", type=float, default=PUBLICAR_HZ, help="frequência de comandos Move")
    args = parser.parse_args()
    # Também suporta o caminho histórico sem depender de instalação editável.
    if not __package__:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from foto_webrtc.config import Config
    from foto_webrtc.observability import LoggingSession
    from foto_webrtc.application import Application

    try:
        config = Config(output_dir=args.pasta_saida, log_dir=args.pasta_logs, log_level=args.nivel_log,
                        linear_gain=GANHO_LINEAR, angular_gain=GANHO_ANGULAR,
                        publish_hz=args.publicar_hz, request_timeout=args.timeout_pedido,
                        frame_timeout=LIMITE_FRAME_ANTIGO_S, posture_delays=tuple(TEMPO_POSTURA_S.items()))
    except ValueError as error:
        parser.error(str(error))
    with LoggingSession(config):
        window = None
        app = None
        handlers = {}
        try:
            window = JanelaCamera()
            if args.testar_janela:
                window.status.set("Teste da janela — sem conexão com o robô. Esc: sair.")
                window.root.mainloop()
            else:
                app = Application(config, window)
                # Sinais só marcam um Event: Loguru não é reentrante em signal handlers.
                for signum in (signal.SIGINT, signal.SIGTERM):
                    handlers[signum] = signal.getsignal(signum)
                    signal.signal(signum, lambda *_: app.stop_event.set())
                app.run()
        except KeyboardInterrupt:
            logger.bind(event="interrupted").info("Sessão interrompida pelo terminal")
        except Exception:
            logger.bind(event="application_failed").exception("Falha na aplicação; confira dependências, sessão gráfica e conexão")
            raise SystemExit(1)
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            if app is not None:
                app.request_shutdown()
                app.closer.join()
            if window is not None:
                window.destruir()


if __name__ == "__main__":
    cli()
