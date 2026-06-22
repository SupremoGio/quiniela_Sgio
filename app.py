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

import click
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for

import football_api
from models import Jugador, Partido, Prediccion, db
from scoring import calcular_puntos

load_dotenv()


def _buscar_jugador_insensible(nombre_jugador):
    """SQLite's LOWER() no convierte tildes/acentos, así que comparamos en
    Python para que la búsqueda sea insensible a mayúsculas y acentos."""
    nombre_norm = nombre_jugador.strip().lower()
    for j in Jugador.query.all():
        if j.nombre.strip().lower() == nombre_norm:
            return j
    return None


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
    columnas_equipos = columnas[2:]  # se descarta la 2da columna (ej. 'CAMPEON')
    if len(columnas_equipos) % 2 != 0:
        columnas_equipos = columnas_equipos[:-1]

    pares = list(zip(columnas_equipos[0::2], columnas_equipos[1::2]))

    filas_ok, filas_error = 0, 0
    for _, fila in df.iterrows():
        nombre_jugador = fila[col_jugador]
        if pd.isna(nombre_jugador) or not str(nombre_jugador).strip():
            continue
        nombre_jugador = str(nombre_jugador).strip()

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
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(app.instance_path, "quiniela.db")
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB para el excel

    os.makedirs(app.instance_path, exist_ok=True)
    db.init_app(app)

    # ---------- helpers internos ----------
    def _sincronizar_calendario():
        """Trae el calendario completo (104 partidos) desde la API y
        crea/actualiza los registros de Partido. Si la API ya marca un
        partido como FINISHED, guarda también el marcador."""
        partidos_api = football_api.obtener_partidos()
        creados, actualizados = 0, 0
        for p in partidos_api:
            datos = football_api.extraer_resultado(p)
            partido = Partido.query.filter_by(api_match_id=datos["api_match_id"]).first()
            if partido is None:
                partido = Partido.query.filter_by(
                    jornada=datos["jornada"],
                    equipo_local=datos["equipo_local"],
                    equipo_visitante=datos["equipo_visitante"],
                ).first()

            if partido is None:
                partido = Partido(
                    jornada=datos["jornada"],
                    equipo_local=datos["equipo_local"],
                    equipo_visitante=datos["equipo_visitante"],
                )
                db.session.add(partido)
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

        for p in partidos_api:
            datos = football_api.extraer_resultado(p)
            if datos["estado"] != "FINISHED":
                continue

            partido = Partido.query.filter_by(api_match_id=datos["api_match_id"]).first()
            if partido is None:
                partido = Partido.query.filter_by(
                    jornada=datos["jornada"],
                    equipo_local=datos["equipo_local"],
                    equipo_visitante=datos["equipo_visitante"],
                ).first()
            if partido is None:
                partido = Partido(
                    jornada=datos["jornada"],
                    equipo_local=datos["equipo_local"],
                    equipo_visitante=datos["equipo_visitante"],
                )
                db.session.add(partido)

            partido.api_match_id = datos["api_match_id"]
            partido.marcador_local = datos["marcador_local"]
            partido.marcador_visitante = datos["marcador_visitante"]
            partido.finalizado = True
            partidos_actualizados += 1

            for pred in partido.predicciones:
                pred.puntos = calcular_puntos(
                    pred.pred_local,
                    pred.pred_visitante,
                    partido.marcador_local,
                    partido.marcador_visitante,
                )
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
    @app.route("/")
    def index():
        jugadores = Jugador.query.all()
        tabla = sorted(jugadores, key=lambda j: j.puntos_totales, reverse=True)
        jornadas = sorted({p.jornada for p in Partido.query.all()})
        return render_template("index.html", tabla=tabla, jornadas=jornadas)

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
            if nombre:
                existe = _buscar_jugador_insensible(nombre)
                if existe:
                    flash("Ese jugador ya existe.", "error")
                else:
                    db.session.add(Jugador(nombre=nombre))
                    db.session.commit()
                    flash(f"Jugador '{nombre}' agregado.", "success")
            return redirect(url_for("jugadores"))

        lista = Jugador.query.order_by(Jugador.nombre).all()
        return render_template("jugadores.html", jugadores=lista)

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
        return render_template("jugador_detalle.html", jugador=jugador, predicciones=predicciones)

    return app


app = crear_app()

if __name__ == "__main__":
    app.run(debug=True)
