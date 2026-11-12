#!/usr/bin/env python3
"""Testa a correção pinhole em uma foto salva, sem conectar ao robô.

Execute na pasta do projeto:
  .venv/bin/python testar_correcao_camera_go2.py
  .venv/bin/python testar_correcao_camera_go2.py --imagem CAMINHO_DA_FOTO
  .venv/bin/python testar_correcao_camera_go2.py --mostrar

--alpha 0 (padrão) reduz bordas pretas e perde parte do campo de visão;
--alpha 1 tenta preservar mais campo. Na calibração atual, alpha 1 também
produziu deformações fortes nas extremidades; não use isso como validação.
A saída não é recortada depois da correção.
O enquadramento mantém a proporção focal fx/fy e centraliza o ponto principal.
A foto deve vir do mesmo stream original, em 1280x720, sem correção prévia.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parent
CALIBRACAO = ROOT / "calibracoes_go2/20261021_180527_uaywafbe/calibracao.npz"
TABULEIRO = ROOT / "fotos" / "fotos tabuleiro"
IMAGEM = TABULEIRO / "calibracao_20261021_093812_illc3whs_vista_070.png"


def load_calibration(path):
    with np.load(path, allow_pickle=False) as data:
        if data["model"].item() != "pinhole":
            raise ValueError("Este teste aceita calibração pinhole; o modelo do arquivo é outro.")
        K = np.array(data["K"], dtype=np.float64)
        D = np.array(data["D"], dtype=np.float64).reshape(-1)
        size = tuple(map(int, data["image_size"]))
        training_files = {str(Path(p).resolve()) for p in data["image_files"].tolist()}
    if (K.shape != (3, 3) or D.size != 5 or len(size) != 2 or min(size) <= 0
            or not np.isfinite(K).all() or not np.isfinite(D).all()
            or K[0, 0] <= 0 or K[1, 1] <= 0):
        raise ValueError("Parâmetros de calibração inválidos.")
    return K, D, size, training_files


def correct_image(frame, K, D, size, alpha):
    actual = (frame.shape[1], frame.shape[0])
    if actual != size:
        raise ValueError(f"Foto em {actual}; a calibração exige {size}. Use o frame original.")
    new_K, roi = cv2.getOptimalNewCameraMatrix(
        K, D, size, alpha, size, centerPrincipalPoint=True)
    map_x, map_y = cv2.initUndistortRectifyMap(K, D, None, new_K, size, cv2.CV_32FC1)
    corrected = cv2.remap(frame, map_x, map_y, cv2.INTER_LINEAR)
    return corrected, new_K, roi


def comparison_image(original, corrected):
    # Legendas fora das fotos; ambas permanecem na mesma escala e resolução.
    comparison = cv2.hconcat([original, corrected])
    comparison = cv2.copyMakeBorder(comparison, 48, 0, 0, 0,
                                    cv2.BORDER_CONSTANT, value=(35, 35, 35))
    width = original.shape[1]
    for label, x in (("ORIGINAL", 16), ("CORRIGIDA - PINHOLE", width + 16)):
        cv2.putText(comparison, label, (x, 32), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (255, 255, 255), 2, cv2.LINE_AA)
    return comparison


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--imagem", type=Path, default=IMAGEM, help="foto original a corrigir")
    parser.add_argument("--calibracao", type=Path, default=CALIBRACAO, help="arquivo NPZ pinhole")
    parser.add_argument("--saida", type=Path, default=ROOT / "testes_correcao_go2",
                        help="pasta dos relatórios; cada execução cria uma subpasta")
    parser.add_argument("--pasta-fotos", type=Path,
                        help="pasta de resultados; padrão: a pasta fotos tabuleiro para suas imagens, ou fotos/ para as demais")
    parser.add_argument("--alpha", type=float, default=0.0, help="enquadramento de 0 a 1; padrão: 0")
    parser.add_argument("--mostrar", action="store_true", help="abre comparação numa janela local")
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.alpha <= 1:
            raise ValueError("--alpha deve estar entre 0 e 1.")
        path = args.imagem.expanduser().resolve()
        calibration = args.calibracao.expanduser().resolve()
        K, D, size, training_files = load_calibration(calibration)
        frame = cv2.imread(str(path))
        if frame is None:
            raise ValueError(f"Não foi possível ler a foto: {path}")
        corrected, new_K, roi = correct_image(frame, K, D, size, args.alpha)
        comparison = comparison_image(frame, corrected)
        base = args.saida.expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix=f"{path.stem}_", dir=base))
        photos = (args.pasta_fotos.expanduser().resolve() if args.pasta_fotos is not None
                  else TABULEIRO if path.is_relative_to(TABULEIRO) else ROOT / "fotos")
        corrected_path = photos / "corrigidas" / f"{output.name}_corrigida.png"
        comparison_path = photos / "corrigidas" / "comparacoes" / f"{output.name}_comparacao.png"
        for destination, pixels in ((corrected_path, corrected), (comparison_path, comparison)):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(destination), pixels):
                raise OSError(f"Não foi possível salvar {destination}")
        reused = str(path) in training_files
        report = {
            "original": str(path), "calibration": str(calibration), "model": "pinhole",
            "corrected": str(corrected_path), "comparison": str(comparison_path),
            "image_size": list(size), "alpha": args.alpha, "new_K": new_K.tolist(),
            "preserves_focal_ratio": True, "center_principal_point": True,
            "valid_roi_xywh": list(map(int, roi)), "cropped": False,
            "reuses_calibration_image": reused,
            "note": "Comparação visual; não constitui aprovação automática da calibração.",
        }
        (output / "relatorio.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        print(f"Imagem corrigida: {corrected_path}")
        print(f"Comparação (original à esquerda): {comparison_path}")
        if reused:
            print("Esta foto participou da calibração. Confira também uma foto nova do mesmo stream.")
        print("Compare linhas que deveriam ser retas, principalmente perto das bordas.")
        if args.mostrar:
            gui = cv2.getBuildInformation()
            no_display = sys.platform.startswith("linux") and not (
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            if no_display or any("GUI:" in line and "NONE" in line for line in gui.splitlines()):
                print("Sem interface gráfica disponível; abra o arquivo comparacao.png salvo.")
            else:
                window = "Go2 - Original | Corrigida - pressione uma tecla para sair"
                try:
                    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
                    cv2.resizeWindow(window, 1280, 400)
                    cv2.imshow(window, comparison)
                    while cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) >= 1:
                        if cv2.waitKey(50) >= 0:
                            break
                finally:
                    cv2.destroyAllWindows()
        return 0
    except (OSError, ValueError, KeyError, cv2.error) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
