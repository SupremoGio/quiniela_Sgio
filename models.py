"""
Modelos de base de datos para la Quiniela Mundial 2026.

- Jugador: cada uno de los participantes de la quiniela.
- Partido: un partido del torneo (jornada/matchday, equipos, marcador real).
- Prediccion: el marcador que un Jugador predijo para un Partido, y los
  puntos que obtuvo una vez que el partido termina.
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Jugador(db.Model):
    __tablename__ = "jugadores"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), unique=True, nullable=False)
    pais = db.Column(db.String(2), nullable=True)  # ISO-3166-1 alpha-2 (ej. "MX")
    creado_en = db.Column(db.DateTime, default=datetime.utcnow)

    predicciones = db.relationship(
        "Prediccion", back_populates="jugador", cascade="all, delete-orphan"
    )

    @property
    def puntos_totales(self):
        return sum(p.puntos or 0 for p in self.predicciones)

    @property
    def aciertos_exactos(self):
        return sum(1 for p in self.predicciones if p.puntos == 3)

    @property
    def aciertos_resultado(self):
        return sum(1 for p in self.predicciones if p.puntos == 1)

    def __repr__(self):
        return f"<Jugador {self.nombre}>"


class Partido(db.Model):
    __tablename__ = "partidos"

    id = db.Column(db.Integer, primary_key=True)
    jornada = db.Column(db.Integer, nullable=False, index=True)  # matchday
    fecha = db.Column(db.DateTime, nullable=True)
    equipo_local = db.Column(db.String(80), nullable=False)
    equipo_visitante = db.Column(db.String(80), nullable=False)

    marcador_local = db.Column(db.Integer, nullable=True)
    marcador_visitante = db.Column(db.Integer, nullable=True)
    finalizado = db.Column(db.Boolean, default=False)

    # id del partido en football-data.org, para poder sincronizar resultados
    api_match_id = db.Column(db.Integer, nullable=True, unique=True)

    predicciones = db.relationship(
        "Prediccion", back_populates="partido", cascade="all, delete-orphan"
    )

    __table_args__ = (
        db.UniqueConstraint(
            "jornada", "equipo_local", "equipo_visitante", name="uq_partido_jornada_equipos"
        ),
    )

    @property
    def resultado_real(self):
        """Devuelve 'L' (local gana), 'V' (visitante gana) o 'E' (empate)."""
        if self.marcador_local is None or self.marcador_visitante is None:
            return None
        if self.marcador_local > self.marcador_visitante:
            return "L"
        if self.marcador_local < self.marcador_visitante:
            return "V"
        return "E"

    def __repr__(self):
        return f"<Partido J{self.jornada}: {self.equipo_local} vs {self.equipo_visitante}>"


class PrediccionCampeon(db.Model):
    __tablename__ = "predicciones_campeon"

    id = db.Column(db.Integer, primary_key=True)
    jugador_id = db.Column(db.Integer, db.ForeignKey("jugadores.id"), nullable=False, unique=True)
    equipo = db.Column(db.String(80), nullable=False)
    puntos = db.Column(db.Integer, nullable=True)  # 5 si acertó, 0 si no, None si aún no se sabe

    jugador = db.relationship(
        "Jugador",
        backref=db.backref("prediccion_campeon", uselist=False, cascade="all, delete-orphan"),
    )

    def __repr__(self):
        return f"<PrediccionCampeon {self.jugador_id}: {self.equipo}>"


class SnapshotPosicion(db.Model):
    __tablename__ = "snapshots_posicion"

    id = db.Column(db.Integer, primary_key=True)
    jugador_id = db.Column(db.Integer, db.ForeignKey("jugadores.id"), nullable=False)
    posicion = db.Column(db.Integer, nullable=False)
    puntos = db.Column(db.Integer, nullable=False, default=0)
    tomado_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    jugador = db.relationship(
        "Jugador", backref=db.backref("snapshots_posicion", cascade="all, delete-orphan")
    )


class ConfigApp(db.Model):
    __tablename__ = "config_app"

    clave = db.Column(db.String(80), primary_key=True)
    valor = db.Column(db.String(200), nullable=True)


class Prediccion(db.Model):
    __tablename__ = "predicciones"

    id = db.Column(db.Integer, primary_key=True)
    jugador_id = db.Column(db.Integer, db.ForeignKey("jugadores.id"), nullable=False)
    partido_id = db.Column(db.Integer, db.ForeignKey("partidos.id"), nullable=False)

    pred_local = db.Column(db.Integer, nullable=False)
    pred_visitante = db.Column(db.Integer, nullable=False)
    puntos = db.Column(db.Integer, nullable=True)  # se calcula al terminar el partido

    jugador = db.relationship("Jugador", back_populates="predicciones")
    partido = db.relationship("Partido", back_populates="predicciones")

    __table_args__ = (
        db.UniqueConstraint("jugador_id", "partido_id", name="uq_prediccion_jugador_partido"),
    )

    @property
    def resultado_predicho(self):
        if self.pred_local > self.pred_visitante:
            return "L"
        if self.pred_local < self.pred_visitante:
            return "V"
        return "E"

    def __repr__(self):
        return f"<Prediccion {self.jugador_id}-{self.partido_id}: {self.pred_local}-{self.pred_visitante}>"
