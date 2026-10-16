"""View: apenas Tkinter e eventos de teclado na thread principal."""

import tkinter as tk

from .models import ConnectionState


class JanelaCamera:
    def __init__(self, title="Camera Go2 - Teleop"):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("960x640")
        self.pressionadas = set()
        self.solturas = {}
        self.imagem = None
        self.fechada = False
        self.on_action = lambda action: None
        self.on_close = lambda: self.root.quit()
        self.status = tk.StringVar(value="Conectando ao Go2...")
        tk.Label(self.root, textvariable=self.status).pack()
        self.reconnect_button = tk.Button(self.root, text="Reconectar (F5)",
                                         command=lambda: self.on_action("Reconectar"), state="disabled")
        self.reconnect_button.pack()
        self.preview = tk.Label(self.root, bg="#202020", fg="white", text="Aguardando imagens da câmera...")
        self.preview.pack(fill="both", expand=True)
        tk.Label(self.root, text="Segure W/S: frente/trás | A/D: lateral | Q/E: giro | Espaço: parar\n"
                 "U: levantar | J: deitar | R: recuperar | H: aceno | K: amortecer | M: modo\n"
                 "P: foto | V: vídeo | F5: reconectar | Esc: sair").pack()
        self.root.bind("<KeyPress>", self._pressionar)
        self.root.bind("<KeyRelease>", self._soltar)
        self.root.bind("<FocusOut>", self._perder_foco)
        self.root.protocol("WM_DELETE_WINDOW", self.fechar)
        self.root.after(0, self.root.focus_force)

    def _pressionar(self, evento):
        if self.fechada:
            return
        tecla = evento.keysym.lower()
        pendente = self.solturas.pop(tecla, None)
        if pendente is not None:
            self.root.after_cancel(pendente)
        if tecla == "escape":
            self.fechar()
        elif tecla == "f5":
            if tecla not in self.pressionadas:
                self.pressionadas.add(tecla)
                self.on_action("Reconectar")
        elif tecla == "space":
            self.pressionadas.clear()
            self.on_action("Parar")
        elif tecla not in self.pressionadas:
            self.pressionadas.add(tecla)
            actions = {"u": "StandUp", "j": "StandDown", "r": "RecoveryStand",
                       "h": "Hello", "k": "Damp", "m": "Consultar", "p": "Foto", "v": "Video"}
            if tecla in actions:
                if tecla in "ujrhkm":
                    self.pressionadas.difference_update("wsadqe")
                self.on_action(actions[tecla])

    def _soltar(self, evento):
        tecla = evento.keysym.lower()
        if tecla not in self.solturas:
            self.solturas[tecla] = self.root.after_idle(self._confirmar_soltura, tecla)

    def _confirmar_soltura(self, tecla):
        self.solturas.pop(tecla, None)
        self.pressionadas.discard(tecla)

    def _perder_foco(self, evento):
        self.pressionadas.clear()
        self.on_action("Parar")

    def mostrar(self, preview):
        self.imagem = tk.PhotoImage(data=preview.ppm, format="PPM")
        self.preview.configure(image=self.imagem, text="")

    def set_connection_state(self, state):
        reconnect = state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR)
        self.reconnect_button.configure(state="normal" if reconnect else "disabled")

    def fechar(self):
        if self.fechada:
            return
        self.fechada = True
        self.pressionadas.clear()
        for callback in self.solturas.values():
            self.root.after_cancel(callback)
        self.solturas.clear()
        self.on_close()

    def destruir(self):
        self.root.destroy()
