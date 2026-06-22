"""Modelos SQLAlchemy: Jugador, Partido, Prediccion.

Tambien define las jornadas/etapas del Mundial 2026 (48 equipos, 12 grupos)
y su orden de presentacion, ya que la fase de eliminacion directa incluye
una ronda nueva (dieciseisavos) que no existia en mundiales de 32 equipos.
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Claves internas canonicas de jornada, en orden de torneo.
JORNADA_KEYS = [
    "jornada_1",
    "jornada_2",
    "jornada_3",
    "dieciseisavos",
    "octavos",
    "cuartos",
    "semifinal",
    "tercer_lugar",
    "final",
]

# Etiquetas legibles en espanol para mostrar en la UI.
JORNADA_LABELS = {
    "jornada_1": "Jornada 1",
    "jornada_2": "Jornada 2",
    "jornada_3": "Jornada 3",
    "dieciseisavos": "Dieciseisavos de final",
    "octavos": "Octavos de final",
    "cuartos": "Cuartos de final",
    "semifinal": "Semifinales",
    "tercer_lugar": "Tercer lugar",
    "final": "Final",
}

# Orden de torneo (no alfabetico) para ordenar listados/menus.
JORNADA_ORDEN = {clave: indice for indice, clave in enumerate(JORNADA_KEYS)}


def jornada_label(clave: str) -> str:
    """Devuelve la etiqueta legible de una jornada, o la clave si no se conoce."""
    return JORNADA_LABELS.get(clave, clave)


def jornadas_ordenadas():
    """Devuelve la lista de claves de jornada en orden de torneo."""
    return list(JORNADA_KEYS)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Jugador(db.Model):
    __tablename__ = "jugador"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False, unique=True)
    creado_en = db.Column(db.DateTime, default=_utcnow, nullable=False)

    predicciones = db.relationship(
        "Prediccion", back_populates="jugador", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Jugador {self.nombre}>"


class Partido(db.Model):
    __tablename__ = "partido"

    id = db.Column(db.Integer, primary_key=True)
    jornada = db.Column(db.String(32), nullable=False, index=True)
    equipo_local = db.Column(db.String(120), nullable=False)
    equipo_visitante = db.Column(db.String(120), nullable=False)
    fecha = db.Column(db.DateTime, nullable=True)
    marcador_local = db.Column(db.Integer, nullable=True)
    marcador_visitante = db.Column(db.Integer, nullable=True)
    external_id = db.Column(db.Integer, nullable=True, unique=True)
    creado_en = db.Column(db.DateTime, default=_utcnow, nullable=False)

    predicciones = db.relationship(
        "Prediccion", back_populates="partido", cascade="all, delete-orphan"
    )

    @property
    def tiene_resultado(self) -> bool:
        return self.marcador_local is not None and self.marcador_visitante is not None

    @property
    def jornada_label(self) -> str:
        return jornada_label(self.jornada)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Partido {self.equipo_local} vs {self.equipo_visitante} ({self.jornada})>"


class Prediccion(db.Model):
    __tablename__ = "prediccion"
    __table_args__ = (
        db.UniqueConstraint("jugador_id", "partido_id", name="uq_jugador_partido"),
    )

    id = db.Column(db.Integer, primary_key=True)
    jugador_id = db.Column(db.Integer, db.ForeignKey("jugador.id"), nullable=False)
    partido_id = db.Column(db.Integer, db.ForeignKey("partido.id"), nullable=False)
    pred_local = db.Column(db.Integer, nullable=False)
    pred_visitante = db.Column(db.Integer, nullable=False)
    puntos = db.Column(db.Integer, nullable=True)

    jugador = db.relationship("Jugador", back_populates="predicciones")
    partido = db.relationship("Partido", back_populates="predicciones")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Prediccion jugador={self.jugador_id} partido={self.partido_id}>"
