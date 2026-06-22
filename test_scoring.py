"""Pruebas para scoring.py."""
from scoring import calcular_puntos


def test_exacto_victoria_local():
    assert calcular_puntos(2, 1, 2, 1) == 3


def test_exacto_victoria_visitante():
    assert calcular_puntos(0, 3, 0, 3) == 3


def test_exacto_empate():
    assert calcular_puntos(1, 1, 1, 1) == 3


def test_exacto_0_0():
    assert calcular_puntos(0, 0, 0, 0) == 3


def test_resultado_correcto_victoria_local_marcador_distinto():
    assert calcular_puntos(2, 0, 3, 1) == 1


def test_resultado_correcto_victoria_visitante_marcador_distinto():
    assert calcular_puntos(0, 2, 1, 3) == 1


def test_resultado_correcto_empate_marcador_distinto():
    assert calcular_puntos(1, 1, 2, 2) == 1


def test_resultado_incorrecto_predijo_local_pero_gano_visitante():
    assert calcular_puntos(2, 1, 1, 2) == 0


def test_resultado_incorrecto_predijo_empate_pero_no_fue():
    assert calcular_puntos(1, 1, 2, 1) == 0


def test_resultado_incorrecto_predijo_victoria_pero_fue_empate():
    assert calcular_puntos(2, 1, 1, 1) == 0


def test_resultado_incorrecto_gano_otro_equipo():
    assert calcular_puntos(3, 0, 0, 3) == 0


def test_marcadores_altos():
    assert calcular_puntos(4, 2, 4, 2) == 3
    assert calcular_puntos(4, 2, 3, 1) == 1
