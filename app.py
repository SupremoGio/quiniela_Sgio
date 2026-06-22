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
from datetime import datetime

import click
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for

import football_api
from models import Jugador, Partido, Prediccion, db
from scoring import calcular_puntos

load_dotenv()


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

            try:
                if archivo.filename.lower().endswith(".csv"):
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
            df.columns = [c.strip().lower() for c in df.columns]
            faltantes = columnas_esperadas - set(df.columns)
            if faltantes:
                flash("Al archivo le faltan estas columnas: " + ", ".join(sorted(faltantes)), "error")
                return redirect(url_for("upload"))

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

                jugador = Jugador.query.filter(
                    db.func.lower(Jugador.nombre) == nombre_jugador.lower()
                ).first()
                if jugador is None:
                    jugador = Jugador(nombre=nombre_jugador)
                    db.session.add(jugador)
                    db.session.flush()

                partido = Partido.query.filter(
                    Partido.jornada == jornada,
                    db.func.lower(Partido.equipo_local) == eq_local.lower(),
                    db.func.lower(Partido.equipo_visitante) == eq_visitante.lower(),
                ).first()
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
                filas_ok += 1

            db.session.commit()
            flash(f"Importadas {filas_ok} predicciones. {filas_error} fila(s) con error.", "success")
            return redirect(url_for("index"))

        return render_template("upload.html")

    @app.route("/jugadores", methods=["GET", "POST"])
    def jugadores():
        if request.method == "POST":
            nombre = request.form.get("nombre", "").strip()
            if nombre:
                existe = Jugador.query.filter(db.func.lower(Jugador.nombre) == nombre.lower()).first()
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
