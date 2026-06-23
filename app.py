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
import os
import re
from datetime import datetime
from difflib import SequenceMatcher
from itertools import combinations
from zoneinfo import ZoneInfo

import click
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for

import football_api
from models import ConfigApp, Jugador, Partido, Prediccion, PrediccionCampeon, db
from scoring import calcular_puntos

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def _buscar_jugador_insensible(nombre_jugador):
    """SQLite's LOWER() no convierte tildes/acentos, así que comparamos en
    Python para que la búsqueda sea insensible a mayúsculas y acentos."""
    nombre_norm = nombre_jugador.strip().lower()
    for j in Jugador.query.all():
        if j.nombre.strip().lower() == nombre_norm:
            return j
    return None


def _posibles_duplicados(jugadores, umbral=0.6):
    """Detecta pares de jugadores cuyo nombre es muy parecido (typos, alias)
    usando similitud de cadenas, para sugerir una fusión sin tener que
    detectarlos a ojo (ej. 'Don Toño' / 'Dontoño', 'Osvaldo' / 'Osvalo')."""
    sugerencias = []
    for a, b in combinations(jugadores, 2):
        na = a.nombre.strip().lower().replace(" ", "")
        nb = b.nombre.strip().lower().replace(" ", "")
        ratio = SequenceMatcher(None, na, nb).ratio()
        if ratio >= umbral:
            sugerencias.append((a, b, ratio))
    sugerencias.sort(key=lambda s: s[2], reverse=True)
    return sugerencias


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
    eq_local_norm = eq_local.strip().lower()
    eq_visitante_norm = eq_visitante.strip().lower()
    for p in Partido.query.filter_by(jornada=jornada).all():
        if p.equipo_local.strip().lower() == eq_local_norm and p.equipo_visitante.strip().lower() == eq_visitante_norm:
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
    with app.app_context():
        db.create_all()
        # Migración: agrega columnas nuevas si no existen (compatible SQLite + PG)
        with db.engine.connect() as conn:
            for ddl in [
                "ALTER TABLE jugadores ADD COLUMN pais VARCHAR(2)",
                "ALTER TABLE predicciones_campeon ADD COLUMN puntos INTEGER",
            ]:
                try:
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

    # ---------- helpers internos ----------
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
                partido.marcador_local = datos["marcador_local"]
                partido.marcador_visitante = datos["marcador_visitante"]
                partido.finalizado = True

        db.session.commit()
        return creados, actualizados

    def _sincronizar_resultados(jornada=None):
        """Actualiza marcadores reales de partidos FINISHED y recalcula
        los puntos de las predicciones asociadas."""
        partidos_api = football_api.obtener_partidos(jornada=jornada)
        partidos_actualizados = 0
        predicciones_calificadas = 0

        todos_en_db = Partido.query.all()
        for p in partidos_api:
            datos = football_api.extraer_resultado(p)
            if datos["estado"] != "FINISHED":
                continue
            if not datos["jornada"] or not datos["equipo_local"] or not datos["equipo_visitante"]:
                continue

            ml = datos["marcador_local"]
            mv = datos["marcador_visitante"]

            # Buscar por api_match_id (partido principal de la API)
            por_api_id = Partido.query.filter_by(api_match_id=datos["api_match_id"]).first()

            # Buscar todos los que coincidan por nombre/alias en CUALQUIER jornada
            por_alias = {}
            for p_db in todos_en_db:
                local_ok = football_api.equipos_coinciden(p_db.equipo_local, datos["equipo_local"])
                visit_ok = football_api.equipos_coinciden(p_db.equipo_visitante, datos["equipo_visitante"])
                if local_ok and visit_ok:
                    por_alias[p_db.id] = p_db

            if not por_api_id and not por_alias:
                continue  # partido no registrado en DB, ignorar

            # Actualizar partido principal (con api_match_id)
            if por_api_id:
                por_api_id.api_match_id = datos["api_match_id"]
                por_api_id.marcador_local = ml
                por_api_id.marcador_visitante = mv
                por_api_id.finalizado = True
                partidos_actualizados += 1
                for pred in por_api_id.predicciones:
                    pred.puntos = calcular_puntos(pred.pred_local, pred.pred_visitante, ml, mv)
                    predicciones_calificadas += 1

            # Actualizar partidos con nombre alternativo (sin tocar api_match_id para evitar UNIQUE)
            for partido in por_alias.values():
                if por_api_id and partido.id == por_api_id.id:
                    continue
                partido.marcador_local = ml
                partido.marcador_visitante = mv
                partido.finalizado = True
                partidos_actualizados += 1
                for pred in partido.predicciones:
                    pred.puntos = calcular_puntos(pred.pred_local, pred.pred_visitante, ml, mv)
                    predicciones_calificadas += 1

        db.session.commit()
        return partidos_actualizados, predicciones_calificadas

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
        partidos_act, preds_calif = _sincronizar_resultados(jornada=jornada)
        print(f"{partidos_act} partido(s) actualizados, {preds_calif} predicción(es) calificadas.")

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
        return render_template("index.html", tabla=tabla, jornadas=jornadas,
                               partidos_por_jornada=partidos_por_jornada,
                               jornada_sel=jornada_sel, stats=stats,
                               preds_campeon=preds_campeon, campeon_real=campeon_real)

    @app.route("/campeon", methods=["POST"])
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

    @app.route("/jornada/<int:jornada>")
    def ver_jornada(jornada):
        partidos = Partido.query.filter_by(jornada=jornada).order_by(Partido.fecha).all()
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

    @app.route("/sync/<int:jornada>", methods=["POST"])
    def sync_jornada(jornada):
        try:
            partidos_act, preds_calif = _sincronizar_resultados(jornada=jornada)
            flash(
                f"Listo: {partidos_act} partido(s) actualizados, "
                f"{preds_calif} predicción(es) calificadas.",
                "success",
            )
        except football_api.FootballDataError as exc:
            flash(str(exc), "error")
        return redirect(url_for("ver_jornada", jornada=jornada))

    @app.route("/partido/<int:partido_id>/resultado", methods=["POST"])
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

    @app.route("/jornada/<int:jornada>/eliminar", methods=["POST"])
    def eliminar_jornada(jornada):
        partidos = Partido.query.filter_by(jornada=jornada).all()
        cantidad = len(partidos)
        for partido in partidos:
            db.session.delete(partido)  # cascade borra también sus predicciones
        db.session.commit()
        flash(f"Se eliminó la jornada {jornada}: {cantidad} partido(s) y sus predicciones.", "success")
        return redirect(url_for("index"))

    @app.route("/sync-calendario", methods=["POST"])
    def sync_calendario():
        try:
            creados, actualizados = _sincronizar_calendario()
            flash(f"Calendario sincronizado: {creados} creados, {actualizados} actualizados.", "success")
        except football_api.FootballDataError as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.route("/upload", methods=["GET", "POST"])
    def upload():
        if request.method == "POST":
            archivo = request.files.get("archivo")
            if not archivo or archivo.filename == "":
                flash("Selecciona un archivo .xlsx o .csv", "error")
                return redirect(url_for("upload"))

            es_csv = archivo.filename.lower().endswith(".csv")
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
        sugerencias = _posibles_duplicados(lista)
        return render_template("jugadores.html", jugadores=lista, sugerencias_fusion=sugerencias)

    @app.route("/jugadores/<int:jugador_id>/pais", methods=["POST"])
    def set_pais_jugador(jugador_id):
        jugador = Jugador.query.get_or_404(jugador_id)
        pais_raw = request.form.get("pais", "").strip().upper()
        jugador.pais = pais_raw[:2] if len(pais_raw) == 2 else None
        db.session.commit()
        return redirect(url_for("jugadores"))

    @app.route("/jugadores/<int:jugador_id>/fusionar", methods=["POST"])
    def fusionar_jugador(jugador_id):
        origen = Jugador.query.get_or_404(jugador_id)
        destino_id = request.form.get("jugador_destino_id", type=int)
        destino = Jugador.query.get_or_404(destino_id)
        movidas, omitidas = 0, 0
        for pred in list(origen.predicciones):
            ya_existe = Prediccion.query.filter_by(
                jugador_id=destino.id, partido_id=pred.partido_id
            ).first()
            if ya_existe is None:
                pred.jugador_id = destino.id
                movidas += 1
            elif (pred.puntos or 0) > (ya_existe.puntos or 0):
                # El origen acertó mejor ese partido: se queda la suya en vez
                # de la del destino, para no perder puntos correctos al fusionar.
                db.session.delete(ya_existe)
                pred.jugador_id = destino.id
                movidas += 1
            else:
                db.session.delete(pred)
                omitidas += 1

        pc_origen = origen.prediccion_campeon
        pc_destino = destino.prediccion_campeon
        if pc_origen is not None:
            if pc_destino is None:
                pc_origen.jugador_id = destino.id
            elif (pc_origen.puntos or 0) > (pc_destino.puntos or 0):
                db.session.delete(pc_destino)
                pc_origen.jugador_id = destino.id
            else:
                db.session.delete(pc_origen)

        db.session.flush()
        db.session.delete(origen)
        db.session.commit()
        flash(
            f"'{origen.nombre}' fusionado con '{destino.nombre}': "
            f"{movidas} predicción(es) movidas, {omitidas} omitida(s) por duplicado.",
            "success",
        )
        return redirect(url_for("jugadores"))

    @app.route("/jugadores/<int:jugador_id>/eliminar", methods=["POST"])
    def eliminar_jugador(jugador_id):
        jugador = Jugador.query.get_or_404(jugador_id)
        db.session.delete(jugador)
        db.session.commit()
        flash(f"Jugador '{jugador.nombre}' eliminado.", "success")
        return redirect(url_for("jugadores"))

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
        return render_template(
            "jugador_detalle.html",
            jugador=jugador,
            predicciones=predicciones,
            ranking=ranking,
            total_jugadores=total_jugadores,
            pred_campeon=pc,
        )

    return app


app = crear_app()

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
