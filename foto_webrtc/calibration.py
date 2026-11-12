"""Correção pinhole do stream original, com mapas reutilizados entre frames."""

from pathlib import Path


class CameraCorrection:
    def __init__(self, path, alpha=0.0):
        import cv2
        import numpy as np

        self.path = Path(path).expanduser().resolve()
        if not 0 <= alpha <= 1:
            raise ValueError("O enquadramento da correção deve estar entre 0 e 1.")
        try:
            with np.load(self.path, allow_pickle=False) as data:
                if data["model"].item() != "pinhole":
                    raise ValueError("A correção da aplicação exige calibração pinhole.")
                self.K = np.array(data["K"], dtype=np.float64)
                self.D = np.array(data["D"], dtype=np.float64).reshape(-1)
                size = np.array(data["image_size"], dtype=np.float64)
        except (OSError, KeyError, ValueError) as error:
            raise ValueError(f"Não foi possível carregar a calibração {self.path}: {error}") from error
        if (self.K.shape != (3, 3) or self.D.size != 5 or size.shape != (2,)
                or not np.isfinite(self.K).all() or not np.isfinite(self.D).all()
                or not np.isfinite(size).all() or (size <= 0).any()
                or not np.equal(size, np.floor(size)).all()
                or self.K[0, 0] <= 0 or self.K[1, 1] <= 0
                or not np.allclose(self.K[2], [0, 0, 1])):
            raise ValueError(f"Parâmetros de calibração inválidos: {self.path}")
        self.size = tuple(map(int, size))
        self.alpha = alpha
        # Mantém fx/fy: com o centro livre, OpenCV pode escolher escalas
        # horizontais/verticais distintas e alterar a proporção dos objetos.
        self.new_K, self.roi = cv2.getOptimalNewCameraMatrix(
            self.K, self.D, self.size, alpha, self.size, centerPrincipalPoint=True)
        self.maps = cv2.initUndistortRectifyMap(
            self.K, self.D, None, self.new_K, self.size, cv2.CV_32FC1)

    def apply(self, original):
        import cv2

        size = (original.shape[1], original.shape[0])
        if size != self.size:
            raise ValueError(f"stream em {size[0]}x{size[1]}; calibração exige "
                             f"{self.size[0]}x{self.size[1]}")
        return cv2.remap(original, *self.maps, cv2.INTER_LINEAR)
