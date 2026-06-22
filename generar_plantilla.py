"""Genera plantilla_predicciones.xlsx con columnas de ejemplo.

Uso: python generar_plantilla.py
"""
from __future__ import annotations

import pandas as pd

COLUMNAS = ["jornada", "jugador", "equipo_local", "equipo_visitante", "pred_local", "pred_visitante"]

FILAS_EJEMPLO = [
    ["jornada_1", "Gio", "Mexico", "Poland", 2, 1],
    ["jornada_1", "Karla", "Mexico", "Poland", 1, 1],
]


def main() -> None:
    df = pd.DataFrame(FILAS_EJEMPLO, columns=COLUMNAS)
    df.to_excel("plantilla_predicciones.xlsx", index=False)
    print("plantilla_predicciones.xlsx generado.")


if __name__ == "__main__":
    main()
