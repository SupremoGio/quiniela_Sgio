"""
Quiniela Mundial 2026 — App Flask

Funcionalidades:
- Tabla de posiciones (standings) de los participantes.
- Vista por jornada: partidos, marcador real y predicción de cada jugador.
- Subir un Excel/CSV con las predicciones de todos para una jornada.
- Sincronizar resultados reales contra la API gratuita football-data.org
  y recalcular automáticamente los puntos de cada predicción.
- Administración simple de jugadores.

Para correrlo localmente:
    pip install -r requirements.txt
    flask --app app init-db
    flask --app app run --debug
"""
import hmac
import io
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import click
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)

import football_api
from models import ConfigApp, Jugador, Partido, Prediccion, PrediccionCampeon, SnapshotPosicion, db
from scoring import calcular_puntos

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


class Admin(UserMixin):
    """Único usuario administrador, definido por variables de entorno
    (no hay tabla de usuarios: este proyecto solo necesita una cuenta)."""

    id = "admin"

    def __init__(self, username):
        self.username = username


def _admin_credenciales_validas(username, password):
    admin_user = os.environ.get("ADMIN_USERNAME", "admin")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_pass:
        return False
    return hmac.compare_digest(username, admin_user) and hmac.compare_digest(password, admin_pass)


def _buscar_jugador_insensible(nombre_jugador):
    """SQLite's LOWER() no convierte tildes/acentos, así que comparamos en
    Python para que la búsqueda sea insensible a mayúsculas y acentos."""
    nombre_norm = nombre_jugador.strip().lower()
    for j in Jugador.query.all():
        if j.nombre.strip().lower() == nombre_norm:
            return j
    return None


def _get_equipos_eliminados():
    """Devuelve un set de nombres de equipos ya eliminados.

    Combina tres fuentes:
    1. Fase de grupos: si ya hay partidos de eliminatoria (jornada >= 4)
       cargados, cualquier equipo que apareció en jornadas 1-3 pero NO
       aparece en ningún partido de eliminatoria no clasificó → eliminado.
    2. Eliminatorias: el equipo que perdió a los 90 min en jornada >= 4
       queda eliminado. Los empates (prórroga/penales) no se detectan aquí.
    3. Config manual: clave 'equipos_eliminados_extra', nombres separados
       por coma — para cubrir empates a 90 min donde no se puede saber
       el perdedor automáticamente.
    """
    eliminados = set()

    partidos_eliminatoria = Partido.query.filter(Partido.jornada >= 4).all()
    partidos_grupos = Partido.query.filter(Partido.jornada <= 3).all()

    # Fuente 1: equipos de grupos que no clasificaron a eliminatorias
    if partidos_eliminatoria:
        equipos_en_eliminatoria = set()
        for p in partidos_eliminatoria:
            equipos_en_eliminatoria.add(p.equipo_local)
            equipos_en_eliminatoria.add(p.equipo_visitante)

        for p in partidos_grupos:
            for equipo in (p.equipo_local, p.equipo_visitante):
                clasifico = any(
                    football_api.equipos_coinciden(equipo, e)
                    for e in equipos_en_eliminatoria
                )
                if not clasifico:
                    eliminados.add(equipo)

    # Fuente 2: perdedores en eliminatorias
    # Pre-calcular equipos por jornada para detectar quién NO avanzó
    from collections import defaultdict
    equipos_por_jornada = defaultdict(set)
    for p in Partido.query.filter(Partido.jornada >= 4).all():
        equipos_por_jornada[p.jornada].add(p.equipo_local)
        equipos_por_jornada[p.jornada].add(p.equipo_visitante)

    for p in partidos_eliminatoria:
        if not p.finalizado or p.marcador_local is None or p.marcador_visitante is None:
            continue
        if p.marcador_local > p.marcador_visitante:
            eliminados.add(p.equipo_visitante)
        elif p.marcador_visitante > p.marcador_local:
            eliminados.add(p.equipo_local)
        else:
            # Empate a 90 min (ET/penales): usar ganador_api si disponible,
            # si no, detectar por ausencia en la siguiente jornada
            if p.ganador_api == "L":
                eliminados.add(p.equipo_visitante)
            elif p.ganador_api == "V":
                eliminados.add(p.equipo_local)
            else:
                next_equipos = equipos_por_jornada.get(p.jornada + 1, set())
                if next_equipos:
                    for equipo in (p.equipo_local, p.equipo_visitante):
                        avanzo = any(
                            football_api.equipos_coinciden(equipo, e)
                            for e in next_equipos
                        )
                        if not avanzo:
                            eliminados.add(equipo)

    # Fuente 3: overrides manuales (empates a 90 min / correcciones)
    cfg = ConfigApp.query.filter_by(clave="equipos_eliminados_extra").first()
    if cfg and cfg.valor:
        for nombre in cfg.valor.split(","):
            nombre = nombre.strip()
            if nombre:
                eliminados.add(nombre)

    return eliminados


def _equipo_esta_eliminado(equipo, eliminados):
    """True si `equipo` coincide (tolerante) con alguno del set `eliminados`."""
    return any(football_api.equipos_coinciden(equipo, e) for e in eliminados)


def _guardar_prediccion_campeon(nombre_jugador, equipo):
    jugador = _buscar_jugador_insensible(nombre_jugador)
    if not jugador:
        return
    equipo = str(equipo).strip()
    if not equipo or equipo.lower() == "nan":
        return
    existing = PrediccionCampeon.query.filter_by(jugador_id=jugador.id).first()
    if existing:
        existing.equipo = equipo
    else:
        db.session.add(PrediccionCampeon(jugador_id=jugador.id, equipo=equipo))


def _buscar_partido_insensible(jornada, eq_local, eq_visitante):
    for p in Partido.query.filter_by(jornada=jornada).all():
        if (football_api.equipos_coinciden(p.equipo_local, eq_local)
                and football_api.equipos_coinciden(p.equipo_visitante, eq_visitante)):
            return p
    return None


def _guardar_prediccion(nombre_jugador, jornada, eq_local, eq_visitante, pred_local, pred_visitante):
    """Crea/actualiza Jugador, Partido y Prediccion para una fila ya parseada."""
    jugador = _buscar_jugador_insensible(nombre_jugador)
    if jugador is None:
        jugador = Jugador(nombre=nombre_jugador)
        db.session.add(jugador)
        db.session.flush()

    partido = _buscar_partido_insensible(jornada, eq_local, eq_visitante)
    if partido is None:
        partido = Partido(jornada=jornada, equipo_local=eq_local, equipo_visitante=eq_visitante)
        db.session.add(partido)
        db.session.flush()

    prediccion = Prediccion.query.filter_by(
        jugador_id=jugador.id, partido_id=partido.id
    ).first()
    if prediccion is None:
        prediccion = Prediccion(jugador_id=jugador.id, partido_id=partido.id)
        db.session.add(prediccion)

    prediccion.pred_local = pred_local
    prediccion.pred_visitante = pred_visitante
    if partido.finalizado:
        prediccion.puntos = calcular_puntos(
            pred_local, pred_visitante, partido.marcador_local, partido.marcador_visitante
        )


def _importar_formato_largo(df):
    """Formato: una fila por jugador+partido, columnas jornada/jugador/
    equipo_local/equipo_visitante/pred_local/pred_visitante."""
    filas_ok, filas_error = 0, 0
    for _, fila in df.iterrows():
        try:
            nombre_jugador = str(fila["jugador"]).strip()
            jornada = int(fila["jornada"])
            eq_local = str(fila["equipo_local"]).strip()
            eq_visitante = str(fila["equipo_visitante"]).strip()
            pred_local = int(fila["pred_local"])
            pred_visitante = int(fila["pred_visitante"])
        except (ValueError, KeyError):
            filas_error += 1
            continue

        _guardar_prediccion(nombre_jugador, jornada, eq_local, eq_visitante, pred_local, pred_visitante)
        filas_ok += 1

    return filas_ok, filas_error


def _leer_pdf_como_df(archivo_bytes):
    """Extrae la primera tabla de un PDF y devuelve un DataFrame limpio.

    Busca la fila que contiene 'INTEGRANTE' o 'JUGADOR' como encabezado,
    descarta las filas anteriores, y devuelve el DataFrame con esa fila
    como cabecera (listo para pasarse a _importar_formato_ancho).
    """
    import pdfplumber

    with pdfplumber.open(archivo_bytes) as pdf:
        filas = []
        for page in pdf.pages:
            tabla = page.extract_table()
            if tabla:
                filas.extend(tabla)

    if not filas:
        raise ValueError("No se encontró ninguna tabla en el PDF.")

    # Encontrar la fila de encabezado (la que contiene INTEGRANTE/JUGADOR)
    header_idx = None
    for i, fila in enumerate(filas):
        vals = [str(v or "").strip().upper() for v in fila]
        if any(v in ("INTEGRANTE", "JUGADOR", "NOMBRE") for v in vals):
            header_idx = i
            break

    if header_idx is None:
        # Si no hay encabezado reconocible, asumir que la primera fila no vacía es el header
        for i, fila in enumerate(filas):
            if any(v for v in fila if v):
                header_idx = i
                break

    if header_idx is None:
        raise ValueError("No se pudo identificar la fila de encabezado en el PDF.")

    headers = [str(v or "").strip() for v in filas[header_idx]]

    # Deduplicar columnas vacías o repetidas para que fila[col] devuelva escalar
    seen = {}
    headers_uniq = []
    for h in headers:
        key = h if h else "_vacío"
        if key in seen:
            seen[key] += 1
            headers_uniq.append(f"{key}_{seen[key]}")
        else:
            seen[key] = 0
            headers_uniq.append(key)

    data = []
    for fila in filas[header_idx + 1:]:
        if any(v for v in fila if v):  # ignorar filas completamente vacías
            data.append([str(v or "").strip() for v in fila])

    df = pd.DataFrame(data, columns=headers_uniq)

    # Eliminar columnas completamente vacías que vengan del PDF
    df = df.loc[:, ~df.columns.str.startswith("_vacío")]

    return df


def _parsear_marcador(valor):
    """'o'/'O' (o vacío) significa 0 goles. Lanza ValueError si no es válido."""
    if valor is None:
        raise ValueError("vacío")
    texto = str(valor).strip().lower()
    if texto in ("", "nan"):
        raise ValueError("vacío")
    if texto == "o":
        return 0
    return int(float(texto))


def _importar_formato_ancho(df, jornada):
    """Formato: una fila por jugador, columnas por equipo en pares
    (equipo_local, equipo_visitante, equipo_local, equipo_visitante, ...).
    La primera columna es el nombre del jugador; las siguientes columnas
    que no sean equipos (p. ej. 'CAMPEON') deben descartarse antes de
    llamar a esta función si no vienen en pares."""
    columnas = list(df.columns)
    if len(columnas) < 3:
        raise ValueError("El archivo no tiene columnas de equipos suficientes.")

    col_jugador = columnas[0]
    col_campeon = columnas[1]  # segunda columna: campeón elegido
    columnas_equipos = columnas[2:]
    if len(columnas_equipos) % 2 != 0:
        columnas_equipos = columnas_equipos[:-1]

    pares = list(zip(columnas_equipos[0::2], columnas_equipos[1::2]))

    filas_ok, filas_error = 0, 0
    for _, fila in df.iterrows():
        nombre_jugador = fila[col_jugador]
        if pd.isna(nombre_jugador) or not str(nombre_jugador).strip():
            continue
        nombre_jugador = str(nombre_jugador).strip()

        campeon_val = fila[col_campeon]
        if not pd.isna(campeon_val) and str(campeon_val).strip():
            _guardar_prediccion_campeon(nombre_jugador, str(campeon_val).strip())

        for eq_local, eq_visitante in pares:
            valor_local = fila[eq_local]
            valor_visitante = fila[eq_visitante]
            if pd.isna(valor_local) and pd.isna(valor_visitante):
                continue
            try:
                pred_local = _parsear_marcador(valor_local)
                pred_visitante = _parsear_marcador(valor_visitante)
            except ValueError:
                filas_error += 1
                continue

            _guardar_prediccion(
                nombre_jugador, jornada, str(eq_local).strip(), str(eq_visitante).strip(),
                pred_local, pred_visitante,
            )
            filas_ok += 1

    return filas_ok, filas_error


def crear_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-esto-en-produccion")
    db_url = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(app.instance_path, "quiniela.db")
    )
    # Railway entrega 'postgres://' pero SQLAlchemy 2.x requiere 'postgresql://'
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB para el excel

    motor = "postgresql" if db_url.startswith("postgresql://") else "sqlite (NO persistente)"
    print(f"[quiniela] Conectando a base de datos: {motor}", flush=True)

    os.makedirs(app.instance_path, exist_ok=True)
    db.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.login_message = "Inicia sesión para acceder a esa sección."
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        if user_id == Admin.id:
            return Admin(os.environ.get("ADMIN_USERNAME", "admin"))
        return None

    with app.app_context():
        db.create_all()
        # Migración: agrega columnas nuevas si no existen (compatible SQLite + PG).
        # Cada DDL usa su propia conexión para que un fallo (ej. "columna ya existe")
        # no deje la transacción en estado abortado e impida los siguientes.
        for ddl in [
            "ALTER TABLE jugadores ADD COLUMN pais VARCHAR(2)",
            "ALTER TABLE predicciones_campeon ADD COLUMN puntos INTEGER",
            "ALTER TABLE partidos ADD COLUMN ganador_api VARCHAR(1)",
        ]:
            try:
                with db.engine.connect() as conn:
                    conn.execute(db.text(ddl))
                    conn.commit()
            except Exception:
                pass

    _GDL = ZoneInfo("America/Mexico_City")

    @app.template_filter("guadalajara")
    def to_guadalajara(dt):
        if dt is None:
            return ""
        gdt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(_GDL)
        return gdt.strftime("%d %b %Y · %H:%M")

    FASES_MUNDIAL = {
        1: "Jornada 1",
        2: "Jornada 2",
        3: "Jornada 3",
        4: "Dieciseisavos de Final",
        5: "Octavos de Final",
        6: "Cuartos de Final",
        7: "Semifinales",
        8: "Final",
    }
    FASES_CORTAS = {
        1: "J1", 2: "J2", 3: "J3",
        4: "16avos", 5: "8vos", 6: "4tos", 7: "SF", 8: "Final",
    }
    app.jinja_env.globals["FASES_MUNDIAL"] = FASES_MUNDIAL
    app.jinja_env.globals["FASES_CORTAS"] = FASES_CORTAS

    _AVATAR_COLORS = [
        "#0B6B3A", "#16365C", "#C8313B", "#D99B1C",
        "#7E5BA6", "#1A7FA1", "#C46E22", "#5B8C2A",
    ]

    def _avatar_color(jugador):
        return _AVATAR_COLORS[jugador.id % len(_AVATAR_COLORS)]

    def _inicial(jugador):
        return jugador.nombre[0].upper() if jugador.nombre else "?"

    def _bandera_emoji(pais):
        if not pais or len(pais) != 2:
            return ""
        base = 0x1F1E6 - ord("A")
        return chr(ord(pais[0].upper()) + base) + chr(ord(pais[1].upper()) + base)

    app.jinja_env.globals["avatar_color"] = _avatar_color
    app.jinja_env.globals["inicial"] = _inicial
    app.jinja_env.globals["bandera_emoji"] = _bandera_emoji
    app.jinja_env.filters["check_eliminado"] = _equipo_esta_eliminado

    # ---------- helpers internos ----------
    def _tomar_snapshot():
        """Guarda un snapshot del ranking actual para calcular movimiento ▲▼."""
        jugadores_all = Jugador.query.order_by(Jugador.nombre).all()
        preds_camp = {pc.jugador_id: pc for pc in PrediccionCampeon.query.all()}
        pts_map = {}
        for j in jugadores_all:
            pts = j.puntos_totales
            pc = preds_camp.get(j.id)
            pts += (pc.puntos or 0) if pc else 0
            pts_map[j.id] = pts
        tabla_snap = sorted(jugadores_all, key=lambda j: pts_map[j.id], reverse=True)
        now = datetime.utcnow()
        for i, j in enumerate(tabla_snap):
            db.session.add(SnapshotPosicion(jugador_id=j.id, posicion=i + 1,
                                            puntos=pts_map[j.id], tomado_en=now))
        db.session.flush()

    def _calcular_analisis_avanzado():
        """Métricas de analista de datos sobre todas las predicciones calificadas:
        por jugador (sesgo de marcador, racha, empates), por equipo (en cuáles
        falla más la gente) y por jornada (dificultad promedio)."""
        predicciones = (
            Prediccion.query.join(Partido)
            .filter(Partido.finalizado.is_(True))
            .all()
        )

        por_jugador = {}
        por_equipo = {}
        por_jornada = {}

        for pred in predicciones:
            p = pred.partido
            pts = pred.puntos or 0

            j = por_jugador.setdefault(pred.jugador_id, {
                "jugador": pred.jugador,
                "total": 0, "puntos": 0, "exactos": 0, "resultado": 0, "fallos": 0,
                "empates_predichos": 0, "empates_acertados": 0,
                "goles_predichos": 0, "goles_reales": 0,
            })
            j["total"] += 1
            j["puntos"] += pts
            if pts == 3:
                j["exactos"] += 1
            elif pts == 1:
                j["resultado"] += 1
            else:
                j["fallos"] += 1
            if pred.pred_local == pred.pred_visitante:
                j["empates_predichos"] += 1
                if p.marcador_local == p.marcador_visitante:
                    j["empates_acertados"] += 1
            j["goles_predichos"] += pred.pred_local + pred.pred_visitante
            j["goles_reales"] += p.marcador_local + p.marcador_visitante

            for equipo in (p.equipo_local, p.equipo_visitante):
                e = por_equipo.setdefault(equipo, {"equipo": equipo, "total": 0, "fallos": 0, "exactos": 0})
                e["total"] += 1
                if pts == 0:
                    e["fallos"] += 1
                elif pts == 3:
                    e["exactos"] += 1

            jn = por_jornada.setdefault(p.jornada, {"jornada": p.jornada, "total": 0, "puntos": 0})
            jn["total"] += 1
            jn["puntos"] += pts

        jugadores_out = []
        for d in por_jugador.values():
            total = d["total"]
            jugadores_out.append({
                "jugador": d["jugador"],
                "puntos": d["puntos"],
                "exactos": d["exactos"],
                "resultado": d["resultado"],
                "fallos": d["fallos"],
                "pct_acierto": round((d["exactos"] + d["resultado"]) / total * 100, 1) if total else 0,
                "promedio": round(d["puntos"] / total, 2) if total else 0,
                "empates_predichos": d["empates_predichos"],
                "empates_acertados": d["empates_acertados"],
                "sesgo_goles": round((d["goles_predichos"] - d["goles_reales"]) / total, 2) if total else 0,
            })
        jugadores_out.sort(key=lambda x: x["puntos"], reverse=True)

        equipos_out = []
        for d in por_equipo.values():
            total = d["total"]
            equipos_out.append({
                "equipo": d["equipo"],
                "total_predicciones": total,
                "pct_fallo": round(d["fallos"] / total * 100, 1) if total else 0,
                "pct_exacto": round(d["exactos"] / total * 100, 1) if total else 0,
            })
        equipos_out.sort(key=lambda x: x["pct_fallo"], reverse=True)

        jornadas_out = []
        for d in sorted(por_jornada.values(), key=lambda x: x["jornada"]):
            total = d["total"]
            jornadas_out.append({
                "jornada": d["jornada"],
                "promedio_puntos": round(d["puntos"] / total, 2) if total else 0,
            })

        return jugadores_out, equipos_out, jornadas_out

    def _sincronizar_calendario():
        """Trae el calendario completo (104 partidos) desde la API y
        crea/actualiza los registros de Partido. Si la API ya marca un
        partido como FINISHED, guarda también el marcador."""
        todos_en_db = Partido.query.all()
        partidos_api = football_api.obtener_partidos()
        creados, actualizados = 0, 0
        for p in partidos_api:
            datos = football_api.extraer_resultado(p)
            if not datos["jornada"] or not datos["equipo_local"] or not datos["equipo_visitante"]:
                continue

            # 1. Buscar por api_match_id
            partido = Partido.query.filter_by(api_match_id=datos["api_match_id"]).first()

            # 2. Buscar por nombre exacto
            if partido is None:
                partido = Partido.query.filter_by(
                    jornada=datos["jornada"],
                    equipo_local=datos["equipo_local"],
                    equipo_visitante=datos["equipo_visitante"],
                ).first()

            # 3. Buscar por alias (nombre en español u otra variante)
            if partido is None:
                for p_db in todos_en_db:
                    if (football_api.equipos_coinciden(p_db.equipo_local, datos["equipo_local"])
                            and football_api.equipos_coinciden(p_db.equipo_visitante, datos["equipo_visitante"])
                            and not p_db.api_match_id):
                        partido = p_db
                        break

            # 4. Buscar por alias con local/visitante invertidos: en partidos de
            # sede neutral (semifinales, final) el orden "local/visitante" es solo
            # nominal y puede venir al revés de como quedó cargado originalmente.
            invertido = False
            if partido is None:
                for p_db in todos_en_db:
                    if (football_api.equipos_coinciden(p_db.equipo_local, datos["equipo_visitante"])
                            and football_api.equipos_coinciden(p_db.equipo_visitante, datos["equipo_local"])
                            and not p_db.api_match_id):
                        partido = p_db
                        invertido = True
                        break

            if partido is None:
                partido = Partido(
                    jornada=datos["jornada"],
                    equipo_local=datos["equipo_local"],
                    equipo_visitante=datos["equipo_visitante"],
                )
                db.session.add(partido)
                todos_en_db.append(partido)
                creados += 1
            else:
                actualizados += 1

            partido.api_match_id = datos["api_match_id"]
            if datos["fecha"]:
                partido.fecha = datetime.fromisoformat(datos["fecha"].replace("Z", "+00:00"))

            if datos["estado"] == "FINISHED":
                ml, mv = datos["marcador_local"], datos["marcador_visitante"]
                if invertido:
                    ml, mv = mv, ml
                partido.marcador_local = ml
                partido.marcador_visitante = mv
                partido.finalizado = True

        db.session.commit()
        return creados, actualizados

    def _sincronizar_resultados(jornada=None):
        """Actualiza fechas para todos los partidos y marcadores/puntos para los FINISHED."""
        partidos_api = football_api.obtener_partidos(jornada=jornada)
        partidos_actualizados = 0
        predicciones_calificadas = 0
        fechas_actualizadas = 0
        encontrados_en_db = 0  # partidos API que encontraron al menos un registro en DB
        total_api = 0
        sin_match = []

        todos_en_db = Partido.query.all()
        for p in partidos_api:
            datos = football_api.extraer_resultado(p)
            if not datos["jornada"] or not datos["equipo_local"] or not datos["equipo_visitante"]:
                continue

            total_api += 1
            ml = datos["marcador_local"]
            mv = datos["marcador_visitante"]
            # Solo consideramos finalizado si la API marcó FINISHED Y el marcador llegó
            es_finalizado = datos["estado"] == "FINISHED" and ml is not None and mv is not None

            por_api_id = Partido.query.filter_by(api_match_id=datos["api_match_id"]).first()

            por_alias = {}
            por_alias_invertido = {}
            for p_db in todos_en_db:
                local_ok = football_api.equipos_coinciden(p_db.equipo_local, datos["equipo_local"])
                visit_ok = football_api.equipos_coinciden(p_db.equipo_visitante, datos["equipo_visitante"])
                if local_ok and visit_ok:
                    por_alias[p_db.id] = p_db
                    continue
                # Sede neutral (semifinales, final): el orden local/visitante puede
                # venir invertido respecto a como quedó cargado el partido en la DB.
                local_ok_inv = football_api.equipos_coinciden(p_db.equipo_local, datos["equipo_visitante"])
                visit_ok_inv = football_api.equipos_coinciden(p_db.equipo_visitante, datos["equipo_local"])
                if local_ok_inv and visit_ok_inv:
                    por_alias_invertido[p_db.id] = p_db

            if not por_api_id and not por_alias and not por_alias_invertido:
                if es_finalizado:
                    sin_match.append(f"{datos['equipo_local']} vs {datos['equipo_visitante']}")
                continue

            encontrados_en_db += 1
            fecha_dt = None
            if datos["fecha"]:
                fecha_dt = datetime.fromisoformat(datos["fecha"].replace("Z", "+00:00"))

            def _aplicar_resultado(partido, invertido=False):
                """Aplica resultado API a un partido y recalifica predicciones.
                Si `invertido` es True, el local/visitante de la API está al
                revés respecto a como está guardado `partido`, así que el
                marcador y el ganador se voltean antes de aplicarlos."""
                nonlocal partidos_actualizados, predicciones_calificadas, fechas_actualizadas
                if fecha_dt:
                    if not partido.fecha:
                        fechas_actualizadas += 1
                    partido.fecha = fecha_dt
                if es_finalizado:
                    m_local, m_visit = (mv, ml) if invertido else (ml, mv)
                    partido.marcador_local = m_local
                    partido.marcador_visitante = m_visit
                    partido.finalizado = True
                    # Guardar ganador real (incluye ET/penales): "L", "V" o None
                    w = datos.get("winner", "")
                    if invertido:
                        w = {"HOME_TEAM": "AWAY_TEAM", "AWAY_TEAM": "HOME_TEAM"}.get(w, w)
                    if w == "HOME_TEAM":
                        partido.ganador_api = "L"
                    elif w == "AWAY_TEAM":
                        partido.ganador_api = "V"
                    else:
                        partido.ganador_api = None
                    partidos_actualizados += 1
                    for pred in partido.predicciones:
                        pred.puntos = calcular_puntos(pred.pred_local, pred.pred_visitante, m_local, m_visit)
                        predicciones_calificadas += 1
                elif partido.finalizado and (partido.marcador_local is None or partido.marcador_visitante is None):
                    # Quedó marcado finalizado sin marcador en un sync previo; resetear
                    partido.finalizado = False

            # Actualizar partido principal (con api_match_id)
            if por_api_id:
                por_api_id.api_match_id = datos["api_match_id"]
                _aplicar_resultado(por_api_id)

            # Actualizar partidos encontrados por alias (nombre en español u otra variante)
            for partido in por_alias.values():
                if por_api_id and partido.id == por_api_id.id:
                    continue
                if not partido.api_match_id:
                    partido.api_match_id = datos["api_match_id"]
                _aplicar_resultado(partido)

            # Actualizar partidos encontrados por alias con orden invertido
            for partido in por_alias_invertido.values():
                if por_api_id and partido.id == por_api_id.id:
                    continue
                if not partido.api_match_id:
                    partido.api_match_id = datos["api_match_id"]
                _aplicar_resultado(partido, invertido=True)

        db.session.commit()
        return partidos_actualizados, predicciones_calificadas, fechas_actualizadas, sin_match, total_api, encontrados_en_db

    # ---------- comandos CLI ----------
    @app.cli.command("init-db")
    def init_db():
        """Crea las tablas en la base de datos."""
        db.create_all()
        print("Base de datos inicializada.")

    @app.cli.command("sync-fixtures")
    def sync_fixtures_cmd():
        """Trae TODO el calendario del Mundial desde football-data.org."""
        creados, actualizados = _sincronizar_calendario()
        print(f"Calendario sincronizado: {creados} creados, {actualizados} actualizados.")

    @app.cli.command("sync-resultados")
    @click.argument("jornada", type=int, required=False, default=None)
    def sync_resultados_cmd(jornada):
        """Sincroniza resultados reales y recalcula puntos.
        Uso: flask --app app sync-resultados [jornada]
        Si no se pasa jornada, sincroniza TODAS las disponibles."""
        partidos_act, preds_calif, fechas_act, sin_match, total_api, encontrados = _sincronizar_resultados(jornada=jornada)
        print(f"API: {total_api} partidos · encontrados en DB: {encontrados} · {partidos_act} actualizados · {preds_calif} predicciones · {fechas_act} fechas nuevas.")
        if sin_match:
            print("Sin match en DB:", ", ".join(sin_match))

    # ---------- autenticación ----------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if _admin_credenciales_validas(username, password):
                login_user(Admin(username))
                return redirect(request.args.get("next") or url_for("index"))
            flash("Usuario o contraseña incorrectos.", "error")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("index"))

    # ---------- rutas ----------
    @app.route("/analisis")
    def analisis():
        jugadores = Jugador.query.order_by(Jugador.nombre).all()
        jornadas = sorted({p.jornada for p in Partido.query.all()})

        preds_campeon = {pc.jugador_id: pc for pc in PrediccionCampeon.query.all()}
        cfg = ConfigApp.query.filter_by(clave="campeon_real").first()
        campeon_real = cfg.valor if cfg else None

        # Desglose de picks del campeón
        from collections import Counter
        picks_count = Counter(pc.equipo for pc in preds_campeon.values())
        picks_ranking = picks_count.most_common()

        player_stats = []
        for j in jugadores:
            pts_por_jornada = {}
            for jn in jornadas:
                pts = sum(
                    p.puntos or 0
                    for p in j.predicciones
                    if p.partido.jornada == jn and p.puntos is not None
                )
                pts_por_jornada[jn] = pts

            calificadas = [p for p in j.predicciones if p.puntos is not None]
            total = len(j.predicciones)
            n_cal = len(calificadas)
            exactos = sum(1 for p in calificadas if p.puntos == 3)
            result1 = sum(1 for p in calificadas if p.puntos == 1)
            pct = round((exactos + result1) / n_cal * 100) if n_cal else 0
            promedio = round(j.puntos_totales / n_cal, 2) if n_cal else 0
            mejor_jn = max(pts_por_jornada, key=pts_por_jornada.get) if pts_por_jornada else None
            pc = preds_campeon.get(j.id)
            pts_total = j.puntos_totales + ((pc.puntos or 0) if pc else 0)

            player_stats.append({
                "jugador": j,
                "puntos": pts_total,
                "exactos": exactos,
                "resultado": result1,
                "total_preds": total,
                "calificadas": n_cal,
                "pct": pct,
                "promedio": promedio,
                "pts_por_jornada": pts_por_jornada,
                "mejor_jornada": mejor_jn,
                "pred_campeon": pc,
            })

        player_stats.sort(key=lambda x: x["puntos"], reverse=True)

        partidos_fin = Partido.query.filter_by(finalizado=True).all()
        partido_stats = []
        for partido in partidos_fin:
            cal = [p for p in partido.predicciones if p.puntos is not None]
            if not cal:
                continue
            ex = sum(1 for p in cal if p.puntos == 3)
            re = sum(1 for p in cal if p.puntos == 1)
            partido_stats.append({
                "partido": partido,
                "total": len(cal),
                "exactos": ex,
                "resultado": re,
                "pct": round((ex + re) / len(cal) * 100),
            })

        partido_stats.sort(key=lambda x: x["pct"])
        mas_dificiles = partido_stats[:5]
        mas_faciles = list(reversed(partido_stats[-5:]))

        return render_template(
            "analisis.html",
            player_stats=player_stats,
            jornadas=jornadas,
            mas_dificiles=mas_dificiles,
            mas_faciles=mas_faciles,
            campeon_real=campeon_real,
            picks_ranking=picks_ranking,
        )

    @app.route("/data")
    @login_required
    def data_dashboard():
        jugadores_stats, equipos_stats, jornadas_stats = _calcular_analisis_avanzado()
        return render_template(
            "data.html",
            jugadores_stats=jugadores_stats,
            equipos_stats=equipos_stats,
            jornadas_stats=jornadas_stats,
        )

    @app.route("/data/export.xlsx")
    @login_required
    def data_export():
        predicciones = (
            Prediccion.query.join(Partido).join(Jugador)
            .order_by(Partido.jornada, Partido.equipo_local, Jugador.nombre)
            .all()
        )
        filas_predicciones = [{
            "jornada": pred.partido.jornada,
            "jugador": pred.jugador.nombre,
            "equipo_local": pred.partido.equipo_local,
            "equipo_visitante": pred.partido.equipo_visitante,
            "pred_local": pred.pred_local,
            "pred_visitante": pred.pred_visitante,
            "marcador_local_real": pred.partido.marcador_local,
            "marcador_visitante_real": pred.partido.marcador_visitante,
            "finalizado": pred.partido.finalizado,
            "puntos": pred.puntos,
        } for pred in predicciones]

        jugadores_stats, equipos_stats, jornadas_stats = _calcular_analisis_avanzado()
        filas_jugadores = [{
            "jugador": d["jugador"].nombre,
            "puntos": d["puntos"],
            "exactos": d["exactos"],
            "resultado": d["resultado"],
            "fallos": d["fallos"],
            "pct_acierto": d["pct_acierto"],
            "promedio_puntos": d["promedio"],
            "empates_predichos": d["empates_predichos"],
            "empates_acertados": d["empates_acertados"],
            "sesgo_goles": d["sesgo_goles"],
        } for d in jugadores_stats]

        preds_campeon = {pc.jugador_id: pc for pc in PrediccionCampeon.query.all()}
        filas_posiciones = []
        for j in Jugador.query.order_by(Jugador.nombre).all():
            pc = preds_campeon.get(j.id)
            puntos_campeon = (pc.puntos or 0) if pc else 0
            filas_posiciones.append({
                "jugador": j.nombre,
                "puntos_predicciones": j.puntos_totales,
                "puntos_campeon": puntos_campeon,
                "puntos_total": j.puntos_totales + puntos_campeon,
                "exactos": j.aciertos_exactos,
                "resultado": j.aciertos_resultado,
            })
        filas_posiciones.sort(key=lambda x: x["puntos_total"], reverse=True)
        for i, fila in enumerate(filas_posiciones, start=1):
            fila["posicion"] = i

        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            pd.DataFrame(filas_posiciones).to_excel(writer, sheet_name="tabla_posiciones", index=False)
            pd.DataFrame(filas_predicciones).to_excel(writer, sheet_name="predicciones", index=False)
            pd.DataFrame(filas_jugadores).to_excel(writer, sheet_name="analisis_jugadores", index=False)
            pd.DataFrame(equipos_stats).to_excel(writer, sheet_name="analisis_equipos", index=False)
            pd.DataFrame(jornadas_stats).to_excel(writer, sheet_name="analisis_jornadas", index=False)
        buffer.seek(0)

        nombre = f"quiniela_data_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
        return send_file(
            buffer,
            as_attachment=True,
            download_name=nombre,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/")
    def index():
        jornada_sel = request.args.get("jornada", type=int)
        jugadores = Jugador.query.all()
        todos_partidos = Partido.query.all()
        jornadas = sorted({p.jornada for p in todos_partidos})
        partidos_por_jornada = {
            jn: sum(1 for p in todos_partidos if p.jornada == jn)
            for jn in jornadas
        }

        preds_campeon = {pc.jugador_id: pc for pc in PrediccionCampeon.query.all()}
        cfg = ConfigApp.query.filter_by(clave="campeon_real").first()
        campeon_real = cfg.valor if cfg else None
        cfg_cerrada = ConfigApp.query.filter_by(clave="quiniela_cerrada").first()
        quiniela_cerrada = bool(cfg_cerrada and cfg_cerrada.valor == "1")
        equipos_eliminados = _get_equipos_eliminados()
        cfg_extra = ConfigApp.query.filter_by(clave="equipos_eliminados_extra").first()
        eliminados_extra_str = cfg_extra.valor if cfg_extra else ""

        stats = {}
        for j in jugadores:
            preds = j.predicciones
            if jornada_sel is not None:
                preds = [p for p in preds if p.partido.jornada == jornada_sel]
            pts = sum(p.puntos or 0 for p in preds)
            if jornada_sel is None:
                pc = preds_campeon.get(j.id)
                pts += (pc.puntos or 0) if pc else 0
            stats[j.id] = {
                "puntos": pts,
                "exactos": sum(1 for p in preds if p.puntos == 3),
                "resultado": sum(1 for p in preds if p.puntos == 1),
            }

        tabla = sorted(jugadores, key=lambda j: stats[j.id]["puntos"], reverse=True)

        last_snap = db.session.query(SnapshotPosicion.tomado_en).order_by(
            SnapshotPosicion.tomado_en.desc()
        ).first()
        movimiento = {}
        if last_snap:
            prev = {s.jugador_id: s.posicion
                    for s in SnapshotPosicion.query.filter_by(tomado_en=last_snap[0]).all()}
            for i, j in enumerate(tabla):
                p = prev.get(j.id)
                movimiento[j.id] = (p - (i + 1)) if p is not None else None

        return render_template("index.html", tabla=tabla, jornadas=jornadas,
                               partidos_por_jornada=partidos_por_jornada,
                               jornada_sel=jornada_sel, stats=stats,
                               preds_campeon=preds_campeon, campeon_real=campeon_real,
                               movimiento=movimiento,
                               equipos_eliminados=equipos_eliminados,
                               eliminados_extra_str=eliminados_extra_str,
                               quiniela_cerrada=quiniela_cerrada)

    @app.route("/campeon", methods=["POST"])
    @login_required
    def set_campeon():
        equipo = request.form.get("campeon_real", "").strip()
        if not equipo:
            flash("Indica el nombre del equipo campeón.", "error")
            return redirect(url_for("index"))
        cfg = ConfigApp.query.filter_by(clave="campeon_real").first()
        if cfg:
            cfg.valor = equipo
        else:
            db.session.add(ConfigApp(clave="campeon_real", valor=equipo))
        for pc in PrediccionCampeon.query.all():
            pc.puntos = 5 if football_api.equipos_coinciden(pc.equipo, equipo) else 0
        db.session.commit()
        ganadores = sum(1 for pc in PrediccionCampeon.query.all() if pc.puntos == 5)
        flash(f"Campeón establecido: {equipo}. {ganadores} jugador(es) con 5 pts extra.", "success")
        return redirect(url_for("index"))

    @app.route("/cerrar", methods=["POST"])
    @login_required
    def cerrar_quiniela():
        equipo = request.form.get("campeon_real", "España").strip() or "España"
        # Guardar campeón
        cfg = ConfigApp.query.filter_by(clave="campeon_real").first()
        if cfg:
            cfg.valor = equipo
        else:
            db.session.add(ConfigApp(clave="campeon_real", valor=equipo))
        for pc in PrediccionCampeon.query.all():
            pc.puntos = 5 if football_api.equipos_coinciden(pc.equipo, equipo) else 0
        # Marcar quiniela como cerrada
        cfg_c = ConfigApp.query.filter_by(clave="quiniela_cerrada").first()
        if cfg_c:
            cfg_c.valor = "1"
        else:
            db.session.add(ConfigApp(clave="quiniela_cerrada", valor="1"))
        db.session.commit()
        ganadores = sum(1 for pc in PrediccionCampeon.query.all() if pc.puntos == 5)
        flash(f"¡Quiniela cerrada! Campeón: {equipo}. {ganadores} jugador(es) con +5 pts.", "success")
        return redirect(url_for("index"))

    @app.route("/abrir", methods=["POST"])
    @login_required
    def abrir_quiniela():
        cfg_c = ConfigApp.query.filter_by(clave="quiniela_cerrada").first()
        if cfg_c:
            cfg_c.valor = "0"
            db.session.commit()
        flash("Quiniela reabierta.", "success")
        return redirect(url_for("index"))

    @app.route("/eliminados", methods=["POST"])
    @login_required
    def set_eliminados_extra():
        valor = request.form.get("eliminados_extra", "").strip()
        cfg = ConfigApp.query.filter_by(clave="equipos_eliminados_extra").first()
        if cfg:
            cfg.valor = valor
        else:
            db.session.add(ConfigApp(clave="equipos_eliminados_extra", valor=valor))
        db.session.commit()
        flash("Equipos eliminados extra guardados.", "success")
        return redirect(url_for("index"))

    @app.route("/jornada/<int:jornada>")
    def ver_jornada(jornada):
        partidos = Partido.query.filter_by(jornada=jornada).order_by(
            db.text("fecha IS NULL"), Partido.fecha
        ).all()
        jugadores = Jugador.query.order_by(Jugador.nombre).all()

        predicciones = {
            (p.jugador_id, p.partido_id): p
            for p in Prediccion.query.join(Partido).filter(Partido.jornada == jornada).all()
        }

        jornadas = sorted({p.jornada for p in Partido.query.all()})

        return render_template(
            "jornada.html",
            jornada=jornada,
            jornadas=jornadas,
            partidos=partidos,
            jugadores=jugadores,
            predicciones=predicciones,
        )

    @app.route("/jornada/<int:jornada>/estado")
    def estado_jornada(jornada):
        partidos = Partido.query.filter_by(jornada=jornada).all()
        ahora = datetime.utcnow()
        data = []
        for p in partidos:
            if p.finalizado:
                estado = "finalizado"
            elif p.fecha and p.fecha <= ahora <= p.fecha + timedelta(hours=3):
                estado = "vivo"
            else:
                estado = "pendiente"
            data.append({
                "id": p.id,
                "estado": estado,
                "marcador_local": p.marcador_local,
                "marcador_visitante": p.marcador_visitante,
                "predicciones": [
                    {"jugador_id": pred.jugador_id, "puntos": pred.puntos}
                    for pred in p.predicciones
                ],
            })
        return {"partidos": data}

    @app.route("/sync/<int:jornada>", methods=["POST"])
    @login_required
    def sync_jornada(jornada):
        try:
            _tomar_snapshot()
            partidos_act, preds_calif, fechas_act, sin_match, total_api, encontrados = _sincronizar_resultados(jornada=jornada)
            if total_api == 0:
                flash(
                    "La API no devolvió partidos para esta jornada. "
                    "Verifica tu FOOTBALL_DATA_API_KEY o inténtalo más tarde.",
                    "error",
                )
            elif encontrados == 0:
                flash(
                    f"La API devolvió {total_api} partidos pero ninguno coincide con los nombres en tu DB. "
                    "Revisa los nombres de equipo.",
                    "error",
                )
            elif partidos_act == 0 and fechas_act == 0:
                flash(
                    f"Calendario OK ({encontrados}/{total_api} partidos sincronizados). "
                    "Sin cambios — fechas ya cargadas, resultados aún no disponibles.",
                    "success",
                )
            elif partidos_act == 0:
                flash(
                    f"Fechas actualizadas ({fechas_act} partido(s)). Aún no hay resultados finalizados.",
                    "success",
                )
            else:
                flash(
                    f"Listo: {partidos_act} partido(s) con resultado, "
                    f"{preds_calif} predicción(es) calificadas, {fechas_act} fecha(s) nuevas.",
                    "success",
                )
            if sin_match:
                flash(
                    "Sin match en DB (revisa el nombre): " + " · ".join(sin_match),
                    "error",
                )
        except football_api.FootballDataError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            app.logger.exception("Error inesperado sincronizando jornada %s", jornada)
            flash(f"Error inesperado al sincronizar: {exc}", "error")
        return redirect(url_for("ver_jornada", jornada=jornada))

    @app.route("/partido/<int:partido_id>/resultado", methods=["POST"])
    @login_required
    def set_resultado(partido_id):
        partido = Partido.query.get_or_404(partido_id)
        try:
            ml = int(request.form["marcador_local"])
            mv = int(request.form["marcador_visitante"])
        except (KeyError, ValueError):
            flash("Marcador inválido.", "error")
            return redirect(url_for("ver_jornada", jornada=partido.jornada))
        partido.marcador_local = ml
        partido.marcador_visitante = mv
        partido.finalizado = True
        for pred in partido.predicciones:
            pred.puntos = calcular_puntos(pred.pred_local, pred.pred_visitante, ml, mv)
        db.session.commit()
        flash(f"Resultado guardado: {partido.equipo_local} {ml} - {mv} {partido.equipo_visitante}", "success")
        return redirect(url_for("ver_jornada", jornada=partido.jornada))

    @app.route("/partido/<int:partido_id>/renombrar-equipos", methods=["POST"])
    @login_required
    def renombrar_equipos(partido_id):
        partido = Partido.query.get_or_404(partido_id)
        eq_local = request.form.get("equipo_local", "").strip()
        eq_visitante = request.form.get("equipo_visitante", "").strip()
        if not eq_local or not eq_visitante:
            flash("Los nombres de equipo no pueden estar vacíos.", "error")
            return redirect(url_for("ver_jornada", jornada=partido.jornada))
        partido.equipo_local = eq_local
        partido.equipo_visitante = eq_visitante
        db.session.commit()
        flash(f"Renombrado: {eq_local} vs {eq_visitante}. Ahora corre \"Fix duplicados\" si aplica.", "success")
        return redirect(url_for("ver_jornada", jornada=partido.jornada))

    @app.route("/partido/<int:partido_id>/eliminar", methods=["POST"])
    @login_required
    def eliminar_partido(partido_id):
        partido = Partido.query.get_or_404(partido_id)
        jornada = partido.jornada
        nombre = f"{partido.equipo_local} vs {partido.equipo_visitante}"
        db.session.delete(partido)
        db.session.commit()
        flash(f"Partido '{nombre}' eliminado.", "success")
        return redirect(url_for("ver_jornada", jornada=jornada))

    @app.route("/jornada/<int:jornada>/eliminar", methods=["POST"])
    @login_required
    def eliminar_jornada(jornada):
        partidos = Partido.query.filter_by(jornada=jornada).all()
        cantidad = len(partidos)
        for partido in partidos:
            db.session.delete(partido)  # cascade borra también sus predicciones
        db.session.commit()
        flash(f"Se eliminó la jornada {jornada}: {cantidad} partido(s) y sus predicciones.", "success")
        return redirect(url_for("index"))

    @app.route("/sync-calendario", methods=["POST"])
    @login_required
    def sync_calendario():
        try:
            creados, actualizados = _sincronizar_calendario()
            flash(f"Calendario sincronizado: {creados} creados, {actualizados} actualizados.", "success")
        except football_api.FootballDataError as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.route("/upload", methods=["GET", "POST"])
    @login_required
    def upload():
        if request.method == "POST":
            archivo = request.files.get("archivo")
            if not archivo or archivo.filename == "":
                flash("Selecciona un archivo .xlsx, .csv o .pdf", "error")
                return redirect(url_for("upload"))

            es_csv = archivo.filename.lower().endswith(".csv")
            es_pdf = archivo.filename.lower().endswith(".pdf")

            if es_pdf:
                # PDF: extraer tabla, convertir a formato ancho y pasar directo a importar
                jornada_form = request.form.get("jornada", "").strip()
                jornada = int(jornada_form) if jornada_form.isdigit() else None
                if jornada is None:
                    flash("Para archivos PDF debes seleccionar la fase en el desplegable.", "error")
                    return redirect(url_for("upload"))
                try:
                    import io as _io
                    archivo_bytes = _io.BytesIO(archivo.read())
                    df_pdf = _leer_pdf_como_df(archivo_bytes)
                    filas_ok, filas_error = _importar_formato_ancho(df_pdf, jornada)
                except ValueError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("upload"))
                except Exception as exc:
                    flash(f"No se pudo procesar el PDF: {exc}", "error")
                    return redirect(url_for("upload"))
                db.session.commit()
                flash(f"Importadas {filas_ok} predicciones desde PDF. {filas_error} fila(s) con error.", "success")
                return redirect(url_for("index"))

            try:
                if es_csv:
                    df = pd.read_csv(archivo)
                else:
                    df = pd.read_excel(archivo)
            except Exception as exc:
                flash(f"No se pudo leer el archivo: {exc}", "error")
                return redirect(url_for("upload"))

            columnas_esperadas = {
                "jornada", "jugador", "equipo_local", "equipo_visitante",
                "pred_local", "pred_visitante",
            }
            df.columns = [str(c).strip().lower() for c in df.columns]

            if columnas_esperadas <= set(df.columns):
                filas_ok, filas_error = _importar_formato_largo(df)
            else:
                # Formato "ancho": una fila por jugador, columnas por equipo
                # en pares (equipo_local, equipo_visitante, equipo_local, ...).
                jornada_form = request.form.get("jornada", "").strip()
                jornada = None
                if jornada_form.isdigit():
                    jornada = int(jornada_form)
                else:
                    m = re.search(r"jornada[_\s-]*(\d+)", archivo.filename, re.IGNORECASE)
                    if m:
                        jornada = int(m.group(1))

                if jornada is None:
                    flash(
                        "No se pudo detectar la jornada. Indícala en el campo "
                        "'Jornada' del formulario o nombra el archivo tipo "
                        "'JORNADA_2.xlsx'.",
                        "error",
                    )
                    return redirect(url_for("upload"))

                try:
                    df_ancho = pd.read_excel(archivo, header=1) if not es_csv else pd.read_csv(archivo, header=1)
                except Exception as exc:
                    flash(f"No se pudo leer el archivo: {exc}", "error")
                    return redirect(url_for("upload"))

                try:
                    filas_ok, filas_error = _importar_formato_ancho(df_ancho, jornada)
                except ValueError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("upload"))

            db.session.commit()
            flash(f"Importadas {filas_ok} predicciones. {filas_error} fila(s) con error.", "success")
            return redirect(url_for("index"))

        return render_template("upload.html")

    @app.route("/jugadores", methods=["GET", "POST"])
    @login_required
    def jugadores():
        if request.method == "POST":
            nombre = request.form.get("nombre", "").strip()
            pais_raw = request.form.get("pais", "").strip().upper()
            pais = pais_raw[:2] if len(pais_raw) == 2 else None
            if nombre:
                existe = _buscar_jugador_insensible(nombre)
                if existe:
                    flash("Ese jugador ya existe.", "error")
                else:
                    db.session.add(Jugador(nombre=nombre, pais=pais))
                    db.session.commit()
                    flash(f"Jugador '{nombre}' agregado.", "success")
            return redirect(url_for("jugadores"))

        lista = Jugador.query.order_by(Jugador.nombre).all()
        return render_template("jugadores.html", jugadores=lista)

    @app.route("/jugadores/<int:jugador_id>/pais", methods=["POST"])
    @login_required
    def set_pais_jugador(jugador_id):
        jugador = Jugador.query.get_or_404(jugador_id)
        pais_raw = request.form.get("pais", "").strip().upper()
        jugador.pais = pais_raw[:2] if len(pais_raw) == 2 else None
        db.session.commit()
        return redirect(url_for("jugadores"))

    @app.route("/jugadores/<int:jugador_id>/eliminar", methods=["POST"])
    @login_required
    def eliminar_jugador(jugador_id):
        jugador = Jugador.query.get_or_404(jugador_id)
        db.session.delete(jugador)
        db.session.commit()
        flash(f"Jugador '{jugador.nombre}' eliminado.", "success")
        return redirect(url_for("jugadores"))

    @app.route("/admin/diagnostico")
    @login_required
    def admin_diagnostico():
        if request.args.get("token") != app.config["SECRET_KEY"]:
            return "no autorizado", 404
        data = []
        for j in Jugador.query.order_by(Jugador.nombre).all():
            pc = j.prediccion_campeon
            data.append({
                "id": j.id,
                "nombre": j.nombre,
                "puntos_totales": j.puntos_totales,
                "num_predicciones": len(j.predicciones),
                "prediccion_campeon": (
                    {"equipo": pc.equipo, "puntos": pc.puntos} if pc else None
                ),
            })
        return {"jugadores": data}

    @app.route("/admin/diagnostico-jornada/<int:jornada>")
    @login_required
    def admin_diagnostico_jornada(jornada):
        partidos = Partido.query.filter_by(jornada=jornada).order_by(Partido.equipo_local).all()
        data = [{
            "id": p.id,
            "equipo_local": p.equipo_local,
            "equipo_visitante": p.equipo_visitante,
            "fecha": p.fecha.isoformat() if p.fecha else None,
            "api_match_id": p.api_match_id,
            "finalizado": p.finalizado,
            "num_predicciones": len(p.predicciones),
        } for p in partidos]
        return {"jornada": jornada, "total": len(data), "partidos": data}

    @app.route("/admin/fix-duplicados", methods=["POST"])
    @login_required
    def fix_duplicados():
        """Fusiona pares de partidos duplicados (español + inglés) en cada jornada."""
        todos = Partido.query.all()
        fusionados = 0

        jornadas_set = sorted({p.jornada for p in todos})
        for jn in jornadas_set:
            partidos_jn = [p for p in todos if p.jornada == jn]
            procesados = set()

            for p1 in partidos_jn:
                if p1.id in procesados:
                    continue
                for p2 in partidos_jn:
                    if p2.id == p1.id or p2.id in procesados:
                        continue

                    directo = (football_api.equipos_coinciden(p1.equipo_local, p2.equipo_local)
                               and football_api.equipos_coinciden(p1.equipo_visitante, p2.equipo_visitante))
                    # Sede neutral (semifinales, final): mismo cruce pero con
                    # local/visitante intercambiados entre los dos registros.
                    invertido = (not directo
                                 and football_api.equipos_coinciden(p1.equipo_local, p2.equipo_visitante)
                                 and football_api.equipos_coinciden(p1.equipo_visitante, p2.equipo_local))

                    if directo or invertido:
                        # Keeper = el que tiene predicciones; other = el vacío
                        keeper = p1 if len(p1.predicciones) >= len(p2.predicciones) else p2
                        other  = p2 if keeper is p1 else p1
                        # other_invertido: True si "other" tiene local/visitante
                        # al revés respecto a la orientación de "keeper"
                        other_invertido = invertido

                        if not keeper.fecha and other.fecha:
                            keeper.fecha = other.fecha
                        if not keeper.api_match_id and other.api_match_id:
                            api_id = other.api_match_id
                            other.api_match_id = None   # libera UNIQUE antes de asignarlo
                            db.session.flush()
                            keeper.api_match_id = api_id
                        if not keeper.finalizado and other.finalizado:
                            if other_invertido:
                                keeper.marcador_local   = other.marcador_visitante
                                keeper.marcador_visitante = other.marcador_local
                            else:
                                keeper.marcador_local   = other.marcador_local
                                keeper.marcador_visitante = other.marcador_visitante
                            keeper.finalizado = True
                            for pred in keeper.predicciones:
                                pred.puntos = calcular_puntos(
                                    pred.pred_local, pred.pred_visitante,
                                    keeper.marcador_local, keeper.marcador_visitante)

                        for pred in list(other.predicciones):
                            if other_invertido:
                                pred.pred_local, pred.pred_visitante = pred.pred_visitante, pred.pred_local
                            existe = Prediccion.query.filter_by(
                                jugador_id=pred.jugador_id, partido_id=keeper.id).first()
                            if existe:
                                db.session.delete(pred)
                            else:
                                pred.partido_id = keeper.id
                                if keeper.finalizado:
                                    pred.puntos = calcular_puntos(
                                        pred.pred_local, pred.pred_visitante,
                                        keeper.marcador_local, keeper.marcador_visitante)

                        db.session.delete(other)
                        procesados.add(other.id)
                        fusionados += 1
                        break
                procesados.add(p1.id)

        db.session.commit()
        flash(f"Duplicados fusionados: {fusionados} par(es) eliminados.", "success")
        return redirect(url_for("index"))

    @app.route("/jugador/<int:jugador_id>")
    def detalle_jugador(jugador_id):
        jugador = Jugador.query.get_or_404(jugador_id)
        predicciones = (
            Prediccion.query.filter_by(jugador_id=jugador_id)
            .join(Partido)
            .order_by(Partido.jornada, Partido.fecha)
            .all()
        )
        # Ranking del jugador entre todos
        todos = sorted(Jugador.query.all(), key=lambda j: j.puntos_totales, reverse=True)
        ranking = next((i + 1 for i, j in enumerate(todos) if j.id == jugador_id), None)
        total_jugadores = len(todos)
        pc = PrediccionCampeon.query.filter_by(jugador_id=jugador_id).first()
        equipos_eliminados = _get_equipos_eliminados()
        return render_template(
            "jugador_detalle.html",
            jugador=jugador,
            predicciones=predicciones,
            ranking=ranking,
            total_jugadores=total_jugadores,
            pred_campeon=pc,
            equipos_eliminados=equipos_eliminados,
        )

    return app


app = crear_app()

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
