# Go2 — câmera e teleoperação via WebRTC

Projeto Python para visualizar a câmera do **Unitree Go2**, tirar fotos JPG,
gravar vídeos MP4/H.264 e enviar comandos de postura e locomoção pelo Wi-Fi
local do robô. O programa principal é `foto_webrtc/teste_video.py` e utiliza
Tkinter para a janela, OpenCV para imagens e aiortc/PyAV para vídeo.

## Pré-requisitos

- Python **3.12** (versão indicada em `.python-version`).
- Linux com sessão gráfica e Tkinter para executar a interface.
- Go2 acessível pelo Wi-Fi AP do robô, no endereço `192.168.12.1`.
- Bibliotecas de sistema para PortAudio, dependência do driver WebRTC.
- PyAV com o encoder `libx264` para gravar MP4.

Em Ubuntu/Debian, instale os pré-requisitos de sistema:

```bash
sudo apt update
sudo apt install python3-venv python3-tk python3-dev portaudio19-dev build-essential libgl1 libglib2.0-0
```

Use os pacotes de sistema correspondentes ao seu Python, caso a distribuição
forneça outra versão por padrão. Tkinter não é instalado pelo `pip`.

## Instalação

Na raiz do repositório:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip check
```

`pyproject.toml` é a fonte única das dependências diretas, com versões do
ambiente atual. `requirements.txt` instala o projeto editável com o extra
`observability`, que inclui Loguru. As dependências transitivas são resolvidas
pelo pip; este arquivo não é um lock completo do ambiente.

Para instalar apenas o programa atual, sem o extra de observabilidade:

```bash
python -m pip install -e .
```

## Configuração

Conecte o notebook ao Wi-Fi AP do Go2 antes de iniciar. O programa usa
`WebRTCConnectionMethod.LocalAP`; não exige credenciais de conta no código.
Internet é necessária para instalar dependências, mas não para a captura local.
Firmware que exigir uma chave AES precisa da configuração descrita na
[documentação do driver](https://github.com/legion1581/unitree_webrtc_connect#readme).
O script atual não fornece uma opção de terminal para essa chave.

Os ajustes atuais ficam nas constantes de `foto_webrtc/teste_video.py`:

| Constante | Padrão | Uso |
| --- | --- | --- |
| `GANHO_LINEAR` | `0.3` | Velocidade linear na API Move, em m/s |
| `GANHO_ANGULAR` | `0.5` | Velocidade de giro, em rad/s |
| `PUBLICAR_HZ` | `20.0` | Frequência de envio de movimento |
| `TIMEOUT_PEDIDO_S` | `5.0` | Espera por resposta de postura/modo |
| `LIMITE_FRAME_ANTIGO_S` | `2.0` | Validade de uma imagem para captura |
| `PASTA_SAIDA` | `capturas_go2` ao lado do script | Fotos e vídeos locais |

Não coloque senhas, tokens ou chaves no código. `.env`, arquivos de chaves,
configurações privadas, logs e capturas são excluídos pelo `.gitignore`.
O programa atual não carrega arquivos `.env` automaticamente.

## Execução

O caminho original continua disponível:

```bash
python foto_webrtc/teste_video.py
```

Após instalar o projeto, também é possível usar:

```bash
go2-env
# ou
python -m go2_env
```

Teste apenas a janela, sem conectar nem enviar comandos ao robô:

```bash
python foto_webrtc/teste_video.py --testar-janela
```

Clique na janela para dar foco ao teclado. Pressione **U** e aguarde a ação
antes de locomover. Uma resposta favorável da API não comprova a postura
física; observe o robô antes de andar.

| Tecla | Ação |
| --- | --- |
| W / S | Frente / trás enquanto segura |
| A / D | Deslocamento lateral |
| Q / E | Giro |
| Espaço | Parar |
| U / J / R | Levantar / deitar / recuperar |
| H / K | Aceno / amortecer juntas |
| M | Consultar modo de movimento |
| P | Salvar foto JPG |
| V | Iniciar ou finalizar MP4 |
| Esc | Parar, finalizar gravação e encerrar |

As capturas ficam em `foto_webrtc/capturas_go2/fotos/` e
`foto_webrtc/capturas_go2/videos/` na instalação editável. Soltar as teclas ou
tirar o foco da janela zera a locomoção. Essa parada depende do programa e da
conexão; não substitui uma parada física de emergência.

## Estrutura

```text
.
├── foto_webrtc/
│   ├── __init__.py
│   ├── teste_video.py       # programa principal: câmera, postura, foto e vídeo
│   ├── foto_webrtc.py       # exemplo de captura de fotos
│   ├── correcao_imagem.py   # exemplo de correção fisheye
│   ├── teste.py             # teleoperação anterior
│   └── testemov.py          # demonstração que movimenta o robô automaticamente
├── src/go2_env/             # entradas go2-env e python -m go2_env
├── tests/                  # verificações sem robô nem janela
├── .gitignore
├── .python-version
├── pyproject.toml
├── requirements.txt
└── README.md
```

Os caminhos históricos foram mantidos para preservar a execução existente.
Os dois pacotes são declarados explicitamente no build; ambientes virtuais,
capturas e pastas privadas não são incluídos no pacote.

## Validação e diagnóstico

```bash
python -m unittest discover -s tests -v
python -m compileall -q src foto_webrtc tests
go2-env --help
python -m pip check
```

Essas verificações não conectam ao Go2 e não validam a locomoção física.
`testemov.py` é uma demonstração com ações automáticas; não é um teste unitário.

- **Janela não abre:** execute em uma sessão gráfica local, confira Tkinter e
  teste `--testar-janela`. Uma sessão sem acesso a `DISPLAY` não abre a interface.
- **Sem vídeo:** confira o Wi-Fi, a conexão WebRTC e se outro aplicativo está
  usando a câmera. O terminal avisa quando faltam imagens recentes.
- **Sem movimento:** consulte o modo com M e acompanhe as respostas de U/R.
  Após deitar/amortecer, a locomoção fica suspensa até levantar/recuperar.
- **MP4 incompleto:** finalize com V ou Esc; encerrar o processo à força pode
  impedir o fechamento do arquivo.

O Loguru está disponível no extra `observability`. A refatoração com threads e
logs correlacionados ainda não está integrada ao programa atual; os protótipos
locais ficam fora do versionamento. O programa atual imprime diagnósticos de
postura/modo, mas não correlaciona as respostas de cada comando Move.

## Versionamento

Antes de publicar, revise `git status`, `git diff --cached` e `git ls-files`.
O `.gitignore` preserva os arquivos locais no disco e impede sua inclusão
acidental no repositório. Não use `git add -f` para adicionar dados privados.
Este preparo cria apenas o repositório e o commit locais; não configura um
remote nem executa push.
