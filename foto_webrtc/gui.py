"""View: apenas Tkinter e eventos de teclado na thread principal."""

import tkinter as tk

from .commands import MOVEMENT_KEYS
from .models import ConnectionState


class JanelaCamera:
    RELEASE_DELAY_MS = 40

    def __init__(self, title="Camera Go2 - Teleop"):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("1100x760")
        self.root.minsize(720, 540)
        self.root.configure(bg="#111827")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.pressionadas = set()
        self.solturas = {}
        self.imagem = None
        self.fechada = False
        self.on_action = lambda action: None
        self.on_close = lambda: self.root.quit()
        self.status = tk.StringVar(value="Conectando ao Go2...")
        self.connection = tk.StringVar(value="GO2  /  Conectando")
        self.movement = tk.StringVar(value="AGUARDANDO CONEXÃO")
        self.media_status = tk.StringVar(value="Câmera: aguardando imagens")
        header = tk.Frame(self.root, bg="#111827", padx=16, pady=12)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(header, textvariable=self.connection, bg="#111827", fg="#e5e7eb",
                 font=("TkDefaultFont", 12, "bold"), anchor="w").grid(row=0, column=0, sticky="ew")
        self.movement_label = tk.Label(header, textvariable=self.movement, bg="#374151",
                                       fg="#fcd34d", padx=12, pady=6,
                                       font=("TkDefaultFont", 10, "bold"))
        self.movement_label.grid(row=0, column=1, rowspan=2, padx=(12, 0))
        self.detail_label = tk.Label(header, textvariable=self.status, bg="#111827", fg="#9ca3af",
                                     anchor="w", justify="left", wraplength=500)
        self.detail_label.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        header.bind("<Configure>", lambda event: self.detail_label.configure(
            wraplength=max(180, event.width - self.movement_label.winfo_reqwidth() - 50)))

        # Canvas não muda o tamanho solicitado ao receber uma imagem nova.
        # Assim o vídeo não empurra os controles nem realimenta o redimensionamento.
        self.preview = tk.Canvas(self.root, bg="#080d16", highlightthickness=0, width=1, height=1)
        self.preview.grid(row=1, column=0, sticky="nsew", padx=12)
        self.preview_image = self.preview.create_image(0, 0, anchor="center", state="hidden")
        self.preview_message = self.preview.create_text(0, 0, fill="#94a3b8",
            text="Aguardando imagens da câmera…", font=("TkDefaultFont", 13), justify="center")
        self.preview.bind("<Configure>", self._enquadrar)

        footer = tk.Frame(self.root, bg="#111827", padx=12, pady=10)
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.media_label = tk.Label(footer, textvariable=self.media_status, bg="#111827", fg="#9ca3af",
                                   anchor="w", justify="left", wraplength=680)
        self.media_label.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        footer.bind("<Configure>", lambda event: self.media_label.configure(wraplength=max(1, event.width - 24)))
        posture = tk.Frame(footer, bg="#111827")
        posture.grid(row=1, column=0, sticky="ew")
        self.robot_buttons = []
        for column, (text, action) in enumerate((("Levantar · U", "StandUp"),
                ("Recuperar · R", "RecoveryStand"), ("Deitar · J", "StandDown"),
                ("Aceno · H", "Hello"), ("Amortecer · K", "Damp"), ("Modo · M", "Consultar"))):
            posture.columnconfigure(column, weight=1, uniform="actions")
            button = self._botao(posture, text, action)
            button.grid(row=0, column=column, sticky="ew", padx=3)
            button.configure(state="disabled")
            self.robot_buttons.append(button)
        tools = tk.Frame(footer, bg="#111827")
        tools.grid(row=2, column=0, sticky="ew", pady=8)
        for column, (text, action) in enumerate((("Foto · P", "Foto"), ("Vídeo · V", "Video"),
                                                ("Reconectar · F5", "Reconectar"), ("PARAR · Espaço", "Parar"))):
            tools.columnconfigure(column, weight=1, uniform="tools")
            button = self._botao(tools, text, action)
            button.grid(row=0, column=column, sticky="ew", padx=3)
            if action == "Reconectar":
                self.reconnect_button = button
                button.configure(state="disabled")
            elif action == "Parar":
                button.configure(bg="#b91c1c", fg="white", activebackground="#dc2626")
        tk.Label(footer, text="Segure ↑/↓ ou W/S: frente/trás   •   ←/→ ou A/D: lateral   •   Q/E: giro\n"
                 "Aguarde PRONTO PARA ANDAR antes de usar as setas.   Esc: sair",
                 bg="#111827", fg="#cbd5e1", justify="center").grid(row=3, column=0, sticky="ew")
        self.root.bind("<KeyPress>", self._pressionar)
        self.root.bind("<KeyRelease>", self._soltar)
        self.root.bind("<FocusOut>", self._perder_foco)
        self.root.protocol("WM_DELETE_WINDOW", self.fechar)
        self.root.after(0, self.root.focus_force)

    def _botao(self, parent, text, action):
        return tk.Button(parent, text=text, command=lambda: self._acionar(action), takefocus=False,
                         bg="#273449", fg="#f1f5f9", activebackground="#3b4d68", activeforeground="white",
                         relief="flat", padx=6, pady=8, cursor="hand2")

    def _acionar(self, action):
        if action == "Parar":
            self.pressionadas.clear()
        elif action in ("StandUp", "StandDown", "RecoveryStand", "Hello", "Damp", "Consultar"):
            self.pressionadas.difference_update(MOVEMENT_KEYS)
        self.on_action(action)

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
            self._acionar("Parar")
        elif tecla not in self.pressionadas:
            self.pressionadas.add(tecla)
            actions = {"u": "StandUp", "j": "StandDown", "r": "RecoveryStand",
                       "h": "Hello", "k": "Damp", "m": "Consultar", "p": "Foto", "v": "Video"}
            if tecla in actions:
                self._acionar(actions[tecla])

    def _soltar(self, evento):
        tecla = evento.keysym.lower()
        if tecla not in self.solturas:
            # No X11 o autorepeat pode emitir Release/Press em eventos separados.
            # after_idle pode confirmar a soltura entre os dois e repetir posturas.
            self.solturas[tecla] = self.root.after(self.RELEASE_DELAY_MS, self._confirmar_soltura, tecla)

    def _confirmar_soltura(self, tecla):
        self.solturas.pop(tecla, None)
        self.pressionadas.discard(tecla)

    def _perder_foco(self, evento):
        self.pressionadas.clear()
        self.on_action("Parar")

    def mostrar(self, preview):
        self.imagem = tk.PhotoImage(data=preview.ppm, format="PPM")
        self.preview.itemconfigure(self.preview_image, image=self.imagem, state="normal")
        self.preview.itemconfigure(self.preview_message, state="hidden")

    def _enquadrar(self, evento):
        for item in (self.preview_image, self.preview_message):
            self.preview.coords(item, evento.width / 2, evento.height / 2)

    def atualizar_estado(self, robot, camera, video):
        self.connection.set(f"GO2  /  {robot.connection_state.value}  ·  Modo {robot.mode or '—'}")
        self.status.set(robot.message)
        self.media_status.set(f"Câmera: {camera}   |   {video.message}")
        if not robot.connected:
            message, color = "SEM CONEXÃO", "#fca5a5"
        elif robot.busy:
            message, color = "AÇÃO EM ANDAMENTO", "#fcd34d"
        elif robot.allowed:
            message, color = "PRONTO PARA ANDAR", "#6ee7b7"
        else:
            message, color = "LEVANTAR (U) PARA ANDAR", "#fcd34d"
        self.movement.set(message)
        self.movement_label.configure(fg=color)
        for button in self.robot_buttons:
            button.configure(state="normal" if robot.connected and not robot.busy else "disabled")

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
