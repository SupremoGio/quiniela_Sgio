"""
Reglas de puntuación de la quiniela.

- Marcador exacto correcto:        3 puntos
- Solo el resultado (L/E/V) correcto, marcador distinto: 1 punto
- Cualquier otro caso:              0 puntos

Esta lógica vive separada de los modelos para poder probarla fácilmente
sin necesidad de base de datos (ver test_scoring.py).
"""


def resultado(local, visitante):
    """Devuelve 'L' (gana local), 'V' (gana visitante) o 'E' (empate)."""
    if local > visitante:
        return "L"
    if local < visitante:
        return "V"
    return "E"


def calcular_puntos(pred_local, pred_visitante, real_local, real_visitante):
    """Calcula los puntos de una predicción dado el marcador real.

    Devuelve None si el partido todavía no tiene marcador real (no ha
    terminado), o un entero (0, 1 o 3) si ya se puede calificar.
    """
    if real_local is None or real_visitante is None:
        return None

    if pred_local == real_local and pred_visitante == real_visitante:
        return 3

    if resultado(pred_local, pred_visitante) == resultado(real_local, real_visitante):
        return 1

    return 0
