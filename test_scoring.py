"""Pruebas simples de scoring.py (sin necesidad de Flask ni base de datos).
Ejecutar con: python test_scoring.py
"""
from scoring import calcular_puntos

casos = [
    # (pred_local, pred_visit, real_local, real_visit) -> puntos esperados
    ((2, 1, 2, 1), 3),   # marcador exacto
    ((2, 0, 3, 1), 1),   # mismo ganador (local), marcador distinto
    ((1, 1, 0, 0), 1),   # ambos empate, marcador distinto
    ((1, 0, 0, 1), 0),   # predijo local, ganó visitante
    ((2, 2, 1, 1), 1),   # empate en ambos, distinto marcador
]

if __name__ == "__main__":
    fallos = 0
    for (pl, pv, rl, rv), esperado in casos:
        resultado = calcular_puntos(pl, pv, rl, rv)
        ok = resultado == esperado
        if not ok:
            fallos += 1
        print(f"pred {pl}-{pv} vs real {rl}-{rv} -> {resultado} pts (esperado {esperado}) {'OK' if ok else 'FALLO'}")

    # caso de partido sin terminar
    sin_terminar = calcular_puntos(1, 0, None, None)
    print(f"partido sin terminar -> {sin_terminar} (esperado None) {'OK' if sin_terminar is None else 'FALLO'}")

    print("\nTodo correcto." if fallos == 0 else f"\n{fallos} caso(s) fallaron.")
