"""Reglas de puntuacion de la quiniela.

Funcion pura, sin acceso a base de datos, para que sea facil de testear.

Reglas:
- Marcador exacto correcto -> 3 puntos
- Solo el resultado (ganador o empate) correcto, marcador distinto -> 1 punto
- Cualquier otro caso -> 0 puntos
"""
from __future__ import annotations


def _resultado(local: int, visitante: int) -> str:
    """Devuelve 'L' si gana el local, 'V' si gana el visitante, 'E' si empatan."""
    if local > visitante:
        return "L"
    if local < visitante:
        return "V"
    return "E"


def calcular_puntos(pred_local: int, pred_visitante: int, real_local: int, real_visitante: int) -> int:
    """Calcula los puntos de una prediccion dado el marcador real.

    Args:
        pred_local: goles predichos del equipo local.
        pred_visitante: goles predichos del equipo visitante.
        real_local: goles reales del equipo local.
        real_visitante: goles reales del equipo visitante.

    Returns:
        3 si el marcador exacto coincide, 1 si solo coincide el resultado
        (ganador/empate), 0 en cualquier otro caso.
    """
    if pred_local == real_local and pred_visitante == real_visitante:
        return 3

    if _resultado(pred_local, pred_visitante) == _resultado(real_local, real_visitante):
        return 1

    return 0
