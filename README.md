# Quiniela Mundial 2026

App en Flask para llevar la quiniela del Mundial 2026 entre tus 18 amigos,
sin tener que ir revisando partido por partido a mano.

## Qué hace

- **Tabla de posiciones** con puntos de cada participante.
- **Vista por jornada**: ves cada partido, el marcador real y la predicción
  de cada uno de los 18.
- **Subida de Excel/CSV**: suben las predicciones de todos de una sola vez.
- **Sincronización automática de resultados** contra la API gratuita
  [football-data.org](https://www.football-data.org) — un botón trae los
  marcadores reales y recalcula los puntos de todas las predicciones.
- **Reglas de puntuación**:
  - Marcador exacto correcto → **3 puntos**
  - Solo acertaste quién ganaba o que fue empate (resultado), marcador distinto → **1 punto**
  - Cualquier otro caso → **0 puntos**

## Stack

- **Flask** + **Flask-SQLAlchemy** (backend y ORM)
- **SQLite** por default (un solo archivo, cero configuración). Se puede
  cambiar a Postgres/Turso con una variable de entorno, ver abajo.
- **pandas + openpyxl** para leer los Excel/CSV de predicciones
- **requests** para consultar football-data.org
- **gunicorn** para producción (Railway, Render, etc.)

## 1. Instalación local

```bash
cd quiniela_app
python -m venv venv
source venv/bin/activate        # en Windows PowerShell: venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

Edita `.env` y pon tu token de football-data.org (ver siguiente sección) y
una `SECRET_KEY` cualquiera.

```bash
flask --app app init-db
flask --app app run --debug
```

Abre http://127.0.0.1:5000

## 2. Consigue tu API key gratis (football-data.org)

1. Entra a https://www.football-data.org/client/register y crea una cuenta
   (es gratis, no pide tarjeta).
2. Te llega un token por correo. Cópialo en `FOOTBALL_DATA_API_KEY` en tu `.env`.
3. El plan gratis incluye el Mundial (código `WC`) con 10 consultas por
   minuto — de sobra para esto, ya que solo necesitamos consultar cuando
   termina un partido, no resultados en vivo minuto a minuto.

## 3. Cargar el calendario y los resultados

Cuando quieras traer todos los partidos del Mundial (sin marcador todavía):

```bash
flask --app app sync-fixtures
```

Para traer resultados reales y calificar las predicciones de una jornada
(también hay un botón "↻ Actualizar resultados" en la página de cada jornada):

```bash
flask --app app sync-resultados 3        # solo la jornada 3
flask --app app sync-resultados          # todas las disponibles
```

## 4. Agregar a los 18 participantes

Ve a "Jugadores" en el menú y agrégalos uno por uno, o simplemente súbelos
ya incluidos en tu primer Excel de predicciones (la app crea automáticamente
a cualquier jugador que no exista todavía).

## 5. Subir las predicciones

Usa el archivo `plantilla_predicciones.xlsx` incluido como referencia de
formato. Columnas requeridas (no importa el orden ni mayúsculas/minúsculas):

| jornada | jugador | equipo_local | equipo_visitante | pred_local | pred_visitante |
|---------|---------|--------------|-------------------|------------|----------------|
| 1       | Gio     | Mexico       | Poland            | 2          | 1              |
| 1       | Karla   | Mexico       | Poland            | 1          | 1              |

Una fila por cada combinación jugador + partido. Si tienes 18 jugadores y
12 partidos en una jornada, son 216 filas — tu Excel de control donde ya
llevas las quinielas de todos seguramente se puede reorganizar así
fácilmente con una tabla pivote o con un script corto.

Los nombres de equipo no necesitan coincidir letra por letra con la API
(la comparación ignora mayúsculas/acentos), pero sí deben ser consistentes
entre las filas del mismo partido para que la app las agrupe bien.

## 6. Desplegarlo en línea (Railway, igual que tu proyecto V6)

1. Sube esta carpeta a un repo de GitHub.
2. En Railway: New Project → Deploy from GitHub.
3. Agrega las variables de entorno `FOOTBALL_DATA_API_KEY` y `SECRET_KEY`
   en la pestaña Variables del servicio.
4. Railway detecta el `Procfile` (`web: gunicorn app:app`) automáticamente.
5. **Importante sobre la base de datos**: el sistema de archivos de Railway
   no es persistente entre deploys. Para que el SQLite no se borre:
   - Opción simple: agrega un **Volume** en Railway y monta `instance/` ahí.
   - Opción recomendada si ya usas Turso en V6: cambia
     `SQLALCHEMY_DATABASE_URI` para apuntar a tu base de Turso. Necesitas
     instalar `sqlalchemy-libsql` y usar una URL tipo
     `sqlite+libsql://<tu-db>.turso.io/?authToken=...`. Es el mismo patrón
     que ya usas en V6, solo aplicado a este proyecto.
6. Después del primer deploy, corre una sola vez (Railway → Shell):
   ```bash
   flask --app app init-db
   flask --app app sync-fixtures
   ```

## 7. Mantenerlo actualizado durante el torneo

La forma más simple: revisar la jornada en curso y dar clic en
"↻ Actualizar resultados" cuando terminen los partidos del día. Si quieres
que sea 100% automático, puedes agregar un **Cron Job de Railway** que
llame a `flask --app app sync-resultados` cada hora durante los días de
partido — la API solo te devuelve algo nuevo cuando un partido realmente
termina, así que no hay riesgo de recalcular de más.

## Estructura del proyecto

```
quiniela_app/
├── app.py                     # rutas Flask + comandos CLI
├── models.py                  # Jugador, Partido, Prediccion (SQLAlchemy)
├── scoring.py                 # reglas de puntuación (3 / 1 / 0 puntos)
├── football_api.py            # cliente de football-data.org
├── test_scoring.py            # pruebas de la lógica de puntuación
├── plantilla_predicciones.xlsx
├── requirements.txt
├── Procfile                   # para Railway/Render
├── .env.example
├── templates/                 # HTML (Jinja2 + Bootstrap)
└── static/style.css
```
# quiniela_Sgio
