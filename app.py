"""App Flask de la Quiniela Mundial 2026: rutas + comandos CLI."""
from __future__ import annotations

import os
import unicodedata
from datetime import datetime

import click
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for

import football_api
from models import (
    JORNADA_KEYS,
    Jugador,
    Partido,
    Prediccion,
    db,
    jornada_label,
    jornadas_ordenadas,
)
from scoring import calcular_puntos

load_dotenv()


def normalizar(texto: str) -> str:
    """Normaliza texto para comparaciones sin distinguir mayusculas/acentos."""
    if texto is None:
        return ""
    texto = str(texto).strip().lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return texto


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "dev-secret-key"

    instance_dir = os.path.join(app.root_path, "instance")
    os.makedirs(instance_dir, exist_ok=True)

    db_uri = os.environ.get("SQLALCHEMY_DATABASE_URI", "sqlite:///instance/quiniela.db")
    if db_uri.startswith("sqlite:///") and not db_uri.startswith("sqlite:////"):
        # Resolver la ruta relativa contra el directorio del proyecto, no el cwd.
        rel_path = db_uri[len("sqlite:///"):]
        abs_path = os.path.join(app.root_path, rel_path)
        db_uri = "sqlite:///" + abs_path

    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    register_routes(app)
    register_cli(app)

    app.jinja_env.globals["jornada_label"] = jornada_label
    app.jinja_env.globals["jornadas_ordenadas"] = jornadas_ordenadas

    return app


# ---------------------------------------------------------------------------
# Logica compartida entre rutas y comandos CLI
# ---------------------------------------------------------------------------

def _recalcular_puntos_de_partido(partido: Partido) -> None:
    """Recalcula Prediccion.puntos para todas las predicciones de un partido."""
    if not partido.tiene_resultado:
        return
    for pred in partido.predicciones:
        pred.puntos = calcular_puntos(
            pred.pred_local,
            pred.pred_visitante,
            partido.marcador_local,
            partido.marcador_visitante,
        )


def _sync_resultados(jornada: str | None = None) -> int:
    """Trae resultados finalizados de football-data.org y recalcula puntos.

    Devuelve el numero de partidos actualizados con marcador nuevo.
    """
    query = Partido.query
    if jornada:
        query = query.filter_by(jornada=jornada)
    partidos = {p.external_id: p for p in query.all() if p.external_id is not None}

    if not partidos:
        return 0

    matches = football_api.fetch_finished_matches()
    actualizados = 0

    for match in matches:
        convertido = football_api.match_to_partido_dict(match)
        if convertido is None:
            continue
        external_id = convertido["external_id"]
        partido = partidos.get(external_id)
        if partido is None:
            continue
        if jornada and partido.jornada != jornada:
            continue
        if convertido["marcador_local"] is None or convertido["marcador_visitante"] is None:
            continue

        partido.marcador_local = convertido["marcador_local"]
        partido.marcador_visitante = convertido["marcador_visitante"]
        _recalcular_puntos_de_partido(partido)
        actualizados += 1

    db.session.commit()
    return actualizados


def _sync_fixtures() -> tuple[int, int]:
    """Trae el calendario completo del Mundial y hace upsert en Partido.

    No sobreescribe marcadores reales ya existentes. Devuelve (creados, actualizados).
    """
    matches = football_api.fetch_wc_matches()
    convertidos = football_api.matches_to_partido_dicts(matches)

    creados = 0
    actualizados = 0

    for datos in convertidos:
        external_id = datos["external_id"]
        partido = None
        if external_id is not None:
            partido = Partido.query.filter_by(external_id=external_id).first()

        fecha = None
        if datos.get("fecha"):
            try:
                fecha = datetime.fromisoformat(datos["fecha"].replace("Z", "+00:00"))
            except ValueError:
                fecha = None

        if partido is None:
            partido = Partido(
                jornada=datos["jornada"],
                equipo_local=datos["equipo_local"],
                equipo_visitante=datos["equipo_visitante"],
                fecha=fecha,
                external_id=external_id,
            )
            # Solo asignamos marcador si la API ya lo trae (partido finalizado).
            if datos["marcador_local"] is not None and datos["marcador_visitante"] is not None:
                partido.marcador_local = datos["marcador_local"]
                partido.marcador_visitante = datos["marcador_visitante"]
            db.session.add(partido)
            creados += 1
        else:
            partido.jornada = datos["jornada"]
            partido.equipo_local = datos["equipo_local"]
            partido.equipo_visitante = datos["equipo_visitante"]
            partido.fecha = fecha
            # No sobreescribir un marcador real ya guardado.
            if partido.marcador_local is None and partido.marcador_visitante is None:
                if datos["marcador_local"] is not None and datos["marcador_visitante"] is not None:
                    partido.marcador_local = datos["marcador_local"]
                    partido.marcador_visitante = datos["marcador_visitante"]
            actualizados += 1

    db.session.commit()
    return creados, actualizados


def _procesar_excel_predicciones(df: pd.DataFrame) -> tuple[int, int]:
    """Procesa un DataFrame de predicciones (ya con columnas normalizadas).

    Crea Jugador/Partido si no existen y hace upsert de Prediccion.
    Devuelve (filas_procesadas, errores).
    """
    columnas_requeridas = {
        "jornada", "jugador", "equipo_local", "equipo_visitante", "pred_local", "pred_visitante"
    }
    columnas_actuales = {normalizar(c) for c in df.columns}
    faltantes = columnas_requeridas - columnas_actuales
    if faltantes:
        raise ValueError(f"Faltan columnas requeridas: {', '.join(sorted(faltantes))}")

    # Normalizar nombres de columnas a las claves canonicas.
    mapa_columnas = {}
    for col in df.columns:
        norm = normalizar(col)
        if norm in columnas_requeridas:
            mapa_columnas[col] = norm
    df = df.rename(columns=mapa_columnas)

    # Indices en memoria para evitar relecturas constantes.
    jugadores_cache: dict[str, Jugador] = {
        normalizar(j.nombre): j for j in Jugador.query.all()
    }
    partidos_cache: dict[tuple[str, str, str], Partido] = {}
    for p in Partido.query.all():
        clave = (p.jornada, normalizar(p.equipo_local), normalizar(p.equipo_visitante))
        partidos_cache[clave] = p

    procesadas = 0
    errores = 0

    for _, fila in df.iterrows():
        try:
            jornada_raw = str(fila["jornada"]).strip()
            jornada = _resolver_jornada(jornada_raw)
            jugador_nombre = str(fila["jugador"]).strip()
            equipo_local = str(fila["equipo_local"]).strip()
            equipo_visitante = str(fila["equipo_visitante"]).strip()
            pred_local = int(fila["pred_local"])
            pred_visitante = int(fila["pred_visitante"])

            clave_jugador = normalizar(jugador_nombre)
            jugador = jugadores_cache.get(clave_jugador)
            if jugador is None:
                jugador = Jugador(nombre=jugador_nombre)
                db.session.add(jugador)
                db.session.flush()
                jugadores_cache[clave_jugador] = jugador

            clave_partido = (jornada, normalizar(equipo_local), normalizar(equipo_visitante))
            partido = partidos_cache.get(clave_partido)
            if partido is None:
                partido = Partido(
                    jornada=jornada,
                    equipo_local=equipo_local,
                    equipo_visitante=equipo_visitante,
                )
                db.session.add(partido)
                db.session.flush()
                partidos_cache[clave_partido] = partido

            prediccion = Prediccion.query.filter_by(
                jugador_id=jugador.id, partido_id=partido.id
            ).first()
            if prediccion is None:
                prediccion = Prediccion(
                    jugador_id=jugador.id,
                    partido_id=partido.id,
                    pred_local=pred_local,
                    pred_visitante=pred_visitante,
                )
                db.session.add(prediccion)
            else:
                prediccion.pred_local = pred_local
                prediccion.pred_visitante = pred_visitante

            if partido.tiene_resultado:
                prediccion.puntos = calcular_puntos(
                    pred_local, pred_visitante, partido.marcador_local, partido.marcador_visitante
                )

            procesadas += 1
        except Exception:  # noqa: BLE001 - seguimos con las demas filas
            errores += 1

    db.session.commit()
    return procesadas, errores


def _resolver_jornada(valor: str) -> str:
    """Acepta tanto las claves canonicas ('jornada_1') como numeros sueltos ('1')."""
    valor_norm = normalizar(valor)
    if valor_norm in JORNADA_KEYS:
        return valor_norm
    if valor_norm in {"1", "2", "3"}:
        return f"jornada_{valor_norm}"
    # Intentar match parcial contra etiquetas conocidas.
    for clave in JORNADA_KEYS:
        if normalizar(clave) == valor_norm or normalizar(jornada_label(clave)) == valor_norm:
            return clave
    # Si no se reconoce, se usa tal cual (permite jornadas ad-hoc).
    return valor_norm


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

def register_routes(app: Flask) -> None:
    @app.route("/")
    def index():
        jugadores = Jugador.query.order_by(Jugador.nombre).all()
        tabla = []
        for jugador in jugadores:
            total = sum(p.puntos or 0 for p in jugador.predicciones)
            tabla.append({"jugador": jugador, "puntos": total})
        tabla.sort(key=lambda fila: fila["puntos"], reverse=True)
        return render_template("index.html", tabla=tabla)

    @app.route("/jornada/<jornada>")
    def ver_jornada(jornada: str):
        partidos = (
            Partido.query.filter_by(jornada=jornada)
            .order_by(Partido.fecha.is_(None), Partido.fecha)
            .all()
        )
        jugadores = Jugador.query.order_by(Jugador.nombre).all()

        # predicciones[partido_id][jugador_id] = Prediccion
        predicciones: dict[int, dict[int, Prediccion]] = {p.id: {} for p in partidos}
        for partido in partidos:
            for pred in partido.predicciones:
                predicciones[partido.id][pred.jugador_id] = pred

        return render_template(
            "jornada.html",
            jornada=jornada,
            partidos=partidos,
            jugadores=jugadores,
            predicciones=predicciones,
        )

    @app.route("/jornada/<jornada>/actualizar", methods=["POST"])
    def actualizar_jornada(jornada: str):
        try:
            actualizados = _sync_resultados(jornada=jornada)
            flash(f"Se actualizaron {actualizados} partido(s).", "success")
        except Exception as exc:  # noqa: BLE001
            flash(f"Error al actualizar resultados: {exc}", "danger")
        return redirect(url_for("ver_jornada", jornada=jornada))

    @app.route("/jugadores", methods=["GET", "POST"])
    def jugadores():
        if request.method == "POST":
            nombre = (request.form.get("nombre") or "").strip()
            if not nombre:
                flash("El nombre no puede estar vacio.", "danger")
            elif Jugador.query.filter_by(nombre=nombre).first():
                flash("Ese jugador ya existe.", "warning")
            else:
                db.session.add(Jugador(nombre=nombre))
                db.session.commit()
                flash(f"Jugador '{nombre}' agregado.", "success")
            return redirect(url_for("jugadores"))

        lista = Jugador.query.order_by(Jugador.nombre).all()
        return render_template("jugadores.html", jugadores=lista)

    @app.route("/jugadores/<int:jugador_id>/eliminar", methods=["POST"])
    def eliminar_jugador(jugador_id: int):
        jugador = Jugador.query.get_or_404(jugador_id)
        db.session.delete(jugador)
        db.session.commit()
        flash(f"Jugador '{jugador.nombre}' eliminado.", "success")
        return redirect(url_for("jugadores"))

    @app.route("/predicciones/subir", methods=["GET", "POST"])
    def subir_predicciones():
        if request.method == "POST":
            archivo = request.files.get("archivo")
            if not archivo or archivo.filename == "":
                flash("Selecciona un archivo.", "danger")
                return redirect(url_for("subir_predicciones"))

            try:
                if archivo.filename.lower().endswith(".csv"):
                    df = pd.read_csv(archivo)
                else:
                    df = pd.read_excel(archivo)
                procesadas, errores = _procesar_excel_predicciones(df)
                mensaje = f"Se procesaron {procesadas} predicciones."
                if errores:
                    mensaje += f" ({errores} filas con errores fueron omitidas.)"
                flash(mensaje, "success" if errores == 0 else "warning")
            except Exception as exc:  # noqa: BLE001
                flash(f"Error al procesar el archivo: {exc}", "danger")

            return redirect(url_for("subir_predicciones"))

        return render_template("subir_predicciones.html")


# ---------------------------------------------------------------------------
# Comandos CLI
# ---------------------------------------------------------------------------

def register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db():
        """Crea todas las tablas de la base de datos."""
        db.create_all()
        click.echo("Base de datos inicializada.")

    @app.cli.command("sync-fixtures")
    def sync_fixtures():
        """Trae el calendario completo del Mundial 2026 desde football-data.org."""
        creados, actualizados = _sync_fixtures()
        click.echo(f"Fixtures sincronizados: {creados} creados, {actualizados} actualizados.")

    @app.cli.command("sync-resultados")
    @click.argument("jornada", required=False)
    def sync_resultados(jornada: str | None):
        """Trae resultados reales y recalcula puntos para una jornada (o todas)."""
        clave_jornada = _resolver_jornada(jornada) if jornada else None
        actualizados = _sync_resultados(jornada=clave_jornada)
        objetivo = jornada_label(clave_jornada) if clave_jornada else "todas las jornadas"
        click.echo(f"Resultados sincronizados para {objetivo}: {actualizados} partido(s) actualizados.")


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
