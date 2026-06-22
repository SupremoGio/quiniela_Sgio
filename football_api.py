"""Cliente minimo para la API de football-data.org (Mundial 2026, codigo WC)."""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org/v4"
COMPETITION_CODE = "WC"

# Mapeo del campo "stage" (y matchday cuando aplica) de la API a nuestras
# claves internas de jornada.
_STAGE_MAP = {
    "LAST_32": "dieciseisavos",
    "ROUND_OF_32": "dieciseisavos",
    "LAST_16": "octavos",
    "ROUND_OF_16": "octavos",
    "QUARTER_FINALS": "cuartos",
    "QUARTERFINALS": "cuartos",
    "SEMI_FINALS": "semifinal",
    "SEMIFINALS": "semifinal",
    "THIRD_PLACE": "tercer_lugar",
    "THIRD_PLACE_FINAL": "tercer_lugar",
    "FINAL": "final",
}

_GROUP_STAGE_VALUES = {"GROUP_STAGE", "GROUP STAGE"}


def _api_key() -> str:
    key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    if not key:
        logger.warning("FOOTBALL_DATA_API_KEY no esta configurada en el entorno.")
    return key


def _headers() -> dict:
    return {"X-Auth-Token": _api_key()}


def _get(path: str, params: dict | None = None, max_retries: int = 3) -> dict | None:
    """GET defensivo: maneja errores HTTP y rate limiting sin tronar el proceso."""
    url = f"{BASE_URL}{path}"
    for intento in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=15)
        except requests.RequestException as exc:
            logger.warning("Error de red consultando %s: %s", url, exc)
            return None

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429:
            espera = int(resp.headers.get("Retry-After", 10))
            logger.warning(
                "Rate limit de football-data.org alcanzado, esperando %s s (intento %s/%s)",
                espera, intento, max_retries,
            )
            time.sleep(espera)
            continue

        logger.warning(
            "football-data.org respondio %s para %s: %s", resp.status_code, url, resp.text[:300]
        )
        return None

    logger.warning("Se agotaron los reintentos para %s", url)
    return None


def fetch_wc_matches(params: dict | None = None) -> list[dict]:
    """Trae todos los partidos del Mundial (codigo WC). Filtros opcionales via params."""
    data = _get(f"/competitions/{COMPETITION_CODE}/matches", params=params)
    if not data:
        return []
    return data.get("matches", [])


def fetch_finished_matches(
    date_from: str | None = None,
    date_to: str | None = None,
    matchday: int | None = None,
) -> list[dict]:
    """Trae partidos finalizados, filtrando opcionalmente por rango de fechas o jornada."""
    params: dict[str, Any] = {"status": "FINISHED"}
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to
    if matchday is not None:
        params["matchday"] = matchday
    return fetch_wc_matches(params=params)


def map_stage_to_jornada(stage: str | None, matchday: int | None) -> str | None:
    """Mapea (stage, matchday) de la API a una clave interna de jornada.

    Devuelve None y registra un warning si el valor es inesperado, en vez de
    tronar todo el proceso de sincronizacion.
    """
    if not stage:
        logger.warning("Partido sin stage, no se puede mapear a jornada.")
        return None

    stage_norm = stage.upper().strip()

    if stage_norm in _GROUP_STAGE_VALUES:
        if matchday in (1, 2, 3):
            return f"jornada_{matchday}"
        logger.warning("Group stage con matchday inesperado: %r", matchday)
        return None

    jornada = _STAGE_MAP.get(stage_norm)
    if jornada is None:
        logger.warning("Stage desconocido de football-data.org: %r", stage)
    return jornada


def match_to_partido_dict(match: dict) -> dict | None:
    """Convierte un dict de partido de la API al formato usado internamente.

    Devuelve None (y registra warning) si no se puede mapear de forma segura.
    """
    try:
        stage = match.get("stage")
        matchday = match.get("matchday")
        jornada = map_stage_to_jornada(stage, matchday)
        if jornada is None:
            return None

        home = match.get("homeTeam") or {}
        away = match.get("awayTeam") or {}
        score = (match.get("score") or {}).get("fullTime") or {}

        return {
            "external_id": match.get("id"),
            "jornada": jornada,
            "equipo_local": home.get("name") or "Por definir",
            "equipo_visitante": away.get("name") or "Por definir",
            "fecha": match.get("utcDate"),
            "marcador_local": score.get("home"),
            "marcador_visitante": score.get("away"),
            "status": match.get("status"),
        }
    except Exception:  # pragma: no cover - defensivo, no debe tronar el sync
        logger.exception("No se pudo convertir el partido de la API: %r", match)
        return None


def matches_to_partido_dicts(matches: Iterable[dict]) -> list[dict]:
    """Convierte una lista de partidos de la API, saltando los que fallen."""
    resultado = []
    for match in matches:
        convertido = match_to_partido_dict(match)
        if convertido is not None:
            resultado.append(convertido)
    return resultado
