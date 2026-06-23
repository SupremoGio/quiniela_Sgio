"""
Cliente para la API gratuita de football-data.org.

football-data.org ofrece, de forma gratuita y "para siempre" (según su
propio creador), los partidos, marcadores y calendario del Mundial
(competition code "WC"), con un límite de 10 llamadas por minuto en el
plan free. Es justo lo que necesitamos: no hace falta nada en vivo
minuto a minuto, solo saber cuándo un partido terminó y con qué marcador.

1. Crea una cuenta gratis en https://www.football-data.org/client/register
2. Copia tu token y ponlo en la variable de entorno FOOTBALL_DATA_API_KEY
   (puedes usar el archivo .env, ver .env.example)

Documentación oficial: https://www.football-data.org/documentation/api
"""
import os
import time
import unicodedata

import requests

BASE_URL = "https://api.football-data.org/v4"
COMPETITION_CODE = "WC"  # FIFA World Cup

# Nombres alternativos (español u otros) → nombres que puede devolver la API (normalizados)
_ALIAS_EQUIPO = {
    "chequia": ["czechia", "czech republic"],
    "republica checa": ["czechia", "czech republic"],
    "sudafrica": ["south africa"],
    "corea del sur": ["korea republic", "south korea"],
    "corea del norte": ["korea dpr", "north korea"],
    "arabia saudita": ["saudi arabia"],
    "estados unidos": ["united states", "usa"],
    "eeuu": ["united states", "usa"],
    "eua": ["united states", "usa"],
    "costa de marfil": ["cote d'ivoire", "ivory coast"],
    "iran": ["ir iran"],
    "nueva zelanda": ["new zealand"],
    "trinidad y tobago": ["trinidad and tobago"],
    "guinea ecuatorial": ["equatorial guinea"],
    "emiratos arabes unidos": ["united arab emirates"],
    "bosnia": ["bosnia and herzegovina"],
    "bosnia herzegovina": ["bosnia and herzegovina"],
    "bosnia & herzegovina": ["bosnia and herzegovina"],
    "cabo verde": ["cape verde", "cape verde islands"],
    "islas cabo verde": ["cape verde", "cape verde islands"],
    "guinea bissau": ["guinea-bissau"],
    "paises bajos": ["netherlands"],
    "holanda": ["netherlands"],
    "inglaterra": ["england"],
    "alemania": ["germany"],
    "espana": ["spain"],
    "francia": ["france"],
    "belgica": ["belgium"],
    "suecia": ["sweden"],
    "suiza": ["switzerland"],
    "noruega": ["norway"],
    "dinamarca": ["denmark"],
    "escocia": ["scotland"],
    "gales": ["wales"],
    "rumania": ["romania"],
    "hungria": ["hungary"],
    "polonia": ["poland"],
    "turquia": ["turkey", "turkiye"],
    "ucrania": ["ukraine"],
    "austria": ["austria"],
    "croacia": ["croatia"],
    "serbia": ["serbia"],
    "eslovenia": ["slovenia"],
    "eslovaquia": ["slovakia"],
    "grecia": ["greece"],
    "portugal": ["portugal"],
    "italia": ["italy"],
    "rusia": ["russia"],
    "marruecos": ["morocco"],
    "argelia": ["algeria"],
    "tunez": ["tunisia"],
    "egipto": ["egypt"],
    "nigeria": ["nigeria"],
    "ghana": ["ghana"],
    "camerun": ["cameroon"],
    "senegal": ["senegal"],
    "mali": ["mali"],
    "burkina faso": ["burkina faso"],
    "tanzania": ["tanzania"],
    "zambia": ["zambia"],
    "mozambique": ["mozambique"],
    "angola": ["angola"],
    "congo": ["congo"],
    "republica democratica del congo": ["dr congo", "congo dr"],
    "rd congo": ["dr congo", "congo dr"],
    "curazao": ["curacao", "curaçao"],
    "japon": ["japan"],
    "china": ["china pr"],
    "australia": ["australia"],
    "indonesia": ["indonesia"],
    "filipinas": ["philippines"],
    "tailandia": ["thailand"],
    "vietnam": ["vietnam"],
    "india": ["india"],
    "irak": ["iraq"],
    "jordania": ["jordan"],
    "siria": ["syria"],
    "libano": ["lebanon"],
    "palestina": ["palestine"],
    "uzbekistan": ["uzbekistan"],
    "kazajistan": ["kazakhstan"],
    "mexico": ["mexico"],
    "canada": ["canada"],
    "costa rica": ["costa rica"],
    "panama": ["panama"],
    "honduras": ["honduras"],
    "el salvador": ["el salvador"],
    "trinidad tobago": ["trinidad and tobago"],
    "jamaica": ["jamaica"],
    "cuba": ["cuba"],
    "haiti": ["haiti"],
    "argentina": ["argentina"],
    "brasil": ["brazil"],
    "colombia": ["colombia"],
    "chile": ["chile"],
    "uruguay": ["uruguay"],
    "peru": ["peru"],
    "venezuela": ["venezuela"],
    "ecuador": ["ecuador"],
    "paraguay": ["paraguay"],
    "bolivia": ["bolivia"],
}


class FootballDataError(Exception):
    pass


def _headers():
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not api_key:
        raise FootballDataError(
            "Falta configurar FOOTBALL_DATA_API_KEY en tu archivo .env. "
            "Crea una cuenta gratis en https://www.football-data.org/client/register"
        )
    return {"X-Auth-Token": api_key}


def _formas_canonicas(nombre):
    """Devuelve todas las formas normalizadas conocidas para un nombre de equipo."""
    norm = normalizar_nombre_equipo(nombre)
    formas = {norm}
    # Buscar en aliases directos
    if norm in _ALIAS_EQUIPO:
        formas |= {normalizar_nombre_equipo(a) for a in _ALIAS_EQUIPO[norm]}
    # Buscar si este nombre es un alias de otro
    for esp, engs in _ALIAS_EQUIPO.items():
        if norm in {normalizar_nombre_equipo(e) for e in engs}:
            formas.add(esp)
            formas |= {normalizar_nombre_equipo(e) for e in engs}
    return formas


def equipos_coinciden(nombre_a, nombre_b):
    """True si dos nombres se refieren al mismo equipo (tolerante a idioma/acentos)."""
    if _formas_canonicas(nombre_a) & _formas_canonicas(nombre_b):
        return True
    # Respaldo: un nombre es subcadena del otro (ej. "bosnia" dentro de "bosnia and herzegovina")
    na = normalizar_nombre_equipo(nombre_a)
    nb = normalizar_nombre_equipo(nombre_b)
    return na in nb or nb in na


def normalizar_nombre_equipo(nombre):
    """Normaliza nombres de equipo para poder compararlos sin importar
    acentos, mayúsculas o pequeñas variaciones de escritura.

    Ej: 'México' / 'Mexico' / 'MEXICO' -> 'mexico'
    """
    if not nombre:
        return ""
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", nombre) if unicodedata.category(c) != "Mn"
    )
    return sin_acentos.strip().lower()


def obtener_partidos(jornada=None, reintentos=3):
    """Consulta los partidos del Mundial en football-data.org.

    Si se especifica `jornada`, filtra por ese matchday. Devuelve la
    lista cruda que entrega la API (lista de dicts).
    """
    params = {}
    if jornada is not None:
        params["matchday"] = jornada

    url = f"{BASE_URL}/competitions/{COMPETITION_CODE}/matches"

    ultimo_error = None
    for intento in range(reintentos):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=15)
            if resp.status_code == 429:
                # límite de 10 req/min alcanzado: esperamos y reintentamos
                time.sleep(6)
                continue
            resp.raise_for_status()
            return resp.json().get("matches", [])
        except requests.RequestException as exc:
            ultimo_error = exc
            time.sleep(2)

    raise FootballDataError(f"No se pudo consultar football-data.org: {ultimo_error}")


def extraer_resultado(partido_api):
    """De un partido tal como lo devuelve la API, extrae los campos que
    nos importan en un formato simple.
    """
    score = partido_api.get("score", {}).get("fullTime", {}) or {}
    return {
        "api_match_id": partido_api.get("id"),
        "jornada": partido_api.get("matchday"),
        "fecha": partido_api.get("utcDate"),
        "estado": partido_api.get("status"),  # SCHEDULED, LIVE, IN_PLAY, FINISHED, ...
        "equipo_local": (partido_api.get("homeTeam") or {}).get("name"),
        "equipo_visitante": (partido_api.get("awayTeam") or {}).get("name"),
        "marcador_local": score.get("home"),
        "marcador_visitante": score.get("away"),
    }
