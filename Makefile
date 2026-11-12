UV ?= uv
PYTHON ?= python
ARGS ?=

.PHONY: help install run window diagnostico legacy test check

help:
	@printf '%s\n' \
		'Comandos disponíveis:' \
		'  make install             Sincroniza o ambiente com uv.lock' \
		'  make run                 Inicia câmera e teleoperação do Go2' \
		'  make run ARGS="..."      Passa opções para go2-env' \
		'  make window             Abre a interface sem conectar ao robô' \
		'  make diagnostico        Consulta o modo do Go2 sem enviar comandos' \
		'  make legacy             Executa foto_webrtc/teste_video.py' \
		'  make test               Executa os testes' \
		'  make check              Verifica dependências e executa os testes'

install:
	$(UV) sync --locked

run:
	$(UV) run --locked go2-env $(ARGS)

window:
	$(UV) run --locked go2-env --testar-janela

diagnostico:
	$(UV) run --locked $(PYTHON) -m foto_webrtc.diagnostico_api

legacy:
	$(UV) run --locked $(PYTHON) foto_webrtc/teste_video.py $(ARGS)

test:
	$(UV) run --locked pytest

check:
	$(UV) run --locked uv pip check
	$(UV) run --locked pytest
