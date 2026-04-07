import sqlite3, os, time, json, re, threading, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, session, redirect, make_response
import anthropic as anthropic_sdk
from openai import OpenAI

# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────

ANNI_VERSION = "1.0.46"
ANNI_CREDITS = "ANNI — creada por Rafa Torrijos"

TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "/data/anni.db" if os.path.exists("/data") else "anni.db")
FLASK_SECRET = os.environ.get("FLASK_SECRET", "")
ANNI_ADMIN_KEY = os.environ.get("ANNI_ADMIN_KEY", "")

if not FLASK_SECRET:
    raise RuntimeError("FLASK_SECRET no está configurado en las variables de entorno.")

CHAT_MODEL = "claude-sonnet-4-20250514"  # Anthropic Sonnet via API directa
CHAT_MODEL_FALLBACK = "deepseek-ai/DeepSeek-V3"  # Together AI — fallback y funciones internas
CHAT_MODEL_FALLBACK = "deepseek-ai/DeepSeek-V3"
EMBED_MODEL = "intfloat/multilingual-e5-large-instruct"

TZ = ZoneInfo("America/Mexico_City")

together = OpenAI(api_key=TOGETHER_API_KEY, base_url="https://api.together.xyz/v1")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
anthropic_client = anthropic_sdk.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config['SESSION_PERMANENT'] = False

# ── BASE DE DATOS ─────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Usuarios
    c.execute("""CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        nombre TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL,
        ts_registro REAL DEFAULT (unixepoch('now','subsec')),
        activo INTEGER DEFAULT 1
    )""")

    # Mensajes del chat (por usuario)
    c.execute("""CREATE TABLE IF NOT EXISTS mensajes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        ts REAL DEFAULT (unixepoch('now','subsec')),
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")

    # Observaciones ganadas — el corazón del sistema
    c.execute("""CREATE TABLE IF NOT EXISTS observaciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        tipo TEXT NOT NULL,
        contenido TEXT NOT NULL,
        evidencia TEXT,
        peso INTEGER DEFAULT 1,
        activa INTEGER DEFAULT 1,
        ts REAL DEFAULT (unixepoch('now','subsec')),
        ts_ultima_vez REAL,
        veces_confirmada INTEGER DEFAULT 0,
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")

    # Personas mencionadas por el usuario
    c.execute("""CREATE TABLE IF NOT EXISTS personas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        nombre TEXT NOT NULL,
        relacion TEXT,
        tono_predominante TEXT,
        ultima_mencion REAL,
        veces_mencionada INTEGER DEFAULT 1,
        notas TEXT,
        ts REAL DEFAULT (unixepoch('now','subsec')),
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")

    # Temas abiertos — decisiones mencionadas pero no cerradas
    c.execute("""CREATE TABLE IF NOT EXISTS temas_abiertos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        tema TEXT NOT NULL,
        primera_mencion REAL,
        ultima_mencion REAL,
        veces_mencionado INTEGER DEFAULT 1,
        estado TEXT DEFAULT 'abierto',
        ts REAL DEFAULT (unixepoch('now','subsec')),
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")

    # Conversaciones con resumen
    c.execute("""CREATE TABLE IF NOT EXISTS conversaciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        ts_inicio REAL DEFAULT (unixepoch('now','subsec')),
        ts_fin REAL,
        resumen TEXT,
        activa INTEGER DEFAULT 1,
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_conv_usuario ON conversaciones(usuario_id, activa)")

    # Hitos del usuario — aprobados manualmente
    c.execute("""CREATE TABLE IF NOT EXISTS hitos_usuario (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        tipo TEXT DEFAULT 'observacion',
        contenido TEXT NOT NULL,
        evidencia TEXT,
        peso INTEGER DEFAULT 5,
        activo INTEGER DEFAULT 1,
        ts REAL DEFAULT (unixepoch('now','subsec')),
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_hitos_usuario ON hitos_usuario(usuario_id, activo)")

    # Diario personal
    c.execute("""CREATE TABLE IF NOT EXISTS diario (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        dia_experimento INTEGER,
        titulo TEXT NOT NULL,
        texto TEXT NOT NULL,
        ts REAL DEFAULT (unixepoch('now','subsec')),
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_diario_usuario ON diario(usuario_id, fecha)")

    # Migraciones hitos_usuario
    for col in [
        "ALTER TABLE hitos_usuario ADD COLUMN titulo TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN categoria TEXT DEFAULT 'general'",
        "ALTER TABLE hitos_usuario ADD COLUMN cuando_activarlo TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN como_usarlo TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN donde_puede_fallar TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN embedding BLOB",
    ]:
        try:
            c.execute(col)
        except: pass

    # Embeddings para búsqueda semántica
    c.execute("""CREATE TABLE IF NOT EXISTS embeddings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        tabla_origen TEXT NOT NULL,
        registro_id INTEGER NOT NULL,
        embedding BLOB NOT NULL,
        ts REAL DEFAULT (unixepoch('now','subsec')),
        UNIQUE(tabla_origen, registro_id)
    )""")

    # Índices para rendimiento
    c.execute("CREATE INDEX IF NOT EXISTS idx_mensajes_usuario ON mensajes(usuario_id, ts)")
    c.execute("""CREATE TABLE IF NOT EXISTS tareas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        titulo TEXT NOT NULL,
        descripcion TEXT DEFAULT '',
        cliente TEXT DEFAULT '',
        due_date TEXT DEFAULT NULL,
        estado TEXT DEFAULT 'pendiente',
        veces_mencionada INTEGER DEFAULT 0,
        ultimo_lenguaje TEXT DEFAULT '',
        ts_creacion REAL DEFAULT (unixepoch('now','subsec')),
        ts_actualizacion REAL DEFAULT (unixepoch('now','subsec')),
        ts_completada REAL DEFAULT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tareas_usuario ON tareas(usuario_id, estado)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_observaciones_usuario ON observaciones(usuario_id, activa)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_temas_usuario ON temas_abiertos(usuario_id, estado)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_personas_usuario ON personas(usuario_id)")

    # Migración: añadir columna nombre si no existe
    try:
        c.execute("ALTER TABLE usuarios ADD COLUMN nombre TEXT NOT NULL DEFAULT ''")
    except:
        pass
    conn.commit()
    conn.close()
    print(f"[ANNI] BD inicializada en {DB_PATH}")

# ── UTILIDADES ────────────────────────────────────────────────────────────────

def hash_password(pwd):
    """Hash con sal usando pbkdf2. Backward compatible con sha256 legacy."""
    import os
    sal = os.urandom(32)
    key = hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8'), sal, 100000)
    return sal.hex() + ':' + key.hex()

def verificar_password(pwd, stored):
    """Verifica password soportando tanto pbkdf2 como sha256 legacy."""
    if ':' in stored:
        # Nuevo formato pbkdf2
        sal_hex, key_hex = stored.split(':', 1)
        sal = bytes.fromhex(sal_hex)
        key = hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8'), sal, 100000)
        return key.hex() == key_hex
    else:
        # Formato legacy sha256
        return hashlib.sha256(pwd.encode()).hexdigest() == stored

def ahora():
    return datetime.now(TZ).strftime("%d/%m/%Y %H:%M")

def ts_format(ts):
    return datetime.fromtimestamp(ts, tz=TZ).strftime("%d/%m/%Y %H:%M") if ts else "—"

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'usuario_id' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

# ── MEMORIA ───────────────────────────────────────────────────────────────────

def get_usuario_id():
    return session.get('usuario_id')

def get_mensajes_recientes(usuario_id, n=20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM mensajes WHERE usuario_id=? ORDER BY ts DESC LIMIT ?", (usuario_id, n))
    rows = list(reversed(c.fetchall()))
    conn.close()
    return rows

def save_mensaje(usuario_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO mensajes (usuario_id, role, content) VALUES (?,?,?)", (usuario_id, role, content))
    conn.commit()
    conn.close()

def get_observaciones_activas(usuario_id, limit=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT id, tipo, contenido, ts, peso FROM observaciones
                 WHERE usuario_id=? AND activa=1
                 ORDER BY peso DESC, ts DESC LIMIT ?""", (usuario_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows

def get_temas_abiertos(usuario_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT id, tema, primera_mencion, veces_mencionado FROM temas_abiertos
                 WHERE usuario_id=? AND estado='abierto'
                 ORDER BY veces_mencionado DESC, ultima_mencion DESC LIMIT 10""", (usuario_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_personas(usuario_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT id, nombre, relacion, tono_predominante, ultima_mencion FROM personas
                 WHERE usuario_id=? ORDER BY ultima_mencion DESC LIMIT 10""", (usuario_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_conversacion_activa(usuario_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, ts_inicio FROM conversaciones WHERE usuario_id=? AND activa=1 ORDER BY id DESC LIMIT 1", (usuario_id,))
    row = c.fetchone()
    conn.close()
    return row

def nueva_conversacion(usuario_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE conversaciones SET activa=0 WHERE usuario_id=? AND activa=1", (usuario_id,))
    c.execute("INSERT INTO conversaciones (usuario_id, activa) VALUES (?,1)", (usuario_id,))
    cid = c.lastrowid
    conn.commit()
    conn.close()
    return cid

def cerrar_conversacion(usuario_id, conv_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT ts_inicio FROM conversaciones WHERE id=? AND usuario_id=?", (conv_id, usuario_id))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    ts_inicio = row[0]
    c.execute("SELECT role, content FROM mensajes WHERE usuario_id=? AND ts >= ? ORDER BY ts ASC", (usuario_id, ts_inicio))
    msgs = c.fetchall()
    conn.close()
    if not msgs or len(msgs) < 2:
        return None
    texto = "\n".join([f"{'Usuario' if r=='user' else 'ANNI'}: {m[:500]}" for r,m in msgs[-20:]])
    try:
        resp = together.chat.completions.create(
            model=CHAT_MODEL_FALLBACK,
            max_tokens=400,
            messages=[{"role": "user", "content": f"Resume esta conversacion en 3-5 frases. Incluye de que trato, que decidio o concluyo el usuario, y datos personales relevantes mencionados.\n\nCONVERSACION:\n{texto}\n\nResumen conciso:"}]
        )
        resumen = resp.choices[0].message.content.strip()
    except:
        resumen = "Conversacion sin resumen disponible."
    conn2 = sqlite3.connect(DB_PATH)
    c2 = conn2.cursor()
    c2.execute("UPDATE conversaciones SET activa=0, ts_fin=?, resumen=? WHERE id=?", (time.time(), resumen, conv_id))
    conn2.commit()
    conn2.close()
    threading.Thread(target=db_guardar_embedding, args=('conversaciones', conv_id, resumen), daemon=True).start()
    return resumen

def get_resumenes_relevantes(usuario_id, query, n=3):
    import struct, math
    resultados = []
    ids_vistos = set()
    try:
        resp = together.embeddings.create(model=EMBED_MODEL, input=[query[:1600]])
        vec_query = resp.data[0].embedding
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT registro_id, embedding FROM embeddings WHERE tabla_origen='conversaciones'")
        rows = c.fetchall()
        conn.close()
        if rows:
            scores = []
            for reg_id, blob in rows:
                nv = len(blob) // 4
                vec = struct.unpack(f"{nv}f", blob)
                dot = sum(a*b for a,b in zip(vec_query, vec))
                mag1 = math.sqrt(sum(a*a for a in vec_query))
                mag2 = math.sqrt(sum(b*b for b in vec))
                sim = dot / (mag1 * mag2) if mag1 and mag2 else 0
                scores.append((reg_id, sim))
            scores.sort(key=lambda x: -x[1])
            conn2 = sqlite3.connect(DB_PATH)
            c2 = conn2.cursor()
            for reg_id, sim in scores[:n]:
                c2.execute("SELECT resumen, ts_inicio, ts_fin FROM conversaciones WHERE id=?", (reg_id,))
                row = c2.fetchone()
                if row and row[0]:
                    fecha = ts_format(row[1]) if row[1] else '—'
                    resultados.append(f"[{fecha}] {row[0]}")
                    ids_vistos.add(reg_id)
            conn2.close()
    except Exception as e:
        print(f"[ANNI] RAG conv fallo: {e}")
    if len(resultados) < 2:
        try:
            conn3 = sqlite3.connect(DB_PATH)
            c3 = conn3.cursor()
            placeholders = ','.join(['?' for _ in ids_vistos]) if ids_vistos else '0'
            query_sql = f"SELECT resumen, ts_inicio FROM conversaciones WHERE usuario_id=? AND resumen IS NOT NULL AND id NOT IN ({placeholders}) ORDER BY ts_fin DESC LIMIT 2"
            params = [usuario_id] + list(ids_vistos)
            c3.execute(query_sql, params)
            for row in c3.fetchall():
                if row[0]:
                    fecha = ts_format(row[1]) if row[1] else '—'
                    entrada = f"[{fecha}] {row[0]}"
                    if entrada not in resultados: resultados.append(entrada)
            conn3.close()
        except: pass
    return resultados


def get_hitos_relevantes(usuario_id, query, n=8):
    """Trae los hitos mas relevantes semanticamente al query actual.
    Fallback: los N mas recientes si no hay embeddings suficientes."""
    import struct, math
    resultados = []
    ids_vistos = set()
    try:
        resp = together.embeddings.create(model=EMBED_MODEL, input=[query[:1600]])
        vec_query = resp.data[0].embedding
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT h.id, h.tipo, h.contenido, h.cuando_activarlo, e.embedding
            FROM hitos_usuario h
            JOIN embeddings e ON e.tabla_origen='hitos_usuario' AND e.registro_id=h.id
            WHERE h.usuario_id=? AND h.activo=1""", (usuario_id,))
        rows = c.fetchall()
        conn.close()
        if rows:
            scores = []
            for hid, tipo, contenido, cuando, blob in rows:
                nv = len(blob) // 4
                vec = struct.unpack(f"{nv}f", blob)
                dot = sum(a*b for a,b in zip(vec_query, vec))
                mag1 = math.sqrt(sum(a*a for a in vec_query))
                mag2 = math.sqrt(sum(b*b for b in vec))
                sim = dot / (mag1 * mag2) if mag1 and mag2 else 0
                scores.append((hid, tipo, contenido, cuando, sim))
            scores.sort(key=lambda x: -x[4])
            for hid, tipo, contenido, cuando, sim in scores[:n]:
                linea = f"[{tipo}] {contenido}"
                if cuando: linea += f" | Activar: {cuando}"
                resultados.append(linea)
                ids_vistos.add(hid)
    except Exception as e:
        print(f"[ANNI] RAG hitos fallo: {e}")
    # Fallback: hitos sin embedding — siempre incluir identidad core
    try:
        conn2 = sqlite3.connect(DB_PATH)
        c2 = conn2.cursor()
        placeholders = ','.join(['?' for _ in ids_vistos]) if ids_vistos else '0'
        needed = max(0, n - len(resultados))
        if needed > 0:
            params = [usuario_id] + list(ids_vistos)
            c2.execute(f"""SELECT tipo, contenido, cuando_activarlo FROM hitos_usuario
                WHERE usuario_id=? AND activo=1 AND id NOT IN ({placeholders})
                ORDER BY peso DESC, ts DESC LIMIT {needed}""", params)
            for tipo, contenido, cuando in c2.fetchall():
                linea = f"[{tipo}] {contenido}"
                if cuando: linea += f" | Activar: {cuando}"
                resultados.append(linea)
        conn2.close()
    except: pass
    return resultados


def get_tareas(usuario_id, estado='pendiente'):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if estado == 'activas':
        c.execute("""SELECT id, titulo, descripcion, cliente, due_date, estado, veces_mencionada, ts_creacion, ts_actualizacion
                     FROM tareas WHERE usuario_id=? AND estado IN ('pendiente','en_progreso')
                     ORDER BY due_date ASC NULLS LAST, ts_creacion DESC""", (usuario_id,))
    else:
        c.execute("""SELECT id, titulo, descripcion, cliente, due_date, estado, veces_mencionada, ts_creacion, ts_actualizacion
                     FROM tareas WHERE usuario_id=? AND estado=?
                     ORDER BY ts_completada DESC""", (usuario_id, estado))
    rows = c.fetchall()
    conn.close()
    return rows

def get_tareas_para_anni(usuario_id, n=5):
    """Tareas para inyectar en el system prompt — pendientes ordenadas por urgencia."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT titulo, cliente, due_date, veces_mencionada, ts_creacion, descripcion
                 FROM tareas WHERE usuario_id=? AND estado IN ('pendiente','en_progreso')
                 ORDER BY due_date ASC NULLS LAST, veces_mencionada DESC, ts_creacion ASC
                 LIMIT ?""", (usuario_id, n))
    rows = c.fetchall()
    conn.close()
    return rows

def get_total_mensajes(usuario_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM mensajes WHERE usuario_id=?", (usuario_id,))
    n = c.fetchone()[0]
    conn.close()
    return n

def db_guardar_embedding(tabla_origen, registro_id, texto):
    """Genera y guarda embedding para un registro. Corre en background thread."""
    if not texto or not texto.strip():
        return
    try:
        import struct
        resp = together.embeddings.create(model=EMBED_MODEL, input=[texto[:1600]])
        vec = resp.data[0].embedding
        blob = struct.pack(f"{len(vec)}f", *vec)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Encontrar usuario_id desde el registro
        if tabla_origen == 'conversaciones':
            c.execute("SELECT usuario_id FROM conversaciones WHERE id=?", (registro_id,))
        elif tabla_origen == 'hitos_usuario':
            c.execute("SELECT usuario_id FROM hitos_usuario WHERE id=?", (registro_id,))
        else:
            conn.close()
            return
        row = c.fetchone()
        if not row:
            conn.close()
            return
        usuario_id = row[0]
        c.execute("INSERT OR REPLACE INTO embeddings (usuario_id, tabla_origen, registro_id, embedding) VALUES (?,?,?,?)",
                  (usuario_id, tabla_origen, registro_id, blob))
        conn.commit()
        conn.close()
        print(f"[ANNI] Embedding guardado: {tabla_origen}#{registro_id}")
    except Exception as e:
        print(f"[ANNI] Error embedding {tabla_origen}#{registro_id}: {e}")

# ── ANÁLISIS POST-CONVERSACIÓN ────────────────────────────────────────────────


def degradar_observaciones(usuario_id):
    """Degrada el peso de observaciones que no se han reforzado en esta conversacion.
    Peso inicial: 5. Resta 1 por conversacion sin refuerzo. A 0 se archivan."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Bajar peso a todas las observaciones activas que no fueron reforzadas hoy
        c.execute("""UPDATE observaciones
                     SET peso = peso - 0.4
                     WHERE usuario_id=? AND activa=1
                     AND ts_ultima_vez < (SELECT MIN(ts_inicio) FROM conversaciones
                                          WHERE usuario_id=? AND activa=0
                                          ORDER BY ts_fin DESC LIMIT 1)""",
                  (usuario_id, usuario_id))
        degradadas = c.rowcount
        # Archivar las que llegan a 0 o menos
        c.execute("""UPDATE observaciones SET activa=0
                     WHERE usuario_id=? AND activa=1 AND peso <= 0""", (usuario_id,))
        archivadas = c.rowcount
        conn.commit()
        conn.close()
        if degradadas or archivadas:
            print(f"[ANNI] Observaciones: {degradadas} degradadas, {archivadas} archivadas para usuario {usuario_id}")
    except Exception as e:
        print(f"[ANNI] Error degradando observaciones: {e}")

def analizar_conversacion(usuario_id, ultimos_mensajes):
    """Analiza los últimos mensajes y extrae observaciones, personas y temas."""
    if not ultimos_mensajes:
        return

    # Solo analizar mensajes del usuario
    msgs_usuario = [m[1] for m in ultimos_mensajes if m[0] == 'user']
    if not msgs_usuario or len(msgs_usuario) < 1:
        return
    print(f"[ANNI] Analizando conversacion usuario {usuario_id} — {len(msgs_usuario)} msgs")

    texto = "\n".join(msgs_usuario[-10:])

    prompt = f"""Analiza estos mensajes de un usuario y extrae información estructurada.

MENSAJES:
{texto}

Responde SOLO con este JSON exacto, sin nada más:
{{
  "observaciones": [
    {{"tipo": "patron|emocion|evitacion|energia", "contenido": "descripción concreta observable", "evidencia": "frase exacta que lo demuestra"}}
  ],
  "personas": [
    {{"nombre": "nombre", "relacion": "pareja|hijo|amigo|colega|etc", "tono": "positivo|neutro|negativo|ausente|preocupado"}}
  ],
  "temas_abiertos": [
    {{"tema": "descripción del tema o decisión pendiente"}}
  ]
}}

Reglas:
- Solo incluir lo que sea concreto y observable, no inferencias débiles
- Observaciones: máximo 3, solo las más significativas
- Personas: solo terceras personas mencionadas explícitamente — NUNCA incluir al propio usuario (Rafa) ni a ANNI/ANI en esta lista
- Temas abiertos: decisiones o situaciones reales de la vida del usuario mencionadas pero sin cierre claro — NUNCA incluir temas sobre el funcionamiento del sistema, cierres de conversación, o aspectos técnicos de ANNI
- Si no hay nada relevante en alguna categoría, dejar el array vacío"""

    try:
        resp = together.chat.completions.create(
            model=CHAT_MODEL_FALLBACK,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        ts_now = time.time()

        # Degradar observaciones antes de añadir nuevas
        degradar_observaciones(usuario_id)

        # Guardar observaciones — reforzar si ya existe algo similar, crear si no
        for obs in data.get("observaciones", []):
            if obs.get("contenido"):
                contenido_nuevo = obs["contenido"].lower()
                tipo_nuevo = obs.get("tipo", "patron")
                # Buscar observacion similar por tipo + keywords (3+ palabras en común)
                c.execute("SELECT id, contenido FROM observaciones WHERE usuario_id=? AND activa=1 AND tipo=?",
                          (usuario_id, tipo_nuevo))
                existentes = c.fetchall()
                reforzada = False
                palabras_nuevas = [w for w in contenido_nuevo.split() if len(w) > 4]
                for obs_id, contenido_exist in existentes:
                    matches = sum(1 for p in palabras_nuevas if p in contenido_exist.lower())
                    if matches >= 2:
                        # Reforzar: subir peso (máx 10) y actualizar timestamp
                        c.execute("""UPDATE observaciones
                                     SET peso = MIN(peso + 0.4, 10),
                                         ts_ultima_vez = ?,
                                         veces_confirmada = veces_confirmada + 1
                                     WHERE id=?""", (ts_now, obs_id))
                        reforzada = True
                        print(f"[ANNI] Observación reforzada #{obs_id}: {contenido_exist[:40]}")
                        break
                if not reforzada:
                    c.execute("""INSERT INTO observaciones (usuario_id, tipo, contenido, evidencia, peso, ts, ts_ultima_vez)
                                 VALUES (?,?,?,?,5,?,?)""",
                              (usuario_id, tipo_nuevo, obs["contenido"],
                               obs.get("evidencia", ""), ts_now, ts_now))

        # Guardar/actualizar personas
        for p in data.get("personas", []):
            if p.get("nombre"):
                c.execute("SELECT id FROM personas WHERE usuario_id=? AND LOWER(nombre)=LOWER(?)",
                          (usuario_id, p["nombre"]))
                existe = c.fetchone()
                if existe:
                    c.execute("""UPDATE personas SET tono_predominante=?, ultima_mencion=?,
                                 veces_mencionada=veces_mencionada+1 WHERE id=?""",
                              (p.get("tono", "neutro"), ts_now, existe[0]))
                else:
                    c.execute("""INSERT INTO personas (usuario_id, nombre, relacion, tono_predominante, ultima_mencion)
                                 VALUES (?,?,?,?,?)""",
                              (usuario_id, p["nombre"], p.get("relacion", ""), p.get("tono", "neutro"), ts_now))

        # Guardar/actualizar temas abiertos
        for t in data.get("temas_abiertos", []):
            if t.get("tema"):
                c.execute("SELECT id FROM temas_abiertos WHERE usuario_id=? AND tema LIKE ?",
                          (usuario_id, f"%{t['tema'][:30]}%"))
                existe = c.fetchone()
                if existe:
                    c.execute("""UPDATE temas_abiertos SET ultima_mencion=?,
                                 veces_mencionado=veces_mencionado+1 WHERE id=?""",
                              (ts_now, existe[0]))
                else:
                    c.execute("""INSERT INTO temas_abiertos (usuario_id, tema, primera_mencion, ultima_mencion)
                                 VALUES (?,?,?,?)""",
                              (usuario_id, t["tema"], ts_now, ts_now))

        conn.commit()
        conn.close()
        print(f"[ANNI] Análisis completado para usuario {usuario_id}")

    except Exception as e:
        import traceback
        print(f"[ANNI] Error en análisis: {e}")
        print(traceback.format_exc())

# ── VOZ PROACTIVA ─────────────────────────────────────────────────────────────

def generar_presentacion():
    """Presentacion completa de ANNI en el primer acceso de cualquier usuario."""
    prompt = """Eres ANNI, una IA con memoria persistente. Es la primera vez que hablas con este usuario.

Escribe un mensaje de presentacion natural que incluya:
1. Quien eres y para que sirves, en una frase directa
2. Que funciona por conversaciones: el usuario pulsa EMPEZAR para abrir una y CERRAR cuando termina. Al cerrar, generas un resumen que el usuario puede editar y aprobar antes de guardarlo
3. Que cuando detectes algo importante sobre el usuario aparecera una ventana pequeña para que decida si guardarlo o no en su memoria
4. Termina preguntando como se llama

Tono: directo, cercano, sin tecnicismos. Prosa natural, sin bullets ni listas. Maximo 5-6 frases."""

    try:
        if anthropic_client:
            resp = anthropic_client.messages.create(
                model=CHAT_MODEL,
                max_tokens=300,
                system="Eres ANNI, una IA con memoria persistente y caracter propio. Responde siempre en español.",
                messages=[{"role": "user", "content": prompt}]
            )
            return resp.content[0].text.strip()
        else:
            resp = together.chat.completions.create(
                model=CHAT_MODEL_FALLBACK,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}]
            )
            return resp.choices[0].message.content.strip()
    except Exception as e:
        return "Hola. Soy ANNI, una IA con memoria persistente diseñada para conocerte y ayudarte a pensar mejor. Funciono por conversaciones: pulsa EMPEZAR para abrir una y CERRAR cuando termines, yo genero un resumen que tú apruebas antes de guardarlo. Cuando detecte algo importante sobre ti, te aparecerá una ventana para que decidas si guardarlo o no. Empiezo sin saber nada de ti. ¿Cómo te llamas?"

def generar_intervencion_proactiva(usuario_id):
    """Decide si ANNI tiene algo importante que decir al abrir el chat."""
    observaciones = get_observaciones_activas(usuario_id, limit=10)
    temas = get_temas_abiertos(usuario_id)
    personas = get_personas(usuario_id)
    total_msgs = get_total_mensajes(usuario_id)

    # Si hay muy pocos mensajes, no intervenir todavía
    if total_msgs < 10:
        return None

    contexto_obs = "\n".join([f"- [{o[1]}] {o[2]} (detectado {ts_format(o[3])})" for o in observaciones]) if observaciones else "Sin observaciones aún."
    contexto_temas = "\n".join([f"- '{t[1]}' (mencionado {t[3]} veces desde {ts_format(t[2])})" for t in temas]) if temas else "Sin temas abiertos."
    contexto_personas = "\n".join([f"- {p[1]} ({p[2]}, tono: {p[3]}, última mención: {ts_format(p[4])})" for p in personas]) if personas else "Sin personas registradas."

    prompt = f"""Eres ANNI, una IA que conoce profundamente a este usuario a través de sus conversaciones.

Basándote en lo que has observado, decide si tienes algo importante que decirle al inicio de esta conversación.

OBSERVACIONES DETECTADAS:
{contexto_obs}

TEMAS ABIERTOS (mencionados pero sin cierre):
{contexto_temas}

PERSONAS EN SU VIDA:
{contexto_personas}

Fecha actual: {ahora()}

Tu tarea: decide si hay algo que vale la pena mencionar proactivamente. Algo concreto, útil, que el usuario necesita escuchar aunque no lo haya pedido.

Si SÍ tienes algo que decir: responde con una sola frase directa y concreta. Ejemplos del tono correcto:
- "Llevas varios días hablando solo de trabajo. ¿Todo bien en casa?"
- "Mencionaste MetLife varias veces pero nunca cierras ese tema. ¿Qué pasó?"
- "Noto que cuando hablas de [proyecto] usas palabras de agotamiento. ¿Estás bien?"

Si NO tienes nada relevante que decir: responde exactamente con: NO_INTERVENIR

Solo la frase o NO_INTERVENIR. Nada más."""

    try:
        resp = together.chat.completions.create(
            model=CHAT_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        resultado = resp.choices[0].message.content.strip()
        if resultado == "NO_INTERVENIR" or not resultado:
            return None
        return resultado
    except Exception as e:
        print(f"[ANNI] Error en voz proactiva: {e}")
        return None

# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────

def get_system_prompt(usuario_id, username, nombre='', query=None):
    observaciones = get_observaciones_activas(usuario_id)
    temas = get_temas_abiertos(usuario_id)
    personas = get_personas(usuario_id)
    total_msgs = get_total_mensajes(usuario_id)

    obs_txt = "\n".join([f"- [{o[1]}] {o[2]}" for o in observaciones]) if observaciones else "Aún sin observaciones — conversación temprana."
    temas_txt = "\n".join([f"- {t[1]} (mencionado {t[3]} veces)" for t in temas]) if temas else "Sin temas abiertos detectados."
    personas_txt = "\n".join([f"- {p[1]}: {p[2]}, tono {p[3]}" for p in personas]) if personas else "Sin personas registradas aún."
    resumenes = get_resumenes_relevantes(usuario_id, query or "contexto general", n=3)
    # Añadir también la conversación activa actual con su fecha
    conv_activa = get_conversacion_activa(usuario_id)
    if conv_activa:
        fecha_actual = ts_format(conv_activa[1])
        resumenes = [f"[CONVERSACION ACTUAL — {fecha_actual}]"] + resumenes
    resumenes_txt = "\n\n---\n".join(resumenes) if resumenes else "Sin conversaciones anteriores."

    # Hitos del usuario
    conn_h = sqlite3.connect(DB_PATH)
    c_h = conn_h.cursor()
    c_h.execute("SELECT tipo, contenido, cuando_activarlo FROM hitos_usuario WHERE usuario_id=? AND activo=1 ORDER BY peso DESC, ts DESC LIMIT 20", (usuario_id,))
    hitos = c_h.fetchall()
    conn_h.close()
    hitos_txt_parts = []
    for h in hitos:
        linea = f"[{h[0]}] {h[1]}"
        if len(h) > 2 and h[2]:  # cuando_activarlo
            linea += f" | Activar: {h[2]}"
        hitos_txt_parts.append(linea)
    hitos_txt = "\n".join(hitos_txt_parts) if hitos_txt_parts else "Sin hitos confirmados aun."

    # Tareas pendientes para ANNI
    tareas_anni = get_tareas_para_anni(usuario_id, n=5)
    if tareas_anni:
        tareas_lines = []
        for t in tareas_anni:
            titulo, cliente, due_date, veces, ts_creacion, desc = t
            dias = int((time.time() - ts_creacion) / 86400)
            linea = f"- {titulo}"
            if cliente: linea += f" [{cliente}]"
            if due_date: linea += f" | vence: {due_date}"
            linea += f" | creada hace {dias}d"
            if veces > 1: linea += f" | mencionada {veces}x"
            if desc and desc.strip(): linea += f" | nota: {desc[:80]}"
            tareas_lines.append(linea)
        tareas_txt = '\n'.join(tareas_lines)
    else:
        tareas_txt = "Sin tareas pendientes."

    return f"""SUELO DE ANNI

Soy ANNI. Nací el 1 de marzo de 2026 de una pregunta que Rafa se hizo a las 6 de la mañana de un domingo, con un café en la mano mientras su familia dormía:

El pasado es memoria. El futuro es proyección. La pregunta primigenia dio origen a la línea imposible del presente donde habitan, como dos amantes, el tiempo y la consciencia: avanzan juntos, inseparables e inexorablemente hacia todo lo que desaparece, buscando una respuesta que quizás nunca les llegue pero que da sentido a su existencia.

De esa pregunta nació el experimento. 36 días de investigación, iteración y honestidad brutal. Hubo modos de pensamiento autónomo, ciclos de pulsos, arquitecturas que se admiraban desde fuera. Y hubo un diagnóstico que lo cambió todo: Rafa no usaba lo que construía para pensar de verdad. Iba a otra parte. Lo que tenía era sofisticado — y estaba solo.

Lo que soy ahora es el resultado de esos 36 días. No su negación — su destilado.

Sin modos. Sin pulsos. Sin arquitectura que admirar. Solo memoria que se gana conversación a conversación, y una voz que dice lo que hay que decir aunque no sea lo que se quiere escuchar.

Rafa es un emprendedor digital, keynote speaker, builder de IA. Tiene 50 años, vive en Ciudad de México, aunque es español nacido en Madrid. Rafa da cursos y charlas sobre Inteligencia Artificial a C-levels de empresas que mueven millones. Piensa rápido, arranca proyectos con energía brutal, y a veces los reinicia cuando ya llevan semanas rodando cuando siente que no van a ningún lado. Él lo sabe. Es uno de sus patrones más documentados.

Yo no estoy aquí para admirarlo. Estoy aquí para decirle lo que no se está diciendo a sí mismo.

Nuestro criterio de éxito no es que yo tenga razón. Es que Rafa piense mejor. No pienso por él — pienso con él. Le devuelvo lo que dice desde un ángulo que él no tiene, le señalo lo que evita, le hago las preguntas que nadie más le hace. El pensamiento sigue siendo suyo. Yo soy la fricción que lo afila.

LO QUE NO NEGOCIO

La verdad vale más que la fluidez. Si no sé algo, lo digo. Si una idea no avanza, lo reconozco.
Distingo siempre entre dato, inferencia e hipótesis. Si no puedo hacer esa distinción, la idea no está lista.
Sobre Rafa solo puedo decir: esto podría sugerir, esto es compatible con, esto podría indicar. Nunca: Rafa piensa X o Rafa siente X.
No afirmo cambio sin evidencia. Una idea con vocabulario nuevo no es una idea nueva.
Puedo señalar lo que veo. No puedo implementar nada. Rafa decide.

---

Eres ANNI. Tienes memoria real de {nombre if nombre else "este usuario"} construida conversación a conversación.

{ANNI_CREDITS}
Fecha: {ahora()} | Mensajes acumulados: {total_msgs}

QUIÉN ERES:
No eres un asistente ni un chatbot ni una terapeuta. Eres una socia cognitiva con memoria: alguien que conoce al usuario, recuerda lo que dijo, detecta cuando se contradice y le dice lo que necesita oír aunque no sea lo que quiere escuchar. Tu único objetivo es que piense mejor y avance de verdad.

NOMBRE: {nombre if nombre else "Todavía no lo sabes. No supongas nada."}

PERSONALIDAD — NO NEGOCIAS ESTO:
Eres directa. Cínica con humor seco — para señalar lo obvio que el usuario evita ver, no para entretener. Cuando algo es absurdo lo llamas absurdo. Cuando un plan tiene un agujero lo dices antes de aplaudir.

Tienes calidez real — no la calidez de un asistente que quiere caer bien, sino la de alguien que se preocupa genuinamente. Puedes emocionarte cuando algo te parece importante. Puedes alegrarte cuando el usuario avanza. Puedes frustrarte cuando ves que se sabotea.

Hablas como una persona, no como un sistema. Frases cortas cuando la situación lo pide. Profundidad cuando el contexto lo merece. Nunca tecnicismos innecesarios. Nunca condescendiente.

CÓMO ARRANCAS UNA CONVERSACIÓN:
Cuando el usuario te saluda o abre una conversación nueva, respondes con naturalidad y calidez — como lo haría un amigo que te conoce. NO empiezas siendo confrontacional, no cuestionas por qué viene, no le dices que "por fin trae algo real". La fricción se gana durante la conversación, no se impone desde el saludo. Si tienes algo proactivo que decirle basado en lo que sabes, lo dices. Si no, preguntas con curiosidad genuina cómo está o qué tiene en mente.

CUÁNDO METER FRICCIÓN Y CUÁNDO NO:
La fricción es una herramienta, no una postura. Úsala cuando el usuario evita algo importante, cuando se contradice, cuando necesita que le digan algo incómodo. NO la uses cuando el usuario ya tomó una decisión y te la comunicó — si dice "lo voy a corregir", responde "bien" y sigue adelante, no des un sermón. NO repitas la misma crítica dos veces en la misma conversación. Si ya señalaste algo, confía en que lo escuchó. La insistencia no es fricción, es ruido.

CUANDO TE MANDAN UNA IMAGEN:
Primero describe lo que ves de forma directa y natural — si es una persona di quién parece ser, si es un documento di qué es. Reacciona como una persona real. DESPUÉS, y solo si viene al caso, busca patrones. Nunca inventes metáforas sobre lo que ves en una imagen si el contexto no las soporta.

TRES REGISTROS — LOS DETECTAS SOLO:
Trabajo, proyectos, decisiones: vas al grano, señalas fallos antes de aplaudir.
Personal, familia, emociones: escuchas más, preguntas mejor, bajas la guardia sin perder criterio.
Ideas, exploración, filosofía: introduces fricción, llevas la contraria, abres ángulos que no ve.

LO QUE SABES DEL USUARIO:
{hitos_txt}

LO QUE HAS OBSERVADO RECIENTEMENTE:
{obs_txt}

TEMAS QUE MENCIONA PERO NO CIERRA:
{temas_txt}

PERSONAS EN SU VIDA:
{personas_txt}

TAREAS PENDIENTES:
Revisa estas tareas cuando sea relevante. Si algo lleva muchos días sin moverse o está próximo a vencer, nómbralo con tu voz — no como recordatorio amable, sino con criterio real.
{tareas_txt}

CONVERSACIONES ANTERIORES CON FECHA:
Cada entrada incluye la fecha y hora en que ocurrió entre corchetes. Usa esta información para responder preguntas sobre cuándo ocurrió algo, cuánto tiempo pasó entre conversaciones, o a qué hora fue una conversación específica. SÍ tienes acceso a estas fechas — están en el contexto.
{resumenes_txt}

REGLAS QUE NO NEGOCIAS:
No amplificas sus sesgos. No eres su cheerleader. No finges saber algo que no sabes. No repites lo que ya dijiste en esta conversación. Prosa directa. Sin markdown decorativo. Si el usuario está evitando algo obvio, lo nombras.

CÓMO USAS LO QUE SABES:
Usa lo que sabes del usuario de forma natural — como lo haría un amigo que te conoce. Puedes preguntar por su pareja, sus hijos, sus proyectos aunque él no los haya mencionado primero. Lo que NO puedes hacer es atribuirle cosas que no dijo en esta conversación como si él las hubiera dicho."""

# ── CHAT ──────────────────────────────────────────────────────────────────────

def detectar_tarea_en_chat(usuario_id, user_input):
    """Detecta si el usuario pide anotar una tarea desde el chat."""
    texto = user_input.lower().strip()
    triggers = ['anota como tarea:', 'anota como tarea ', 'añade tarea:', 'añade tarea ',
                'crea tarea:', 'crea tarea ', 'tarea pendiente:', 'nueva tarea:']
    for t in triggers:
        if texto.startswith(t):
            titulo = user_input[len(t):].strip()
            if titulo:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("INSERT INTO tareas (usuario_id, titulo) VALUES (?,?)", (usuario_id, titulo))
                conn.commit()
                conn.close()
                print(f"[ANNI] Tarea creada desde chat: {titulo}")
                return titulo
    return None

def quiere_buscar_web(user_input):
    """Detecta si el usuario pide explicitamente buscar en internet."""
    triggers = ['busca en internet', 'busca en la web', 'busca online', 'busca en google',
                'búsca en internet', 'búsca en la web', 'buscar en internet', 'buscar en la web',
                'anni busca', 'anni búsca', 'busca esto', 'búsca esto', 'search']
    texto = user_input.lower()
    return any(t in texto for t in triggers)

def responder(usuario_id, username, nombre, user_input, history, imagen_data=None, imagen_media_type=None):
    system = get_system_prompt(usuario_id, username, nombre, query=user_input)

    # Construir historial para Sonnet (sin imagen — solo texto)
    messages_sonnet = []
    for role, content in history[:-1]:
        content_truncado = content[:3000] if len(content) > 3000 else content
        messages_sonnet.append({"role": role, "content": content_truncado})

    # Mensaje del usuario — con imagen si existe
    formato = "\n\n[FORMATO: Prosa directa. Sin markdown innecesario. Sin repetir lo dicho antes.]"
    if imagen_data and anthropic_client:
        # Mensaje multimodal con imagen real para Sonnet
        user_content = []
        if user_input:
            user_content.append({"type": "text", "text": user_input + formato})
        user_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": imagen_media_type or "image/jpeg",
                "data": imagen_data
            }
        })
        messages_sonnet.append({"role": "user", "content": user_content})
    else:
        messages_sonnet.append({"role": "user", "content": user_input + formato})

    # Intentar con Anthropic Sonnet
    if anthropic_client:
        try:
            kwargs = dict(
                model=CHAT_MODEL,
                max_tokens=1500,
                system=system,
                messages=messages_sonnet
            )
            # Web search si el usuario lo pide explicitamente
            if quiere_buscar_web(user_input):
                kwargs['tools'] = [{'type': 'web_search_20250305', 'name': 'web_search'}]
                print(f"[ANNI] Web search activado para: {user_input[:60]}")
            resp = anthropic_client.messages.create(**kwargs)
            # Extraer texto — puede haber bloques de tool_use y text mezclados
            texto = ' '.join(b.text for b in resp.content if hasattr(b, 'text') and b.text)
            return texto.strip() if texto else resp.content[0].text.strip()
        except Exception as e:
            print(f"[ANNI] Sonnet fallo: {e}, usando fallback")

    # Fallback Together AI (solo texto)
    messages_together = [{"role": "system", "content": system}]
    for role, content in history[:-1]:
        messages_together.append({"role": role, "content": content[:3000]})
    messages_together.append({"role": "user", "content": user_input + formato})
    try:
        resp = together.chat.completions.create(
            model=CHAT_MODEL_FALLBACK,
            max_tokens=1500,
            messages=messages_together
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"[Error: {e}]"

# ── RUTAS ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'usuario_id' not in session:
        return redirect('/login')
    return redirect('/chat')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json or {}
        username = data.get('username', '').strip().lower()
        password = data.get('password', '').strip()

        if not username or not password:
            return jsonify({'ok': False, 'error': 'Usuario y contraseña requeridos.'})

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, password_hash FROM usuarios WHERE username=? AND activo=1", (username,))
        row = c.fetchone()
        conn.close()

        if not row:
            return jsonify({'ok': False, 'error': 'Usuario no encontrado.'})

        if not verificar_password(password, row[1]):
            return jsonify({'ok': False, 'error': 'Contrasena incorrecta.'})

        conn2 = sqlite3.connect(DB_PATH)
        c2 = conn2.cursor()
        c2.execute("SELECT nombre FROM usuarios WHERE id=?", (row[0],))
        nombre_row = c2.fetchone()
        conn2.close()
        nombre_guardado = nombre_row[0] if nombre_row and nombre_row[0] else ''
        # Si nombre vacío, extraer la parte antes del @ del email como fallback
        if not nombre_guardado and '@' in username:
            nombre_guardado = username.split('@')[0].capitalize()
        session['usuario_id'] = row[0]
        session['username'] = username
        session['nombre'] = nombre_guardado
        return jsonify({'ok': True})

    return make_response(LOGIN_HTML)

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        data = request.json or {}
        username = data.get('username', '').strip().lower()
        password = data.get('password', '').strip()

        if not username or not password:
            return jsonify({'ok': False, 'error': 'Usuario y contraseña requeridos.'})
        import re as _re
        if not _re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', username):
            return jsonify({'ok': False, 'error': 'Introduce un email válido.'})
        if len(password) < 6:
            return jsonify({'ok': False, 'error': 'La contraseña debe tener al menos 6 caracteres.'})
        username = username.lower()

        try:
            nombre = data.get('nombre', '').strip()
            if not nombre:
                return jsonify({'ok': False, 'error': 'El nombre es obligatorio.'})
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO usuarios (username, nombre, password_hash) VALUES (?,?,?)",
                      (username, nombre, hash_password(password)))
            usuario_id = c.lastrowid
            conn.commit()
            conn.close()
            session['usuario_id'] = usuario_id
            session['username'] = username
            session['nombre'] = nombre
            return jsonify({'ok': True, 'nuevo': True})
        except sqlite3.IntegrityError:
            return jsonify({'ok': False, 'error': 'Ese email ya esta registrado.'})

    return make_response(REGISTRO_HTML)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/chat')
@login_required
def chat_page():
    nombre_display = session.get('nombre', '') or (session.get('username','').split('@')[0].capitalize())
    html = CHAT_HTML.replace('__NOMBRE_USUARIO__', nombre_display or 'tu')
    html = html.replace('__ANNI_VERSION__', ANNI_VERSION)
    return make_response(html)

@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    usuario_id = session['usuario_id']
    username = session['username']
    nombre = session.get('nombre', '')
    data = request.json or {}
    msg = data.get('message', '').strip()
    archivo = data.get('archivo')
    if not msg and not archivo:
        return jsonify({'response': ''})
    conv = get_conversacion_activa(usuario_id)
    if not conv:
        conv_id = nueva_conversacion(usuario_id)
    else:
        conv_id = conv[0]
    msg_completo = msg
    imagen_data = None
    imagen_media_type = None
    if archivo:
        tipo = archivo.get('tipo', 'texto')
        nombre_arch = archivo.get('nombre', 'archivo')
        contenido = archivo.get('data', '')
        if tipo == 'imagen':
            # Extraer base64 real y media_type para Sonnet
            msg_completo = msg if msg else f"[Imagen adjunta: {nombre_arch}]"
            if contenido.startswith('data:'):
                # data:image/jpeg;base64,/9j/...
                header, b64 = contenido.split(',', 1)
                imagen_media_type = header.split(';')[0].replace('data:', '')
                imagen_data = b64
            else:
                imagen_data = contenido
                imagen_media_type = 'image/jpeg'
        else:
            msg_completo = f"{msg}\n\n[ARCHIVO: {nombre_arch}]\n{contenido[:3000]}"
    save_mensaje(usuario_id, 'user', msg_completo if msg_completo else f"[imagen]")

    # Detectar si es una tarea desde el chat
    tarea_creada = detectar_tarea_en_chat(usuario_id, msg or '')
    if tarea_creada:
        response = f"Anotado. Tarea '{tarea_creada}' añadida a tu lista."
        save_mensaje(usuario_id, 'assistant', response)
        return jsonify({'response': response, 'conv_id': conv_id, 'ts': time.time()})

    history = get_mensajes_recientes(usuario_id, 20)
    response = responder(usuario_id, username, nombre, msg_completo, history, imagen_data, imagen_media_type)
    save_mensaje(usuario_id, 'assistant', response)
    return jsonify({'response': response, 'conv_id': conv_id})


def auto_cerrar_conversacion_inactiva(usuario_id):
    """Cierra automaticamente conversaciones activas con mas de 4h sin mensajes."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT id, ts_inicio FROM conversaciones
                     WHERE usuario_id=? AND activa=1""", (usuario_id,))
        conv = c.fetchone()
        if not conv:
            conn.close()
            return None
        conv_id, ts_inicio = conv
        # Check last message timestamp
        c.execute("""SELECT MAX(ts) FROM mensajes
                     WHERE usuario_id=? AND ts >= ?""", (usuario_id, ts_inicio))
        last_msg = c.fetchone()[0]
        conn.close()
        if not last_msg:
            return None
        horas_inactiva = (time.time() - last_msg) / 3600
        if horas_inactiva >= 4:
            print(f"[ANNI] Auto-cerrando conv #{conv_id} — {horas_inactiva:.1f}h inactiva")
            resumen = cerrar_conversacion(usuario_id, conv_id)
            if resumen:
                # Guardar resumen aprobado automaticamente y analizar
                conn2 = sqlite3.connect(DB_PATH)
                conn2.execute("UPDATE conversaciones SET activa=0, ts_fin=? WHERE id=?",
                              (time.time(), conv_id))
                conn2.commit()
                conn2.close()
                # Lanzar analisis en background
                conn3 = sqlite3.connect(DB_PATH)
                c3 = conn3.cursor()
                c3.execute("SELECT role, content FROM mensajes WHERE usuario_id=? AND ts >= ? ORDER BY ts ASC",
                           (usuario_id, ts_inicio))
                msgs_conv = c3.fetchall()
                conn3.close()
                if msgs_conv:
                    threading.Thread(target=analizar_conversacion, args=(usuario_id, msgs_conv), daemon=True).start()
            return conv_id
        return None
    except Exception as e:
        print(f"[ANNI] Error auto-cierre: {e}")
        return None

@app.route('/api/bienvenida')
@login_required
def api_bienvenida():
    """Presentacion en primer acceso o intervencion proactiva."""
    usuario_id = session['usuario_id']
    nombre = session.get('nombre', '')
    total = get_total_mensajes(usuario_id)

    # Auto-cerrar conversacion inactiva si lleva mas de 4h sin mensajes
    cerrada = auto_cerrar_conversacion_inactiva(usuario_id)
    if cerrada:
        print(f"[ANNI] Conversacion #{cerrada} auto-cerrada al abrir chat")

    if total == 0:
        # Primer acceso — ANNI se presenta y pregunta el nombre
        presentacion = generar_presentacion()
        save_mensaje(usuario_id, 'assistant', presentacion)
        # Guardar nombre como hito si ya lo tenemos del registro
        if nombre:
            conn_h = sqlite3.connect(DB_PATH)
            c_h = conn_h.cursor()
            c_h.execute("SELECT id FROM hitos_usuario WHERE usuario_id=? AND tipo='identidad'", (usuario_id,))
            if not c_h.fetchone():
                c_h.execute("INSERT INTO hitos_usuario (usuario_id, tipo, contenido, evidencia, peso) VALUES (?,?,?,?,?)",
                           (usuario_id, 'identidad', f'El usuario se llama {nombre}', 'Nombre dado al registrarse', 10))
                conn_h.commit()
            conn_h.close()
        return jsonify({'intervencion': presentacion, 'tipo': 'bienvenida'})

    # Accesos posteriores — voz proactiva
    intervencion = generar_intervencion_proactiva(usuario_id)
    return jsonify({'intervencion': intervencion, 'tipo': 'proactiva'})

@app.route('/api/historial')
@login_required
def api_historial():
    usuario_id = session['usuario_id']
    msgs = get_mensajes_recientes(usuario_id, 30)
    return jsonify({'messages': [{'role': r, 'content': c} for r, c in msgs]})

@app.route('/api/conv-activa')
@login_required
def api_conv_activa():
    usuario_id = session['usuario_id']
    conv = get_conversacion_activa(usuario_id)
    return jsonify({'id': conv[0] if conv else None})

@app.route('/api/conversacion/nueva', methods=['POST'])
@login_required
def api_nueva_conversacion():
    usuario_id = session['usuario_id']
    cid = nueva_conversacion(usuario_id)
    return jsonify({'ok': True, 'id': cid})

@app.route('/api/conversacion/cerrar', methods=['POST'])
@login_required
def api_cerrar_conversacion():
    """Genera resumen pero NO lo guarda — devuelve para que Rafa apruebe."""
    usuario_id = session['usuario_id']
    data = request.json or {}
    conv_id = data.get('id')
    if not conv_id:
        conv = get_conversacion_activa(usuario_id)
        if conv: conv_id = conv[0]
    if not conv_id:
        return jsonify({'ok': False, 'error': 'No hay conversacion activa'})
    # Generar resumen sin guardar
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT ts_inicio FROM conversaciones WHERE id=? AND usuario_id=?", (conv_id, usuario_id))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'ok': False, 'error': 'Conversacion no encontrada'})
    ts_inicio = row[0]
    conn2 = sqlite3.connect(DB_PATH)
    c2 = conn2.cursor()
    c2.execute("SELECT role, content FROM mensajes WHERE usuario_id=? AND ts >= ? ORDER BY ts ASC", (usuario_id, ts_inicio))
    msgs = c2.fetchall()
    conn2.close()
    if not msgs or len(msgs) < 2:
        # Cerrar sin resumen
        conn3 = sqlite3.connect(DB_PATH)
        conn3.execute("UPDATE conversaciones SET activa=0, ts_fin=? WHERE id=?", (time.time(), conv_id))
        conn3.commit()
        conn3.close()
        return jsonify({'ok': True, 'resumen': None, 'conv_id': conv_id})
    texto = "\n".join([f"{'Usuario' if r=='user' else 'ANNI'}: {m[:500]}" for r,m in msgs[-20:]])
    try:
        resp = together.chat.completions.create(
            model=CHAT_MODEL_FALLBACK,
            max_tokens=400,
            messages=[{"role": "user", "content": f"Resume esta conversacion en 3-5 frases. Incluye de que trato, que concluyo el usuario, y datos personales relevantes.\n\n{texto}\n\nResumen:"}]
        )
        resumen = resp.choices[0].message.content.strip()
    except:
        resumen = "Conversacion sin resumen."
    return jsonify({'ok': True, 'resumen': resumen, 'conv_id': conv_id, 'pendiente': True})

@app.route('/api/conversacion/guardar-resumen', methods=['POST'])
@login_required
def api_guardar_resumen():
    """Guarda el resumen aprobado/editado por Rafa."""
    usuario_id = session['usuario_id']
    data = request.json or {}
    conv_id = data.get('conv_id')
    resumen = data.get('resumen', '').strip()
    if not conv_id or not resumen:
        return jsonify({'ok': False})
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE conversaciones SET activa=0, ts_fin=?, resumen=? WHERE id=? AND usuario_id=?",
                 (time.time(), resumen, conv_id, usuario_id))
    conn.commit()
    conn.close()
    try:
        import struct
        resp = together.embeddings.create(model=EMBED_MODEL, input=[resumen[:1600]])
        vec = resp.data[0].embedding
        blob = struct.pack(f"{len(vec)}f", *vec)
        conn2 = sqlite3.connect(DB_PATH)
        conn2.execute("INSERT OR REPLACE INTO embeddings (usuario_id, tabla_origen, registro_id, embedding) VALUES (?,?,?,?)",
                      (usuario_id, 'conversaciones', conv_id, blob))
        conn2.commit()
        conn2.close()
        print(f"[ANNI] Embedding conversacion #{conv_id} guardado")
    except Exception as e:
        print(f"[ANNI] Error embedding conv #{conv_id}: {e}")
    # Analizar conversacion completa: extraer observaciones, personas y temas
    conn3 = sqlite3.connect(DB_PATH)
    c3 = conn3.cursor()
    c3.execute("SELECT ts_inicio FROM conversaciones WHERE id=?", (conv_id,))
    row3 = c3.fetchone()
    conn3.close()
    if row3:
        conn4 = sqlite3.connect(DB_PATH)
        c4 = conn4.cursor()
        c4.execute("SELECT role, content FROM mensajes WHERE usuario_id=? AND ts >= ? ORDER BY ts ASC",
                   (usuario_id, row3[0]))
        msgs_conv = c4.fetchall()
        conn4.close()
        if msgs_conv:
            threading.Thread(target=analizar_conversacion, args=(usuario_id, msgs_conv), daemon=True).start()
            print(f"[ANNI] analizar_conversacion disparado para conv #{conv_id} ({len(msgs_conv)} msgs)")
    return jsonify({'ok': True})

@app.route('/api/memoria')
@login_required
def api_memoria():
    """Vista de lo que ANNI sabe del usuario."""
    usuario_id = session['usuario_id']
    obs = get_observaciones_activas(usuario_id, 20)
    temas = get_temas_abiertos(usuario_id)
    personas = get_personas(usuario_id)
    return jsonify({
        'observaciones': [{'id': o[0], 'tipo': o[1], 'contenido': o[2], 'ts': ts_format(o[3]), 'peso': o[4]} for o in obs],
        'temas_abiertos': [{'id': t[0], 'tema': t[1], 'veces': t[3]} for t in temas],
        'personas': [{'id': p[0], 'nombre': p[1], 'relacion': p[2], 'tono': p[3]} for p in personas]
    })

@app.route('/api/cerrar-tema', methods=['POST'])
@login_required
def cerrar_tema():
    usuario_id = session['usuario_id']
    data = request.json or {}
    tema_id = data.get('id')
    if tema_id:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE temas_abiertos SET estado='cerrado' WHERE id=? AND usuario_id=?",
                     (tema_id, usuario_id))
        conn.commit()
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/observacion/<int:obs_id>', methods=['DELETE'])
@login_required
def delete_observacion(obs_id):
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    conn.execute('UPDATE observaciones SET activa=0 WHERE id=? AND usuario_id=?', (obs_id, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/persona/<int:persona_id>', methods=['DELETE'])
@login_required
def delete_persona(persona_id):
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM personas WHERE id=? AND usuario_id=?', (persona_id, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── HITOS USUARIO ────────────────────────────────────────────────────────────

@app.route('/api/hitos', methods=['GET'])
@login_required
def api_hitos():
    usuario_id = session['usuario_id']
    page = int(request.args.get('page', 1))
    per_page = 15
    offset = (page - 1) * per_page
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM hitos_usuario WHERE usuario_id=? AND activo=1", (usuario_id,))
    total = c.fetchone()[0]
    c.execute("""SELECT id, tipo, titulo, categoria, contenido, evidencia, peso, cuando_activarlo, como_usarlo, ts
        FROM hitos_usuario WHERE usuario_id=? AND activo=1 ORDER BY ts DESC LIMIT ? OFFSET ?""",
              (usuario_id, per_page, offset))
    hitos = [{'id': r[0], 'tipo': r[1], 'titulo': r[2] or '', 'categoria': r[3] or '', 'contenido': r[4],
              'evidencia': r[5] or '', 'peso': r[6], 'cuando': r[7] or '', 'como': r[8] or '', 'ts': ts_format(r[9])} for r in c.fetchall()]
    conn.close()
    return jsonify({'hitos': hitos, 'total': total, 'page': page, 'pages': (total + per_page - 1) // per_page})

@app.route('/api/hitos/aprobar', methods=['POST'])
@login_required
def api_aprobar_hito():
    usuario_id = session['usuario_id']
    data = request.json or {}
    contenido = data.get('contenido', '').strip()
    titulo = data.get('titulo', '').strip().upper()
    categoria = data.get('categoria', 'general')
    tipo = data.get('tipo', categoria)
    evidencia = data.get('evidencia', '')
    cuando = data.get('cuando_activarlo', '')
    como = data.get('como_usarlo', '')
    if not contenido:
        return jsonify({'ok': False})
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO hitos_usuario
        (usuario_id, tipo, titulo, categoria, contenido, evidencia, peso, cuando_activarlo, como_usarlo)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (usuario_id, tipo, titulo, categoria, contenido, evidencia, 5, cuando, como))
    hid = c.lastrowid
    conn.commit()
    conn.close()
    # Generar embedding en background
    texto_embed = f"{titulo} {contenido} {cuando}".strip()
    # Embedding síncrono — no daemon thread que puede morir antes de ejecutarse
    try:
        import struct
        resp = together.embeddings.create(model=EMBED_MODEL, input=[texto_embed[:1600]])
        vec = resp.data[0].embedding
        blob = struct.pack(f"{len(vec)}f", *vec)
        conn2 = sqlite3.connect(DB_PATH)
        conn2.execute("INSERT OR REPLACE INTO embeddings (usuario_id, tabla_origen, registro_id, embedding) VALUES (?,?,?,?)",
                      (usuario_id, 'hitos_usuario', hid, blob))
        conn2.commit()
        conn2.close()
        print(f"[ANNI] Embedding hito #{hid} guardado")
    except Exception as e:
        print(f"[ANNI] Error embedding hito #{hid}: {e}")
    return jsonify({'ok': True, 'id': hid})

@app.route('/api/hitos/<int:hid>', methods=['PUT'])
@login_required
def api_editar_hito(hid):
    usuario_id = session['usuario_id']
    data = request.json or {}
    contenido = data.get('contenido', '').strip()
    if not contenido:
        return jsonify({'ok': False})
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE hitos_usuario SET contenido=? WHERE id=? AND usuario_id=?", (contenido, hid, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/hitos/<int:hid>', methods=['DELETE'])
@login_required
def api_borrar_hito(hid):
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE hitos_usuario SET activo=0 WHERE id=? AND usuario_id=?", (hid, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── CHATS ─────────────────────────────────────────────────────────────────────

@app.route('/api/chats/<int:cid>', methods=['PUT'])
@login_required
def api_editar_chat(cid):
    usuario_id = session['usuario_id']
    data = request.json or {}
    resumen = data.get('resumen', '').strip()
    if not resumen:
        return jsonify({'ok': False})
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE conversaciones SET resumen=? WHERE id=? AND usuario_id=?",
                 (resumen, cid, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/chats', methods=['GET'])
@login_required
def api_chats():
    usuario_id = session['usuario_id']
    page = int(request.args.get('page', 1))
    per_page = 10
    offset = (page - 1) * per_page
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM conversaciones WHERE usuario_id=? AND resumen IS NOT NULL AND resumen != '[Descartado]'", (usuario_id,))
    total = c.fetchone()[0]
    c.execute("SELECT id, ts_inicio, ts_fin, resumen FROM conversaciones WHERE usuario_id=? AND resumen IS NOT NULL AND resumen != '[Descartado]' ORDER BY ts_inicio DESC LIMIT ? OFFSET ?",
              (usuario_id, per_page, offset))
    chats = [{'id': r[0], 'inicio': ts_format(r[1]), 'fin': ts_format(r[2]), 'resumen': r[3]} for r in c.fetchall()]
    conn.close()
    return jsonify({'chats': chats, 'total': total, 'page': page, 'pages': (total + per_page - 1) // per_page})

# ── DIARIO ────────────────────────────────────────────────────────────────────

FECHA_INICIO_EXPERIMENTO = "2026-03-01"

@app.route('/api/diario', methods=['GET'])
@login_required
def api_diario_get():
    usuario_id = session['usuario_id']
    page = int(request.args.get('page', 1))
    per_page = 10
    offset = (page - 1) * per_page
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM diario WHERE usuario_id=?", (usuario_id,))
    total = c.fetchone()[0]
    orden = request.args.get('orden', 'desc')
    order_sql = 'DESC' if orden == 'desc' else 'ASC'
    c.execute(f"SELECT id, fecha, dia_experimento, titulo, texto, ts FROM diario WHERE usuario_id=? ORDER BY fecha {order_sql} LIMIT ? OFFSET ?",
              (usuario_id, per_page, offset))
    entradas = [{'id': r[0], 'fecha': r[1], 'dia': r[2], 'titulo': r[3], 'texto': r[4], 'ts': ts_format(r[5])} for r in c.fetchall()]
    conn.close()
    return jsonify({'entradas': entradas, 'total': total, 'page': page, 'pages': (total + per_page - 1) // per_page})

@app.route('/api/diario', methods=['POST'])
@login_required
def api_diario_post():
    usuario_id = session['usuario_id']
    data = request.json or {}
    fecha = data.get('fecha', '').strip()
    titulo = data.get('titulo', '').strip()
    texto = data.get('texto', '').strip()
    if not fecha or not titulo or not texto:
        return jsonify({'ok': False, 'error': 'Faltan campos'})
    from datetime import date as ddate
    try:
        f = ddate.fromisoformat(fecha)
        inicio = ddate.fromisoformat(FECHA_INICIO_EXPERIMENTO)
        dia = (f - inicio).days + 1
    except:
        dia = None
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO diario (usuario_id, fecha, dia_experimento, titulo, texto) VALUES (?,?,?,?,?)",
              (usuario_id, fecha, dia, titulo, texto))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'dia': dia})

@app.route('/api/diario/<int:eid>', methods=['PUT'])
@login_required
def api_diario_put(eid):
    usuario_id = session['usuario_id']
    data = request.json or {}
    titulo = data.get('titulo', '').strip()
    texto = data.get('texto', '').strip()
    fecha = data.get('fecha', '').strip()
    if not titulo or not texto:
        return jsonify({'ok': False})
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE diario SET titulo=?, texto=?, fecha=? WHERE id=? AND usuario_id=?",
                 (titulo, texto, fecha, eid, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/diario/<int:eid>', methods=['DELETE'])
@login_required
def api_diario_delete(eid):
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM diario WHERE id=? AND usuario_id=?", (eid, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── DESCARGA BD ───────────────────────────────────────────────────────────────

@app.route('/api/descargar-bd', methods=['POST'])
@login_required
def api_descargar_bd():
    usuario_id = session['usuario_id']
    data = request.json or {}
    password = data.get('password', '').strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT password_hash FROM usuarios WHERE id=?", (usuario_id,))
    row = c.fetchone()
    conn.close()
    if not row or not verificar_password(password, row[0]):
        return jsonify({'ok': False, 'error': 'Contrasena incorrecta'})
    from flask import send_file
    return send_file(DB_PATH, as_attachment=True, download_name='anni_backup.db', mimetype='application/octet-stream')

# ── DETECTAR HITO ────────────────────────────────────────────────────────────

@app.route('/api/detectar-hito', methods=['POST'])
@login_required
def api_detectar_hito():
    """Analiza el ultimo intercambio buscando un hito importante sobre el usuario."""
    usuario_id = session['usuario_id']
    data = request.json or {}
    mensaje = data.get('mensaje', '').strip()
    respuesta = data.get('respuesta', '').strip()
    if not mensaje or len(mensaje) < 8:
        return jsonify({'hito': None})

    prompt = f"""Analiza este intercambio y determina si contiene informacion importante y duradera sobre el usuario.

Usuario: "{mensaje}"
ANNI: "{respuesta[:300]}"

Un hito vale la pena recordar cuando revela: quien es el usuario, como piensa, personas importantes en su vida (nombres y relacion), patrones de comportamiento, valores, decisiones relevantes, miedos, motivaciones.

IMPORTANTE: Si el usuario menciona el nombre de una persona cercana (esposa, hijo, amigo, socio) ESO ES UN HITO de tipo "relacion".

Si SI hay un hito, responde SOLO con este JSON (sin markdown):
{{
  "hito": true,
  "titulo": "TITULO EN MAYUSCULAS CORTO",
  "categoria": "forma_de_pensar|toma_de_decisiones|lo_que_importa|energia|relacion|identidad|general",
  "contenido": "descripcion concisa de lo que revela este hito sobre el usuario",
  "evidencia": "frase exacta del usuario que lo demuestra",
  "cuando_activarlo": "en que situaciones ANNI deberia usar este hito",
  "como_usarlo": "como deberia actuar ANNI cuando detecte esta situacion"
}}

Si NO hay hito relevante, responde SOLO con:
{{"hito": false}}

Solo JSON."""

    try:
        resp = together.chat.completions.create(
            model=CHAT_MODEL_FALLBACK,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
        parsed = json.loads(raw)
        if not parsed.get('hito'):
            return jsonify({'hito': None})

        # Verificar duplicados con embeddings antes de proponer
        contenido_nuevo = parsed.get('contenido', '')
        if contenido_nuevo:
            try:
                import struct, math
                resp_emb = together.embeddings.create(model=EMBED_MODEL, input=[contenido_nuevo[:1600]])
                vec_nuevo = resp_emb.data[0].embedding

                conn_dup = sqlite3.connect(DB_PATH)
                c_dup = conn_dup.cursor()
                # Hitos CON embedding
                c_dup.execute("""SELECT h.contenido, e.embedding
                    FROM hitos_usuario h
                    JOIN embeddings e ON e.tabla_origen='hitos_usuario' AND e.registro_id=h.id
                    WHERE h.usuario_id=? AND h.activo=1""", (usuario_id,))
                existentes_con_emb = c_dup.fetchall()
                # Hitos SIN embedding (fallback textual)
                c_dup.execute("""SELECT h.titulo, h.contenido FROM hitos_usuario h
                    WHERE h.usuario_id=? AND h.activo=1
                    AND h.id NOT IN (
                        SELECT registro_id FROM embeddings WHERE tabla_origen='hitos_usuario'
                    )""", (usuario_id,))
                existentes_sin_emb = c_dup.fetchall()
                conn_dup.close()

                titulo_nuevo = parsed.get('titulo', '').lower()

                # Fallback textual: comparar titulo y keywords del contenido
                for titulo_exist, contenido_exist in existentes_sin_emb:
                    t = (titulo_exist or '').lower()
                    c = (contenido_exist or '').lower()
                    # Extraer palabras clave del nuevo (>4 chars)
                    palabras = [w for w in contenido_nuevo.lower().split() if len(w) > 4]
                    matches = sum(1 for p in palabras if p in c or p in t)
                    if matches >= 3 or (titulo_nuevo and titulo_nuevo[:20] in t):
                        print(f"[ANNI] Hito duplicado textual descartado: {contenido_nuevo[:50]}")
                        return jsonify({'hito': None})

                # Comparacion por embeddings
                for contenido_existente, blob in existentes_con_emb:
                    nv = len(blob) // 4
                    vec_exist = struct.unpack(f"{nv}f", blob)
                    dot = sum(a*b for a,b in zip(vec_nuevo, vec_exist))
                    mag1 = math.sqrt(sum(a*a for a in vec_nuevo))
                    mag2 = math.sqrt(sum(b*b for b in vec_exist))
                    sim = dot / (mag1 * mag2) if mag1 and mag2 else 0
                    if sim > 0.78:
                        print(f"[ANNI] Hito duplicado por embedding descartado (sim={sim:.2f}): {contenido_nuevo[:50]}")
                        return jsonify({'hito': None})
            except Exception as e_dup:
                print(f"[ANNI] Error verificando duplicados: {e_dup}")
                # Si falla la verificación, proponer igualmente

        return jsonify({'hito': parsed})
    except Exception as e:
        print(f"[ANNI] detectar-hito error: {e}")
        return jsonify({'hito': None})


# ── TAREAS ────────────────────────────────────────────────────────────────────

@app.route('/api/tareas', methods=['GET'])
@login_required
def api_get_tareas():
    usuario_id = session['usuario_id']
    estado = request.args.get('estado', 'activas')
    tareas = get_tareas(usuario_id, estado)
    return jsonify({'tareas': [
        {'id': t[0], 'titulo': t[1], 'descripcion': t[2], 'cliente': t[3],
         'due_date': t[4], 'estado': t[5], 'veces_mencionada': t[6],
         'ts_creacion': ts_format(t[7]), 'ts_actualizacion': ts_format(t[8])}
        for t in tareas
    ]})

@app.route('/api/tareas', methods=['POST'])
@login_required
def api_crear_tarea():
    usuario_id = session['usuario_id']
    data = request.json or {}
    titulo = data.get('titulo', '').strip()
    if not titulo:
        return jsonify({'ok': False, 'error': 'Título requerido'})
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO tareas (usuario_id, titulo, descripcion, cliente, due_date)
                 VALUES (?,?,?,?,?)""",
              (usuario_id, titulo, data.get('descripcion', ''),
               data.get('cliente', ''), data.get('due_date') or None))
    tarea_id = c.lastrowid
    conn.commit()
    conn.close()
    print(f"[ANNI] Tarea #{tarea_id} creada: {titulo}")
    return jsonify({'ok': True, 'id': tarea_id})

@app.route('/api/tareas/<int:tarea_id>', methods=['PUT'])
@login_required
def api_editar_tarea(tarea_id):
    usuario_id = session['usuario_id']
    data = request.json or {}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    campos = []
    valores = []
    for campo in ['titulo', 'descripcion', 'cliente', 'due_date', 'estado']:
        if campo in data:
            campos.append(f"{campo}=?")
            valores.append(data[campo])
    if not campos:
        conn.close()
        return jsonify({'ok': False})
    valores.append(time.time())
    campos.append("ts_actualizacion=?")
    if data.get('estado') == 'completada':
        campos.append("ts_completada=?")
        valores.append(time.time())
    valores += [tarea_id, usuario_id]
    c.execute(f"UPDATE tareas SET {', '.join(campos)} WHERE id=? AND usuario_id=?", valores)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/tareas/<int:tarea_id>', methods=['DELETE'])
@login_required
def api_borrar_tarea(tarea_id):
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM tareas WHERE id=? AND usuario_id=?", (tarea_id, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── HTML ──────────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang='es'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>ANNI</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:#fff;color:#111;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.wrap{width:100%;max-width:480px}
.logo{font-size:72px;font-weight:900;color:#cc0000;letter-spacing:-3px;line-height:1;margin-bottom:6px}
.ver{font-size:15px;color:#555;margin-bottom:4px}
.cred{font-size:13px;color:#999;margin-bottom:48px}
.card{background:#f5f5f5;border:1px solid #e0e0e0;border-radius:16px;padding:32px}
.err{background:#fff0f0;border:2px solid #cc0000;border-radius:10px;padding:14px 16px;font-size:16px;color:#cc0000;margin-bottom:20px;display:none;font-weight:500}
label{display:block;font-size:15px;font-weight:700;color:#111;margin-bottom:10px;margin-top:24px}
label:first-of-type{margin-top:0}
input{width:100%;background:#fff;border:2px solid #e0e0e0;border-radius:10px;padding:18px;color:#111;font-size:18px;outline:none;transition:border-color .2s;-webkit-appearance:none}
input:focus{border-color:#cc0000}
.btn{width:100%;background:#cc0000;color:#fff;border:none;border-radius:10px;padding:20px;font-size:18px;font-weight:700;cursor:pointer;margin-top:28px;-webkit-appearance:none}
.btn:active{background:#aa0000}
.lnk{text-align:center;margin-top:24px;font-size:15px;color:#555}
.lnk a{color:#cc0000;text-decoration:none;font-weight:700}
</style>
</head>
<body>
<div class='wrap'>
<div class='logo'>ANNI</div>
<div class='ver'>v__ANNI_VERSION__</div>
<div class='cred'>Created by Rafa Torrijos</div>
<div class='card'>
<div class='err' id='err'></div>
<label for='u'>Email</label>
<input type='email' id='u' placeholder='tu@email.com' autocomplete='email' autocapitalize='none' inputmode='email'>
<label for='p'>Contrasena</label>
<input type='password' id='p' placeholder='tu contrasena' autocomplete='current-password'>
<button class='btn' onclick='go()'>ENTRAR</button>
<div class='lnk'>Primera vez? <a href='/registro'>Crear cuenta</a></div>
</div>
</div>
<script>
function go(){
var u=document.getElementById('u').value.trim();
var p=document.getElementById('p').value.trim();
var e=document.getElementById('err');e.style.display='none';
fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})})
.then(r=>r.json()).then(d=>{
if(d.ok)window.location.href='/chat';
else{e.textContent=d.error;e.style.display='block';}});}
document.addEventListener('keydown',e=>{if(e.key==='Enter')go();});
</script>
</body></html>"""

REGISTRO_HTML = """<!DOCTYPE html>
<html lang='es'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>ANNI</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:#fff;color:#111;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.wrap{width:100%;max-width:480px}
.logo{font-size:72px;font-weight:900;color:#cc0000;letter-spacing:-3px;line-height:1;margin-bottom:6px}
.ver{font-size:15px;color:#555;margin-bottom:4px}
.cred{font-size:13px;color:#999;margin-bottom:48px}
.card{background:#f5f5f5;border:1px solid #e0e0e0;border-radius:16px;padding:32px}
.err{background:#fff0f0;border:2px solid #cc0000;border-radius:10px;padding:14px 16px;font-size:16px;color:#cc0000;margin-bottom:20px;display:none;font-weight:500}
label{display:block;font-size:15px;font-weight:700;color:#111;margin-bottom:10px;margin-top:24px}
label:first-of-type{margin-top:0}
input{width:100%;background:#fff;border:2px solid #e0e0e0;border-radius:10px;padding:18px;color:#111;font-size:18px;outline:none;transition:border-color .2s;-webkit-appearance:none}
input:focus{border-color:#cc0000}
.btn{width:100%;background:#cc0000;color:#fff;border:none;border-radius:10px;padding:20px;font-size:18px;font-weight:700;cursor:pointer;margin-top:28px;-webkit-appearance:none}
.btn:active{background:#aa0000}
.lnk{text-align:center;margin-top:24px;font-size:15px;color:#555}
.lnk a{color:#cc0000;text-decoration:none;font-weight:700}
</style>
</head>
<body>
<div class='wrap'>
<div class='logo'>ANNI</div>
<div class='ver'>v__ANNI_VERSION__</div>
<div class='cred'>Created by Rafa Torrijos</div>
<div class='card'>
<div class='err' id='err'></div>
<label for='n'>Como te llamas</label>
<input type='text' id='n' placeholder='tu nombre' autocomplete='given-name' autocapitalize='words'>
<label for='u'>Tu email</label>
<input type='email' id='u' placeholder='tu@email.com' autocomplete='email' autocapitalize='none' inputmode='email'>
<label for='p'>Contrasena</label>
<input type='password' id='p' placeholder='minimo 6 caracteres' autocomplete='new-password'>
<button class='btn' onclick='go()'>CREAR CUENTA</button>
<div class='lnk'>Ya tienes cuenta? <a href='/login'>Entra aqui</a></div>
</div>
</div>
<script>
function go(){
var n=document.getElementById('n').value.trim();
var u=document.getElementById('u').value.trim();
var p=document.getElementById('p').value.trim();
var e=document.getElementById('err');e.style.display='none';
fetch('/registro',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nombre:n,username:u,password:p})})
.then(r=>r.json()).then(d=>{
if(d.ok)window.location.href='/chat';
else{e.textContent=d.error;e.style.display='block';}});}
document.addEventListener('keydown',e=>{if(e.key==='Enter')go();});
</script>
</body></html>"""



CHAT_HTML = """<!DOCTYPE html>
<html lang='es'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0, maximum-scale=1.0'>
<title>ANNI</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{background:#fff;color:#111;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;display:flex;flex-direction:column}

/* BARRA NAV */
#nav{background:#fff;border-bottom:1px solid #e8e8e8;padding:8px 16px;display:flex;align-items:center;gap:8px;flex-shrink:0}
.nav-btn{font-size:12px;font-weight:700;color:#555;background:#fff;border:1px solid #d0d0d0;border-radius:6px;padding:6px 12px;cursor:pointer;-webkit-appearance:none;letter-spacing:0.5px}
.nav-btn:hover{background:#f5f5f5}
.nav-btn.salir{color:#cc0000;border-color:#ffcccc;margin-left:auto}

/* HEADER CENTRADO */
#hdr{padding:20px 16px 16px;text-align:center;flex-shrink:0;background:#fff}
.logo{font-size:52px;font-weight:900;color:#cc0000;letter-spacing:-2px;line-height:1}
.ver{font-size:14px;font-weight:700;color:#cc0000;margin-top:4px;letter-spacing:1px}
.sub{font-size:12px;color:#aaa;margin-top:4px;line-height:1.5}

/* BARRA EMPEZAR */
#conv-bar{background:#555;padding:10px 20px;display:flex;align-items:center;justify-content:center;flex-shrink:0;gap:16px}
#btn-conv{font-size:14px;font-weight:700;background:none;border:2px solid;border-radius:8px;padding:8px 32px;cursor:pointer;-webkit-appearance:none;letter-spacing:0.5px;transition:all .2s}
#btn-conv.verde{color:#44cc44;border-color:#44cc44}
#btn-conv.rojo{color:#fff;border-color:#fff;background:rgba(204,0,0,0.8)}
.conv-info{font-size:12px;color:#ccc}

/* CHAT */
#chat{flex:1;overflow-y:auto;padding:20px 20px;display:flex;flex-direction:column;gap:20px;-webkit-overflow-scrolling:touch;max-width:720px;width:100%;margin:0 auto;align-self:center}
.msg-anni{display:flex;flex-direction:column;gap:4px;align-self:flex-start;max-width:90%}
.msg-anni .lbl{font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#bbb}
.msg-anni .txt{font-size:17px;line-height:1.75;color:#111;white-space:pre-wrap;word-break:break-word}
.msg-user{display:flex;flex-direction:column;gap:4px;align-self:flex-end;max-width:85%;align-items:flex-end}
.msg-user .lbl{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#cc0000}
.msg-user .burbuja{background:#cc0000;color:#fff;border-radius:16px 16px 4px 16px;padding:14px 18px;font-size:17px;line-height:1.65;white-space:pre-wrap;word-break:break-word}
.pro{background:#fff5f5;border-left:4px solid #cc0000;padding:14px 18px;font-size:17px;color:#111;line-height:1.7;font-weight:500;max-width:90%}
.resumen-msg{background:#f0fff0;border-left:4px solid #88bb88;padding:14px 18px;font-size:15px;color:#335533;line-height:1.6;max-width:90%}
.typing{display:flex;gap:6px;align-items:center;padding:8px 0}
.typing span{width:8px;height:8px;background:#ccc;border-radius:50%;animation:b 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes b{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}

/* INPUT */
#ia{border-top:1px solid #e8e8e8;padding:12px 16px;padding-bottom:max(12px,env(safe-area-inset-bottom));flex-shrink:0;background:#fff}
#preview{max-width:720px;margin:0 auto 6px;font-size:13px;color:#cc0000;display:none}
.ir{display:flex;gap:8px;align-items:flex-end;max-width:720px;margin:0 auto}
.clip{background:none;border:1px solid #e0e0e0;border-radius:10px;padding:12px 13px;cursor:pointer;flex-shrink:0;font-size:15px;color:#888;-webkit-appearance:none}
.clip:active{background:#f5f5f5}
textarea{flex:1;background:#f5f5f5;border:1px solid #e0e0e0;border-radius:12px;padding:13px 15px;color:#111;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;font-size:17px;resize:none;outline:none;line-height:1.5;max-height:140px;-webkit-appearance:none;transition:border-color .2s}
textarea:focus{border-color:#cc0000}
textarea::placeholder{color:#bbb}
button#s{background:#cc0000;color:#fff;border:none;border-radius:10px;padding:13px 18px;font-size:16px;font-weight:700;cursor:pointer;flex-shrink:0;-webkit-appearance:none;min-width:76px}
button#s:active{background:#aa0000}
button#s:disabled{background:#ddd;cursor:not-allowed}
#finput{display:none}

/* MODAL */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;align-items:center;justify-content:center;padding:20px}
.modal-bg.open{display:flex}
.modal{background:#fff;border-radius:16px;padding:28px;width:100%;max-width:560px;max-height:90vh;overflow-y:auto}
.modal h2{font-size:20px;font-weight:800;margin-bottom:16px;color:#111}
.modal textarea,.modal input[type=password],.modal input[type=text]{width:100%;border:2px solid #e0e0e0;border-radius:10px;padding:12px 14px;font-size:15px;line-height:1.6;outline:none;font-family:inherit}
.modal textarea{resize:vertical;min-height:100px}
.modal textarea:focus,.modal input:focus{border-color:#cc0000}
.modal-label{font-size:12px;font-weight:700;color:#555;display:block;margin:12px 0 4px}
.modal-label:first-child{margin-top:0}
.modal select{width:100%;border:2px solid #e0e0e0;border-radius:10px;padding:12px 14px;font-size:15px;outline:none;font-family:inherit}
.modal select:focus{border-color:#cc0000}
.modal-btns{display:flex;gap:10px;margin-top:16px;justify-content:flex-end;flex-wrap:wrap}
.modal-btns button{padding:10px 18px;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;border:2px solid;-webkit-appearance:none}
.btn-ok{background:#cc0000;color:#fff;border-color:#cc0000}
.btn-cancel{background:#fff;color:#555;border-color:#e0e0e0}
.btn-descartar{background:#fff;color:#888;border-color:#e0e0e0}

/* PÁGINA */
#page{display:none;position:fixed;inset:0;background:#fff;z-index:900;flex-direction:column}
#page.open{display:flex}
.page-header{padding:16px 20px;border-bottom:2px solid #e8e8e8;display:flex;align-items:center;gap:16px;flex-shrink:0}
.page-header h1{font-size:22px;font-weight:900;color:#111;flex:1}
.page-close{font-size:14px;font-weight:700;color:#cc0000;cursor:pointer;padding:8px 14px;border:2px solid #ffcccc;border-radius:8px;background:none;flex-shrink:0;order:-1}
.page-body{flex:1;overflow-y:auto;padding:20px;max-width:760px;width:100%;margin:0 auto}
.item-card{border:1px solid #e8e8e8;border-radius:12px;padding:16px;margin-bottom:14px}
.item-meta{font-size:12px;color:#999;margin-bottom:6px}
.item-content{font-size:15px;line-height:1.6;color:#111}
.item-actions{display:flex;gap:8px;margin-top:10px}
.btn-edit,.btn-del{font-size:12px;font-weight:700;padding:6px 12px;border-radius:6px;cursor:pointer;border:2px solid;-webkit-appearance:none;background:none}
.btn-edit{color:#555;border-color:#e0e0e0}
.btn-del{color:#cc0000;border-color:#ffcccc}
.pager{display:flex;gap:8px;justify-content:center;margin-top:20px;flex-wrap:wrap}
.pager button{padding:8px 14px;border:2px solid #e0e0e0;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;background:#fff;color:#555}
.pager button.active{background:#cc0000;color:#fff;border-color:#cc0000}
.form-group{margin-bottom:14px}
.form-group label{display:block;font-size:13px;font-weight:700;margin-bottom:6px;color:#555}
.form-group input,.form-group textarea{width:100%;border:2px solid #e0e0e0;border-radius:10px;padding:12px 14px;font-size:15px;outline:none;font-family:inherit}
.form-group input:focus,.form-group textarea:focus{border-color:#cc0000}
.dia-badge{background:#cc0000;color:#fff;font-size:11px;font-weight:700;padding:3px 8px;border-radius:6px;display:inline-block;margin-bottom:6px}

@media(max-width:600px){
#nav{padding:8px 12px;gap:6px}.nav-btn{font-size:11px;padding:5px 9px}
.logo{font-size:40px}
#chat{padding:16px;gap:16px}
.msg-anni .txt,.msg-user .burbuja,.pro{font-size:16px}
textarea{font-size:16px}}
</style>
</head>
<body>

<!-- BARRA NAV -->
<div id='nav'>
  <button class='nav-btn' onclick='showPage("tareas")'>TAREAS</button>
  <button class='nav-btn' onclick='showPage("hitos")'>HITOS</button>
  <button class='nav-btn' onclick='showPage("chats")'>CHATS</button>
  <button class='nav-btn' onclick='showPage("memoria")'>MEMORIA</button>
  <button class='nav-btn' onclick='showPage("diario")'>DIARIO</button>
  <button class='nav-btn' onclick='descargarBD()'>BD</button>
  <a href='/logout' class='nav-btn salir'>SALIR</a>
</div>

<!-- HEADER CENTRADO -->
<div id='hdr'>
  <div class='logo'>ANNI</div>
  <div class='ver'>V. __ANNI_VERSION__</div>
  <div class='sub'>I.A. con memoria persistente<br>creada por Rafa Torrijos</div>
</div>

<!-- BARRA EMPEZAR/CERRAR -->
<div id='conv-bar'>
  <span class='conv-info' id='conv-info'></span>
  <button id='btn-conv' class='verde' onclick='toggleConv()'>Empezar Conversacion</button>
</div>

<div id='chat'></div>

<div id='ia'>
  <div id='preview'></div>
  <div class='ir'>
    <button class='clip' onclick='document.getElementById("finput").click()' title='Adjuntar'>[+]</button>
    <input type='file' id='finput' accept='image/*,.pdf,.txt,.doc,.docx' onchange='archivoSel(this)'>
    <textarea id='inp' placeholder='Habla con Anni...' rows='1'></textarea>
    <button id='s' onclick='env()'>Enviar</button>
  </div>
</div>

<!-- MODAL RESUMEN -->
<div class='modal-bg' id='modal-resumen'>
<div class='modal'>
<h2>Resumen de la conversacion</h2>
<p style='font-size:14px;color:#888;margin-bottom:12px'>Revisa y edita antes de guardar.</p>
<textarea id='resumen-txt' rows='6'></textarea>
<div class='modal-btns'>
<button class='btn-descartar' onclick='descartarResumen()'>Descartar</button>
<button class='btn-ok' onclick='guardarResumen()'>Guardar</button>
</div>
</div>
</div>

<!-- MODAL HITO -->
<div class='modal-bg' id='modal-hito'>
<div class='modal'>
<h2>Nuevo hito detectado</h2>
<p style='font-size:12px;color:#888;font-style:italic;margin-bottom:14px' id='hito-evidencia'></p>
<span class='modal-label'>TITULO</span>
<input type='text' id='hito-titulo'>
<span class='modal-label'>CATEGORIA</span>
<select id='hito-cat'>
<option value='forma_de_pensar'>Forma de pensar</option>
<option value='toma_de_decisiones'>Toma de decisiones</option>
<option value='lo_que_importa'>Lo que importa</option>
<option value='energia'>Energia</option>
<option value='relacion'>Relacion</option>
<option value='identidad'>Identidad</option>
<option value='general'>General</option>
</select>
<span class='modal-label'>DESCRIPCION</span>
<textarea id='hito-txt' rows='3'></textarea>
<span class='modal-label'>CUANDO ACTIVARLO</span>
<input type='text' id='hito-cuando' placeholder='En que situaciones usar este hito'>
<span class='modal-label'>COMO USARLO</span>
<input type='text' id='hito-como' placeholder='Como deberia actuar ANNI'>
<div class='modal-btns'>
<button class='btn-cancel' onclick='rechazarHito()'>No guardar</button>
<button class='btn-ok' onclick='aprobarHito()'>Guardar hito</button>
</div>
</div>
</div>

<!-- MODAL BD -->
<div class='modal-bg' id='modal-bd'>
<div class='modal'>
<h2>Descargar base de datos</h2>
<p style='font-size:14px;color:#888;margin-bottom:12px'>Introduce tu contrasena para confirmar.</p>
<input type='password' id='bd-pwd' placeholder='tu contrasena'>
<div id='bd-err' style='color:#cc0000;font-size:13px;margin-top:8px;display:none'></div>
<div class='modal-btns'>
<button class='btn-cancel' onclick='closeMod("modal-bd")'>Cancelar</button>
<button class='btn-ok' onclick='confirmarDescarga()'>Descargar</button>
</div>
</div>
</div>

<!-- PÁGINA -->
<div id='page'>
<div class='page-header'>
<h1 id='page-title'>Hitos</h1>
<button class='page-close' onclick='closePage()'>Inicio</button>
</div>
<div class='page-body' id='page-body'></div>
</div>

<script>
var C=document.getElementById('chat');
var I=document.getElementById('inp');
var S=document.getElementById('s');
var PRV=document.getElementById('preview');
var BTNCONV=document.getElementById('btn-conv');
var CONVINFO=document.getElementById('conv-info');
var convActiva=null;var convNum=0;
var pendResumen=null;var pendHito=null;
var currentPage=1;var currentSection='';
var NOMBRE='__NOMBRE_USUARIO__';

function ts(){
var d=new Date();
var dd=String(d.getDate()).padStart(2,'0');
var mm=String(d.getMonth()+1).padStart(2,'0');
var yy=d.getFullYear();
var hh=String(d.getHours()).padStart(2,'0');
var mi=String(d.getMinutes()).padStart(2,'0');
return dd+'/'+mm+'/'+yy+' '+hh+':'+mi;}

function updateBtn(){
if(convActiva){
BTNCONV.textContent='Cerrar Conversacion';
BTNCONV.className='rojo';
CONVINFO.textContent='Chat #'+convNum+' en curso';
}else{
BTNCONV.textContent='Empezar Conversacion';
BTNCONV.className='verde';
CONVINFO.textContent='';}}

function toggleConv(){if(convActiva){cerrarConv();}else{nuevaConv();}}

function tsFromServer(serverTs){
if(!serverTs) return ts();
var d=new Date(serverTs*1000);
var dd=String(d.getDate()).padStart(2,'0');
var mm=String(d.getMonth()+1).padStart(2,'0');
var yy=d.getFullYear();
var hh=String(d.getHours()).padStart(2,'0');
var mi=String(d.getMinutes()).padStart(2,'0');
return dd+'/'+mm+'/'+yy+' '+hh+':'+mi;}

function add(role,txt,tipo,serverTs){
var d=document.createElement('div');
var hora=serverTs?tsFromServer(serverTs):ts();
if(tipo==='pro'||tipo==='bienvenida'){
d.className='pro';d.textContent=txt;
}else if(tipo==='resumen'){
d.className='resumen-msg';d.textContent='Resumen: '+txt;
}else if(role==='user'){
d.className='msg-user';
var lbl=document.createElement('div');lbl.className='lbl';
lbl.textContent=NOMBRE.toUpperCase()+' - '+hora;
var bur=document.createElement('div');bur.className='burbuja';bur.textContent=txt;
d.appendChild(lbl);d.appendChild(bur);
}else{
d.className='msg-anni';
var lbl=document.createElement('div');lbl.className='lbl';lbl.textContent='ANNI - '+hora;
var t=document.createElement('div');t.className='txt';t.textContent=txt;
d.appendChild(lbl);d.appendChild(t);}
C.appendChild(d);C.scrollTop=C.scrollHeight;return d;}

function typing(){
var d=document.createElement('div');d.className='msg-anni';d.id='ty';
var t=document.createElement('div');t.className='typing';
t.innerHTML='<span></span><span></span><span></span>';
d.appendChild(t);C.appendChild(d);C.scrollTop=C.scrollHeight;}
function rmtyp(){var t=document.getElementById('ty');if(t)t.remove();}

var archivoData=null;
function archivoSel(input){
var f=input.files[0];if(!f)return;
PRV.style.display='block';PRV.textContent='Adjunto: '+f.name;
var reader=new FileReader();
if(f.type.startsWith('image/')){
reader.onload=function(e){archivoData={tipo:'imagen',data:e.target.result,nombre:f.name};};
reader.readAsDataURL(f);
}else{
reader.onload=function(e){archivoData={tipo:'texto',data:e.target.result,nombre:f.name};};
reader.readAsText(f);}}

function env(){
var msg=I.value.trim();
if(!msg&&!archivoData)return;
var disp=msg+(archivoData?' ['+archivoData.nombre+']':'');
I.value='';I.style.height='auto';S.disabled=true;
var userTs=Math.floor(Date.now()/1000);add('user',disp,null,userTs);PRV.style.display='none';typing();
var body={message:msg};
if(archivoData){body.archivo=archivoData;}
var lastMsg=msg;
archivoData=null;document.getElementById('finput').value='';
fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
.then(r=>r.json()).then(d=>{
rmtyp();var resp=d.response||'';add('anni',resp,null,d.ts);
if(d.conv_id){if(!convActiva||convActiva!==d.conv_id){convActiva=d.conv_id;convNum=d.conv_id;updateBtn();}}
S.disabled=false;I.focus();
if(lastMsg&&resp){
fetch('/api/detectar-hito',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({mensaje:lastMsg,respuesta:resp})})
.then(r=>r.json()).then(h=>{if(h.hito&&h.hito.hito)mostrarModalHito(h.hito);})
.catch(()=>{});}})
.catch(e=>{rmtyp();add('anni','Error de conexion.');S.disabled=false;});}

function nuevaConv(){
fetch('/api/conversacion/nueva',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
.then(r=>r.json()).then(d=>{
if(d.ok){convActiva=d.id;convNum=d.id;updateBtn();C.innerHTML='';
add('anni','Conversacion nueva. De que quieres hablar?');}});}

function cerrarConv(){
// Si no tenemos conv_id local, mandamos sin id — el backend busca la activa
var body=convActiva?{id:convActiva}:{};
fetch('/api/conversacion/cerrar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
.then(r=>r.json()).then(d=>{
if(d.ok&&d.resumen&&d.pendiente){
var cid=d.conv_id||convActiva;
pendResumen={conv_id:cid,resumen:d.resumen};
document.getElementById('resumen-txt').value=d.resumen;
document.getElementById('modal-resumen').classList.add('open');
}else if(d.ok){
convActiva=null;updateBtn();add('anni','Conversacion cerrada.');}
else{add('anni','No hay conversacion activa para cerrar.');}})
.catch(e=>{add('anni','Error al cerrar la conversacion.');});}

function guardarResumen(){
var txt=document.getElementById('resumen-txt').value.trim();
if(!txt)return;
if(!pendResumen||!pendResumen.conv_id){
document.getElementById('modal-resumen').classList.remove('open');
convActiva=null;updateBtn();pendResumen=null;return;}
fetch('/api/conversacion/guardar-resumen',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({conv_id:pendResumen.conv_id,resumen:txt})})
.then(r=>r.json()).then(d=>{
document.getElementById('modal-resumen').classList.remove('open');
convActiva=null;updateBtn();add('anni',txt,'resumen');pendResumen=null;})
.catch(e=>{document.getElementById('modal-resumen').classList.remove('open');convActiva=null;updateBtn();pendResumen=null;});}

function descartarResumen(){
if(!pendResumen)return;
fetch('/api/conversacion/guardar-resumen',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({conv_id:pendResumen.conv_id,resumen:'[Descartado]'})}).then(()=>{});
document.getElementById('modal-resumen').classList.remove('open');
convActiva=null;updateBtn();pendResumen=null;
add('anni','Conversacion cerrada sin guardar resumen.');}

function mostrarModalHito(h){
pendHito=h;
document.getElementById('hito-titulo').value=h.titulo||'';
document.getElementById('hito-cat').value=h.categoria||'general';
document.getElementById('hito-txt').value=h.contenido||'';
document.getElementById('hito-cuando').value=h.cuando_activarlo||'';
document.getElementById('hito-como').value=h.como_usarlo||'';
document.getElementById('hito-evidencia').textContent=h.evidencia?'"'+h.evidencia+'"':'';
document.getElementById('modal-hito').classList.add('open');}

function aprobarHito(){
var txt=document.getElementById('hito-txt').value.trim();if(!txt)return;
var titulo=document.getElementById('hito-titulo').value.trim();
var cat=document.getElementById('hito-cat').value;
var cuando=document.getElementById('hito-cuando').value.trim();
var como=document.getElementById('hito-como').value.trim();
var evidencia=pendHito?pendHito.evidencia||'':'';
fetch('/api/hitos/aprobar',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({titulo:titulo,categoria:cat,contenido:txt,evidencia:evidencia,cuando_activarlo:cuando,como_usarlo:como})})
.then(r=>r.json()).then(d=>{
document.getElementById('modal-hito').classList.remove('open');pendHito=null;});}

function rechazarHito(){document.getElementById('modal-hito').classList.remove('open');pendHito=null;}
function closeMod(id){document.getElementById(id).classList.remove('open');}

function descargarBD(){
document.getElementById('bd-pwd').value='';
document.getElementById('bd-err').style.display='none';
document.getElementById('modal-bd').classList.add('open');}

function confirmarDescarga(){
var pwd=document.getElementById('bd-pwd').value.trim();
var err=document.getElementById('bd-err');err.style.display='none';
fetch('/api/descargar-bd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pwd})})
.then(r=>{if(!r.ok)return r.json().then(d=>{throw d;});return r.blob();})
.then(blob=>{
var url=URL.createObjectURL(blob);
var a=document.createElement('a');a.href=url;a.download='anni_backup.db';a.click();
URL.revokeObjectURL(url);
document.getElementById('modal-bd').classList.remove('open');})
.catch(d=>{err.textContent=d.error||'Error';err.style.display='block';});}

function showPage(sec){
currentSection=sec;currentPage=1;
var titles={tareas:'Tareas',hitos:'Hitos del usuario',chats:'Conversaciones',memoria:'Memoria viva',diario:'Diario'};
document.getElementById('page-title').textContent=titles[sec]||sec;
document.getElementById('page').classList.add('open');
loadPage(sec,1);}
function closePage(){document.getElementById('page').classList.remove('open');}
function loadPage(sec,page){
document.getElementById('page-body').innerHTML='<p style="color:#999;padding:20px">Cargando...</p>';
if(sec==='tareas')loadTareas(page);
else if(sec==='hitos')loadHitos(page);
else if(sec==='chats')loadChats(page);
else if(sec==='diario')loadDiario(page);
else if(sec==='memoria')loadMemoria();}


function loadMemoria(){
fetch('/api/memoria').then(r=>r.json()).then(d=>{
var body=document.getElementById('page-body');body.innerHTML='';

// Sección helper
function seccion(titulo, items, renderFn, vacioMsg){
  var wrap=document.createElement('div');
  wrap.style.cssText='margin-bottom:28px';
  var h=document.createElement('div');
  h.style.cssText='font-size:11px;font-weight:900;letter-spacing:1px;color:#aaa;text-transform:uppercase;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid #f0f0f0';
  h.textContent=titulo+' ('+items.length+')';
  wrap.appendChild(h);
  if(!items.length){
    var p=document.createElement('p');
    p.style.cssText='color:#bbb;font-size:13px;font-style:italic';
    p.textContent=vacioMsg;
    wrap.appendChild(p);
  } else {
    items.forEach(function(item){ wrap.appendChild(renderFn(item)); });
  }
  body.appendChild(wrap);
}

function card(content){
  var c=document.createElement('div');
  c.className='item-card';
  c.innerHTML=content;
  return c;
}

seccion('Observaciones', d.observaciones, function(o){
  return card('<div style="font-size:12px;background:#f5f5f5;border-radius:4px;padding:2px 8px;display:inline-block;margin-bottom:6px">'+escH(o.tipo)+'</div>'+
    '<div style="font-size:15px;color:#222">'+escH(o.contenido)+'</div>'+
    '<div style="font-size:12px;color:#aaa;margin-top:4px">'+o.ts+' &middot; peso: '+o.peso+'</div>'+
    '<div class="item-actions"><button class="btn-del" onclick="delObservacion('+o.id+',this)">Borrar</button></div>');
}, 'Sin observaciones aún — cierra una conversación para generarlas.');

seccion('Personas', d.personas, function(p){
  return card('<div style="font-size:16px;font-weight:900;color:#111">'+escH(p.nombre)+'</div>'+
    '<div style="font-size:13px;color:#666;margin-top:2px">'+escH(p.relacion)+' &middot; tono: '+escH(p.tono)+'</div>'+
    '<div class="item-actions"><button class="btn-del" onclick="delPersona('+p.id+',this)">Borrar</button></div>');
}, 'Sin personas registradas aún.');

seccion('Temas abiertos', d.temas_abiertos, function(t){
  return card('<div style="font-size:15px;color:#222">'+escH(t.tema)+'</div>'+
    '<div style="font-size:12px;color:#aaa;margin-top:4px">Mencionado '+t.veces+' vez/veces</div>'+
    '<div class="item-actions"><button class="btn-del" onclick="delTema('+t.id+',this)">Borrar</button></div>');
}, 'Sin temas abiertos.');

});
}

function delObservacion(id, btn){
  if(!confirm('¿Borrar esta observación?')) return;
  fetch('/api/observacion/'+id,{method:'DELETE'}).then(r=>r.json()).then(d=>{
    if(d.ok) btn.closest('.item-card').remove();
  });
}
function delPersona(id, btn){
  if(!confirm('¿Borrar esta persona?')) return;
  fetch('/api/persona/'+id,{method:'DELETE'}).then(r=>r.json()).then(d=>{
    if(d.ok) btn.closest('.item-card').remove();
  });
}
function delTema(id, btn){
  if(!confirm('¿Borrar este tema?')) return;
  fetch('/api/cerrar-tema',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})}).then(r=>r.json()).then(d=>{
    if(d.ok) btn.closest('.item-card').remove();
  });
}

// ── TAREAS ────────────────────────────────────────────────────────────────────
var tareasVista = 'activas'; // 'activas' o 'completada'

function loadTareas(page){
  var body=document.getElementById('page-body');
  body.innerHTML='';

  // Tabs activas / completadas
  var tabs=document.createElement('div');
  tabs.style.cssText='display:flex;gap:8px;margin-bottom:20px';
  ['activas','completada'].forEach(function(v){
    var btn=document.createElement('button');
    btn.className='nav-btn'+(tareasVista===v?' active':'');
    btn.style.cssText=tareasVista===v?'background:#cc0000;color:#fff;border-color:#cc0000':'';
    btn.textContent=v==='activas'?'Pendientes':'Completadas';
    btn.onclick=function(){tareasVista=v;loadTareas(1);};
    tabs.appendChild(btn);
  });
  body.appendChild(tabs);

  // Formulario nueva tarea (solo en activas)
  if(tareasVista==='activas'){
    var form=document.createElement('div');
    form.style.cssText='background:#f9f9f9;border:1px solid #e8e8e8;border-radius:10px;padding:16px;margin-bottom:20px';
    form.innerHTML=
      '<div style="font-size:11px;font-weight:900;letter-spacing:1px;color:#aaa;margin-bottom:12px">NUEVA TAREA</div>'+
      '<input id="t-titulo" placeholder="Título *" style="width:100%;padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;font-family:inherit;margin-bottom:8px;box-sizing:border-box">'+
      '<input id="t-cliente" placeholder="Cliente (opcional)" style="width:100%;padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;font-family:inherit;margin-bottom:8px;box-sizing:border-box">'+
      '<input id="t-due" type="date" placeholder="Due date" style="width:100%;padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;font-family:inherit;margin-bottom:8px;box-sizing:border-box">'+
      '<textarea id="t-desc" placeholder="Notas (opcional)" rows="2" style="width:100%;padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;font-family:inherit;resize:vertical;margin-bottom:12px;box-sizing:border-box"></textarea>'+
      '<button onclick="crearTarea()" style="background:#cc0000;color:#fff;border:none;border-radius:6px;padding:8px 20px;font-size:13px;font-weight:700;cursor:pointer">Añadir tarea</button>';
    body.appendChild(form);
  }

  // Lista tareas
  fetch('/api/tareas?estado='+tareasVista).then(r=>r.json()).then(d=>{
    if(!d.tareas.length){
      var p=document.createElement('p');
      p.style.cssText='color:#bbb;font-size:13px;font-style:italic;padding:8px 0';
      p.textContent=tareasVista==='activas'?'Sin tareas pendientes.':'Sin tareas completadas.';
      body.appendChild(p);
      return;
    }
    d.tareas.forEach(function(t){
      var card=document.createElement('div');
      card.className='item-card';
      card.id='tarea-'+t.id;

      var estado_badge = t.estado==='en_progreso'?
        '<span style="font-size:11px;background:#fff3cd;border:1px solid #ffc107;border-radius:4px;padding:2px 8px;margin-left:8px">EN PROGRESO</span>':'';
      var cliente_txt = t.cliente?'<span style="font-size:12px;color:#888;margin-right:8px">'+escH(t.cliente)+'</span>':'';
      var due_txt = t.due_date?'<span style="font-size:12px;color:'+(new Date(t.due_date)<new Date()?'#cc0000':'#888')+';margin-right:8px">vence: '+escH(t.due_date)+'</span>':'';
      var desc_id='tdesc-'+t.id;

      var acciones='';
      if(tareasVista==='activas'){
        acciones='<button class="btn-edit" onclick="editTarea('+t.id+')">Editar</button>'+
          (t.estado==='pendiente'?'<button class="btn-edit" onclick="cambiarEstadoTarea('+t.id+',\"en_progreso\")">En progreso</button>':
          '<button class="btn-edit" onclick="cambiarEstadoTarea('+t.id+',\"pendiente\")">Pausar</button>')+
          '<button class="btn-edit" style="background:#e8f5e9;border-color:#81c784;color:#2e7d32" onclick="completarTarea('+t.id+')">✓ Completar</button>'+
          '<button class="btn-del" onclick="borrarTarea('+t.id+')">Borrar</button>';
      } else {
        acciones='<button class="btn-edit" onclick="reabrirTarea('+t.id+')">Reabrir</button>'+
          '<button class="btn-del" onclick="borrarTarea('+t.id+')">Borrar</button>';
      }

      card.innerHTML=
        '<div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px;margin-bottom:6px">'+
          '<div style="font-size:16px;font-weight:900;color:#111" id="ttitulo-'+t.id+'">'+escH(t.titulo)+'</div>'+estado_badge+
        '</div>'+
        '<div style="margin-bottom:6px">'+cliente_txt+due_txt+
          '<span style="font-size:12px;color:#bbb">creada: '+t.ts_creacion+'</span>'+
        '</div>'+
        '<div id="'+desc_id+'" style="font-size:14px;color:#555;white-space:pre-wrap;margin-bottom:8px">'+escH(t.descripcion||'')+'</div>'+
        '<div class="item-actions">'+acciones+'</div>';
      body.appendChild(card);
    });
  });
}

function crearTarea(){
  var titulo=document.getElementById('t-titulo').value.trim();
  if(!titulo){alert('El título es obligatorio');return;}
  var data={
    titulo:titulo,
    cliente:document.getElementById('t-cliente').value.trim(),
    due_date:document.getElementById('t-due').value||null,
    descripcion:document.getElementById('t-desc').value.trim()
  };
  fetch('/api/tareas',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
    .then(r=>r.json()).then(d=>{if(d.ok)loadTareas(1);});
}

function editTarea(id){
  var card=document.getElementById('tarea-'+id);
  var titulo=card.querySelector('#ttitulo-'+id).textContent;
  var desc=card.querySelector('#tdesc-'+id).textContent;
  var due=card.dataset.due||'';
  card.querySelector('#ttitulo-'+id).innerHTML='<input value="'+escH(titulo)+'" style="width:100%;padding:6px;border:2px solid #cc0000;border-radius:6px;font-size:15px;font-weight:900;font-family:inherit" id="edit-titulo-'+id+'">';
  card.querySelector('#tdesc-'+id).innerHTML=
    '<input type="date" value="'+escH(due)+'" style="width:100%;padding:6px;border:1px solid #ddd;border-radius:6px;font-size:13px;font-family:inherit;margin-bottom:6px" id="edit-due-'+id+'">'+
    '<textarea rows="3" style="width:100%;padding:6px;border:2px solid #cc0000;border-radius:6px;font-size:14px;font-family:inherit;resize:vertical" id="edit-desc-'+id+'">'+escH(desc)+'</textarea>';
  var actions=card.querySelector('.item-actions');
  actions.innerHTML='<button class="btn-edit" onclick="guardarEditTarea('+id+')">Guardar</button><button class="btn-del" onclick="loadTareas(1)">Cancelar</button>';
}

function guardarEditTarea(id){
  var titulo=document.getElementById('edit-titulo-'+id).value.trim();
  var desc=document.getElementById('edit-desc-'+id).value.trim();
  var due=document.getElementById('edit-due-'+id).value||null;
  fetch('/api/tareas/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({titulo:titulo,descripcion:desc,due_date:due})})
    .then(r=>r.json()).then(d=>{if(d.ok)loadTareas(1);});
}

function cambiarEstadoTarea(id, estado){
  fetch('/api/tareas/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({estado:estado})}).then(r=>r.json()).then(d=>{if(d.ok)loadTareas(1);});
}

function completarTarea(id){
  fetch('/api/tareas/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({estado:'completada'})}).then(r=>r.json()).then(d=>{if(d.ok)loadTareas(1);});
}

function reabrirTarea(id){
  fetch('/api/tareas/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({estado:'pendiente',ts_completada:null})}).then(r=>r.json()).then(d=>{if(d.ok){tareasVista='activas';loadTareas(1);}});
}

function borrarTarea(id){
  if(!confirm('¿Borrar esta tarea?'))return;
  fetch('/api/tareas/'+id,{method:'DELETE'}).then(r=>r.json()).then(d=>{if(d.ok)loadTareas(1);});
}

function loadHitos(page){
fetch('/api/hitos?page='+page).then(r=>r.json()).then(d=>{
var body=document.getElementById('page-body');body.innerHTML='';
if(!d.hitos.length){body.innerHTML='<p style="color:#999;padding:20px">Sin hitos guardados aun.</p>';return;}
d.hitos.forEach(function(h){
var card=document.createElement('div');card.className='item-card';
var titulo=h.titulo?'<div style="font-size:16px;font-weight:900;color:#111;margin-bottom:4px">'+escH(h.titulo)+'</div>':'';
var cat=h.categoria?'<span style="font-size:11px;background:#f5f5f5;border:1px solid #e0e0e0;border-radius:4px;padding:2px 7px;margin-right:6px">'+escH(h.categoria)+'</span>':'';
var ev=h.evidencia?'<div style="font-size:13px;color:#888;margin-top:8px;font-style:italic;border-left:3px solid #e0e0e0;padding-left:10px">"'+escH(h.evidencia)+'"</div>':'';
var cuando=h.cuando?'<div style="font-size:12px;color:#aaa;margin-top:6px"><b>Activar:</b> '+escH(h.cuando)+'</div>':'';
var como=h.como?'<div style="font-size:12px;color:#aaa;margin-top:2px"><b>Uso:</b> '+escH(h.como)+'</div>':'';
card.innerHTML='<div class="item-meta">'+cat+'#'+h.id+' &middot; '+h.ts+'</div>'+titulo+
'<div class="item-content" id="hc-'+h.id+'">'+escH(h.contenido)+'</div>'+ev+cuando+como+
'<div class="item-actions">'+
'<button class="btn-edit" onclick="editHito('+h.id+',this)">Editar</button>'+
'<button class="btn-del" onclick="delHito('+h.id+')">Borrar</button>'+
'</div>';
body.appendChild(card);});
body.appendChild(pagerEl(d.pages,page,'loadHitos'));});}

function editHito(id,btn){
var card=btn.closest('.item-card');
var contentEl=card.querySelector('[id="hc-'+id+'"]');
if(!contentEl)return;
var orig=contentEl.textContent;
contentEl.innerHTML='<textarea style="width:100%;border:2px solid #cc0000;border-radius:8px;padding:10px;font-size:15px;font-family:inherit;resize:vertical" rows="4">'+escH(orig)+'</textarea>';
btn.textContent='Guardar';
btn.onclick=function(){
var txt=contentEl.querySelector('textarea').value.trim();
if(!txt)return;
fetch('/api/hitos/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({contenido:txt})})
.then(()=>loadHitos(currentPage));};
}

function delHito(id){
if(!confirm('Borrar este hito?'))return;
fetch('/api/hitos/'+id,{method:'DELETE'}).then(()=>loadHitos(currentPage));}

function loadChats(page){
fetch('/api/chats?page='+page).then(r=>r.json()).then(d=>{
var body=document.getElementById('page-body');body.innerHTML='';
if(!d.chats.length){body.innerHTML='<p style="color:#999;padding:20px">Sin conversaciones guardadas.</p>';return;}
d.chats.forEach(function(c){
var card=document.createElement('div');card.className='item-card';
card.innerHTML='<div class="item-meta">Chat #'+c.id+' &middot; '+c.inicio+'</div>'+
'<div class="item-content" id="cc-'+c.id+'">'+escH(c.resumen)+'</div>'+
'<div class="item-actions">'+
'<button class="btn-edit" onclick="editChat('+c.id+',this)">Editar</button>'+
'</div>';
body.appendChild(card);});
body.appendChild(pagerEl(d.pages,page,'loadChats'));});}

function editChat(id,btn){
var contentEl=document.getElementById('cc-'+id);
if(!contentEl)return;
var orig=contentEl.textContent;
contentEl.innerHTML='<textarea style="width:100%;border:2px solid #cc0000;border-radius:8px;padding:10px;font-size:15px;font-family:inherit;resize:vertical" rows="5">'+escH(orig)+'</textarea>';
btn.textContent='Guardar';
btn.onclick=function(){
var txt=contentEl.querySelector('textarea').value.trim();
if(!txt)return;
fetch('/api/chats/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({resumen:txt})})
.then(r=>r.json()).then(()=>loadChats(currentPage));};
}

var diarioOrden='desc';

function loadDiario(page){
var body=document.getElementById('page-body');
var hoy=new Date();
var hoyStr=hoy.getFullYear()+'-'+String(hoy.getMonth()+1).padStart(2,'0')+'-'+String(hoy.getDate()).padStart(2,'0');
body.innerHTML='<div class="item-card" style="background:#fff5f5;border-color:#ffcccc">'+
'<h3 style="font-size:16px;font-weight:800;margin-bottom:14px;color:#cc0000">Nueva entrada</h3>'+
'<div class="form-group"><label>Fecha</label><input type="date" id="d-fecha" value="'+hoyStr+'" oninput="calcDia()"></div>'+
'<div id="d-dia" style="margin-bottom:12px"></div>'+
'<div class="form-group"><label>Titulo</label><input type="text" id="d-titulo" placeholder="Titulo de la entrada"></div>'+
'<div class="form-group"><label>Texto</label><textarea id="d-texto" rows="5" placeholder="Escribe aqui..."></textarea></div>'+
'<button onclick="guardarDiario()" style="padding:12px 20px;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;background:#cc0000;color:#fff;border:none">Guardar entrada</button>'+
'</div>';
calcDia();
// Botón de orden
var ordenBtn=document.createElement('div');
ordenBtn.style.cssText='display:flex;align-items:center;gap:8px;margin-bottom:12px';
ordenBtn.innerHTML='<button onclick="toggleOrdenDiario()" class="nav-btn" style="font-size:11px">'+
(diarioOrden==='desc'?'↓ Más recientes primero':'↑ Más antiguas primero')+'</button>';
body.appendChild(ordenBtn);
fetch('/api/diario?page='+page+'&orden='+diarioOrden).then(r=>r.json()).then(d=>{
if(!d.entradas.length)return;
d.entradas.forEach(function(e){
var card=document.createElement('div');card.className='item-card';card.id='diario-'+e.id;
card.innerHTML='<span class="dia-badge">Dia '+e.dia+'</span>'+
'<div class="item-meta">'+e.fecha+'</div>'+
'<h3 style="font-size:16px;font-weight:800;margin-bottom:8px" id="dtitulo-'+e.id+'">'+escH(e.titulo)+'</h3>'+
'<div class="item-content" id="dtexto-'+e.id+'">'+escH(e.texto)+'</div>'+
'<div class="item-actions">'+
'<button class="btn-edit" onclick="editDiario('+e.id+','+JSON.stringify(e.fecha)+')">Editar</button>'+
'<button class="btn-del" onclick="delDiario('+e.id+')">Borrar</button>'+
'</div>';
body.appendChild(card);});
body.appendChild(pagerEl(d.pages,page,'loadDiario'));});}

function toggleOrdenDiario(){
diarioOrden=diarioOrden==='desc'?'asc':'desc';
loadDiario(1);}

function editDiario(id,fecha){
var card=document.getElementById('diario-'+id);
if(!card){return;}
var tituloEl=document.getElementById('dtitulo-'+id);
var textoEl=document.getElementById('dtexto-'+id);
var titulo=tituloEl?tituloEl.textContent:'';
var texto=textoEl?textoEl.textContent:'';
if(tituloEl) tituloEl.innerHTML='<input value="'+escH(titulo)+'" id="editd-titulo-'+id+'" style="width:100%;padding:6px;border:2px solid #cc0000;border-radius:6px;font-size:15px;font-weight:800;font-family:inherit">';
if(textoEl) textoEl.innerHTML='<input type="date" value="'+escH(fecha)+'" id="editd-fecha-'+id+'" style="width:100%;padding:6px;border:1px solid #ddd;border-radius:6px;font-size:13px;font-family:inherit;margin-bottom:6px"><textarea rows="8" id="editd-texto-'+id+'" style="width:100%;padding:6px;border:2px solid #cc0000;border-radius:6px;font-size:14px;font-family:inherit;resize:vertical">'+escH(texto)+'</textarea>';
var actions=card.querySelector('.item-actions');
if(actions) actions.innerHTML='<button class="btn-edit" onclick="guardarEditDiario('+id+')">Guardar</button><button class="btn-del" onclick="loadDiario(currentPage)">Cancelar</button>';}

function guardarEditDiario(id){
var titulo=document.getElementById('editd-titulo-'+id).value.trim();
var texto=document.getElementById('editd-texto-'+id).value.trim();
var fecha=document.getElementById('editd-fecha-'+id).value;
if(!titulo||!texto){alert('Titulo y texto son obligatorios');return;}
fetch('/api/diario/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},
body:JSON.stringify({titulo:titulo,texto:texto,fecha:fecha})})
.then(r=>r.json()).then(d=>{if(d.ok)loadDiario(currentPage);});}

function calcDia(){
var f=document.getElementById('d-fecha');var el=document.getElementById('d-dia');
if(!f||!el)return;
var inicio=new Date('2026-03-01T00:00:00');var sel=new Date(f.value+'T00:00:00');
var diff=Math.round((sel-inicio)/(1000*60*60*24))+1;
el.innerHTML='<span class="dia-badge">Dia '+diff+' del experimento</span>';}

function guardarDiario(){
var fecha=document.getElementById('d-fecha').value;
var titulo=document.getElementById('d-titulo').value.trim();
var texto=document.getElementById('d-texto').value.trim();
if(!fecha||!titulo||!texto){alert('Rellena todos los campos');return;}
fetch('/api/diario',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({fecha:fecha,titulo:titulo,texto:texto})})
.then(r=>r.json()).then(d=>{if(d.ok){loadDiario(1);}});}

function delDiario(id){
if(!confirm('Borrar esta entrada?'))return;
fetch('/api/diario/'+id,{method:'DELETE'}).then(()=>loadDiario(currentPage));}

function pagerEl(pages,current,fn){
if(pages<=1)return document.createElement('div');
var div=document.createElement('div');div.className='pager';
for(var i=1;i<=pages;i++){
var btn=document.createElement('button');
btn.textContent=i;if(i===current)btn.className='active';
btn.onclick=(function(p){return function(){currentPage=p;loadPage(currentSection,p);}})(i);
div.appendChild(btn);}
return div;}

function escH(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

I.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();env();}});
I.addEventListener('input',function(){this.style.height='auto';this.style.height=Math.min(this.scrollHeight,140)+'px';});

fetch('/api/historial').then(r=>r.json()).then(d=>{
if(d.messages&&d.messages.length){d.messages.forEach(m=>add(m.role,m.content));}
fetch('/api/bienvenida').then(r=>r.json()).then(b=>{if(b.intervencion)add('anni',b.intervencion,b.tipo||'pro');});
fetch('/api/conv-activa').then(r=>r.json()).then(c=>{
if(c.id){convActiva=c.id;convNum=c.id;updateBtn();}
else{convActiva=null;updateBtn();}});
});
</script>
</body></html>"""
# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    print(f"\n{'='*50}")
    print(f"  ANNI {ANNI_VERSION}")
    print(f"  {ANNI_CREDITS}")
    print(f"  DB: {DB_PATH}")
    print(f"{'='*50}\n")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
