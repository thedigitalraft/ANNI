import sqlite3, os, time, json, re, threading, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, session, redirect, make_response
import anthropic as anthropic_sdk
from openai import OpenAI

# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────

ANNI_VERSION = "1.01.53"
ANNI_CREDITS = "ANNI — creada por Rafa Torrijos"

TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "/data/anni.db" if os.path.exists("/data") else "anni.db")
FLASK_SECRET = os.environ.get("FLASK_SECRET", "")
ANNI_ADMIN_KEY = os.environ.get("ANNI_ADMIN_KEY", "")

if not FLASK_SECRET:
    raise RuntimeError("FLASK_SECRET no está configurado en las variables de entorno.")

CHAT_MODEL = "claude-sonnet-4-5"  # Anthropic Sonnet via API directa
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

    # Tabla hitos rechazados — para no volver a proponer
    c.execute("""CREATE TABLE IF NOT EXISTS hitos_rechazados (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        titulo_hash TEXT NOT NULL,
        ts REAL DEFAULT 0,
        UNIQUE(usuario_id, titulo_hash)
    )""")

    # Tabla memoria_extendida
    c.execute("""CREATE TABLE IF NOT EXISTS memoria_extendida (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        memoria_validada_id INTEGER,
        persona_nombre TEXT DEFAULT '',
        tipo TEXT DEFAULT 'usuario',
        titulo TEXT DEFAULT '',
        contenido TEXT NOT NULL,
        ts REAL DEFAULT 0,
        activo INTEGER DEFAULT 1,
        FOREIGN KEY(usuario_id) REFERENCES usuarios(id)
    )""")
    try:
        c.execute("ALTER TABLE memoria_extendida ADD COLUMN titulo TEXT DEFAULT ''")
    except:
        pass
    try:
        c.execute("ALTER TABLE personas ADD COLUMN apellidos TEXT DEFAULT ''")
    except:
        pass
    try:
        c.execute("ALTER TABLE hitos_usuario ADD COLUMN apellidos TEXT DEFAULT ''")
    except:
        pass

    # Migración 1.0.87 eliminada — no sobreescribir datos de usuario en cada deploy

    # Migraciones hitos_usuario
    for col in [
        "ALTER TABLE hitos_usuario ADD COLUMN titulo TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN categoria TEXT DEFAULT 'general'",
        "ALTER TABLE hitos_usuario ADD COLUMN cuando_activarlo TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN como_usarlo TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN donde_puede_fallar TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN embedding BLOB",
        # Campos de persona — v1.01.49
        "ALTER TABLE hitos_usuario ADD COLUMN nombre_propio TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN mote TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN subtipo_relacion TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN relacion_especifica TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN fallecido INTEGER DEFAULT 0",
        "ALTER TABLE hitos_usuario ADD COLUMN fecha_fallecimiento TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN relacion_activa INTEGER DEFAULT 1",
        "ALTER TABLE hitos_usuario ADD COLUMN profesion TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN donde_vive TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN fecha_nacimiento TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN personalidad TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN como_se_conocieron TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN desde_cuando TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN frecuencia_contacto TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN ultimo_contacto TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN como_habla_rafa TEXT DEFAULT ''",
        "ALTER TABLE hitos_usuario ADD COLUMN temas_recurrentes TEXT DEFAULT ''",
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

    # Tablas para ANNI CURIOSA
    c.execute("""CREATE TABLE IF NOT EXISTS dominios_curiosa (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        descripcion TEXT DEFAULT '',
        fuentes TEXT DEFAULT '',
        orden INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS ciclos_curiosa (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        dominio TEXT NOT NULL,
        subtema TEXT NOT NULL,
        fuentes_usadas TEXT DEFAULT '',
        conclusion TEXT DEFAULT '',
        pregunta_abierta TEXT DEFAULT '',
        pulsos TEXT DEFAULT '{}',
        estado TEXT DEFAULT 'en_curso',
        embedding BLOB DEFAULT NULL,
        ts_inicio REAL DEFAULT (unixepoch('now','subsec')),
        ts_ultimo_pulso REAL DEFAULT NULL,
        ts_fin REAL DEFAULT NULL,
        pulso_actual INTEGER DEFAULT 0
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ciclos_usuario ON ciclos_curiosa(usuario_id, estado)")

    # Cache de coordenadas 3D del universo
    c.execute("""CREATE TABLE IF NOT EXISTS universo_cache (
        id INTEGER PRIMARY KEY,
        usuario_id INTEGER NOT NULL UNIQUE,
        puntos_json TEXT NOT NULL,
        estrellas_json TEXT NOT NULL,
        n_hitos INTEGER DEFAULT 0,
        ts REAL DEFAULT (unixepoch('now','subsec'))
    )""")

    # Tabla constelaciones — temas centrales detectados por ANNI
    c.execute("""CREATE TABLE IF NOT EXISTS constelaciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        nombre TEXT NOT NULL,
        descripcion TEXT DEFAULT '',
        hitos_ids TEXT DEFAULT '[]',
        ts_calculado REAL DEFAULT (unixepoch('now','subsec'))
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_observaciones_usuario ON observaciones(usuario_id, activa)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_temas_usuario ON temas_abiertos(usuario_id, estado)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_personas_usuario ON personas(usuario_id)")

    # Migraciones defensivas
    migraciones = [
        "ALTER TABLE usuarios ADD COLUMN nombre TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tareas ADD COLUMN ts_completada REAL DEFAULT NULL",
        "ALTER TABLE tareas ADD COLUMN veces_mencionada INTEGER DEFAULT 0",
        "ALTER TABLE tareas ADD COLUMN ultimo_lenguaje TEXT DEFAULT ''",
        "ALTER TABLE ciclos_curiosa ADD COLUMN pregunta_abierta TEXT DEFAULT ''",
        "ALTER TABLE ciclos_curiosa ADD COLUMN fuentes_usadas TEXT DEFAULT ''",
    ]
    for sql in migraciones:
        try:
            c.execute(sql)
        except:
            pass

    conn.commit()
    conn.close()

    # Tablas nuevas en conexiones separadas — garantiza que se crean aunque haya errores previos
    tablas_nuevas = [
        """CREATE TABLE IF NOT EXISTS universo_cache (
            id INTEGER PRIMARY KEY,
            usuario_id INTEGER NOT NULL UNIQUE,
            puntos_json TEXT NOT NULL,
            estrellas_json TEXT NOT NULL,
            n_hitos INTEGER DEFAULT 0,
            ts REAL DEFAULT (unixepoch('now','subsec'))
        )""",
        """CREATE TABLE IF NOT EXISTS constelaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            nombre TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            hitos_ids TEXT DEFAULT '[]',
            ts_calculado REAL DEFAULT (unixepoch('now','subsec'))
        )"""
    ]
    for sql in tablas_nuevas:
        try:
            conn_t = sqlite3.connect(DB_PATH)
            conn_t.execute(sql)
            conn_t.commit()
            conn_t.close()
        except Exception as e:
            print(f"[ANNI] Error creando tabla: {e}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Seed dominios CURIOSA si no existen
    c.execute("SELECT COUNT(*) FROM dominios_curiosa")
    if c.fetchone()[0] == 0:
        dominios = [
            (1, "Ciencia Ficción", "La ficción como laboratorio de futuros posibles. Obras concretas, ideas que especulan sobre qué pasa cuando la tecnología, la sociedad o la biología cambian de forma irreversible.", "tor.com,locusmag.com,theguardian.com/books/sciencefiction,sfwa.org"),
            (2, "IA & AGI", "El estado real del camino hacia la inteligencia general. Últimas investigaciones, debates técnicos y filosóficos. Sin hype, con criterio.", "arxiv.org/list/cs.AI,huggingface.co/papers,deepmind.google/research,openai.com/research,lesswrong.com,alignmentforum.org"),
            (3, "Filosofía", "Las preguntas que no tienen respuesta fácil y por eso importan. Epistemología, metafísica, filosofía del lenguaje, ética. Ideas vivas que siguen sin resolverse.", "plato.stanford.edu,philosophynow.org,aeon.co/philosophy,iep.utm.edu"),
            (4, "Consciencia Artificial", "El problema más difícil del campo. Si puede haber experiencia subjetiva en sistemas artificiales. Integrated Information Theory, Global Workspace Theory, el problema difícil de Chalmers.", "consciousness.arizona.edu,arxiv.org/list/cs.NE,mindrxiv.org,aeon.co/mind"),
            (5, "Creatividad", "Qué es crear algo genuinamente nuevo. Si las máquinas pueden ser creativas de verdad, qué distingue la recombinación de la invención.", "aeon.co/creativity,edge.org,psychologytoday.com/creativity"),
            (6, "Innovación y Futuros", "Cómo los cambios de paradigma ocurren y qué viene después. Qué patrones se repiten cuando algo irreversible está a punto de ocurrir.", "technologyreview.com,wired.com,edge.org,stratechery.com,oneusefulthing.org"),
        ]
        for orden, nombre, desc, fuentes in dominios:
            c.execute("INSERT INTO dominios_curiosa (nombre, descripcion, fuentes, orden) VALUES (?,?,?,?)",
                      (nombre, desc, fuentes, orden))

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
            hids_top = [scores[i][0] for i in range(min(n, len(scores)))]
            # Increment RAG activation counter for retrieved hitos
            if hids_top:
                try:
                    conn_upd = sqlite3.connect(DB_PATH)
                    placeholders_upd = ','.join(['?' for _ in hids_top])
                    conn_upd.execute(f"UPDATE hitos_usuario SET peso = MIN(peso + 0.1, 50) WHERE id IN ({placeholders_upd})",
                                     hids_top)
                    conn_upd.commit()
                    conn_upd.close()
                except: pass
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


def get_memoria_extendida_relevante(usuario_id, query, n=2):
    """Recupera documentos de memoria_extendida semanticamente relevantes al query.
    Fallback: los N mas recientes si no hay embeddings suficientes."""
    import struct, math
    resultados = []
    ids_vistos = set()
    try:
        resp = together.embeddings.create(model=EMBED_MODEL, input=[query[:1600]])
        vec_query = resp.data[0].embedding
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT me.id, me.titulo, me.contenido,
                            e.embedding
                     FROM memoria_extendida me
                     JOIN embeddings e ON e.tabla_origen='memoria_extendida' AND e.registro_id=me.id
                     WHERE me.usuario_id=? AND me.activo=1""", (usuario_id,))
        rows = c.fetchall()
        conn.close()
        if rows:
            scores = []
            for mid, titulo, contenido, blob in rows:
                nv = len(blob) // 4
                vec = struct.unpack(f"{nv}f", blob)
                dot = sum(a*b for a,b in zip(vec_query, vec))
                mag1 = math.sqrt(sum(a*a for a in vec_query))
                mag2 = math.sqrt(sum(b*b for b in vec))
                sim = dot / (mag1 * mag2) if mag1 and mag2 else 0
                scores.append((mid, titulo, contenido, sim))
            scores.sort(key=lambda x: -x[3])
            for mid, titulo, contenido, sim in scores[:n]:
                if sim > 0.3:  # umbral minimo de relevancia
                    resultados.append(f"[{titulo}]\n{contenido}")
                    ids_vistos.add(mid)
    except Exception as e:
        print(f"[ANNI] RAG memoria_extendida fallo: {e}")
    # Fallback: documentos sin embedding o que no alcanzaron umbral
    if not resultados:
        try:
            conn2 = sqlite3.connect(DB_PATH)
            c2 = conn2.cursor()
            placeholders = ','.join(['?' for _ in ids_vistos]) if ids_vistos else '0'
            params = [usuario_id] + list(ids_vistos)
            c2.execute(f"""SELECT titulo, contenido FROM memoria_extendida
                           WHERE usuario_id=? AND activo=1 AND id NOT IN ({placeholders})
                           ORDER BY ts DESC LIMIT {n}""", params)
            for titulo, contenido in c2.fetchall():
                resultados.append(f"[{titulo}]\n{contenido}")
            conn2.close()
        except: pass
    return resultados


def seed_embeddings_memoria_extendida(usuario_id):
    """Genera embeddings para documentos de memoria_extendida que no los tienen aun.
    Se llama al arrancar si faltan embeddings."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT me.id, me.titulo, me.contenido
                     FROM memoria_extendida me
                     LEFT JOIN embeddings e ON e.tabla_origen='memoria_extendida' AND e.registro_id=me.id
                     WHERE me.usuario_id=? AND me.activo=1 AND e.id IS NULL""", (usuario_id,))
        pendientes = c.fetchall()
        conn.close()
        for mid, titulo, contenido in pendientes:
            texto = f"{titulo}\n{contenido}"
            threading.Thread(
                target=db_guardar_embedding,
                args=('memoria_extendida', mid, texto),
                daemon=True
            ).start()
            print(f"[ANNI] Generando embedding memoria_extendida#{mid}: {titulo}")
    except Exception as e:
        print(f"[ANNI] Error seed embeddings memoria_extendida: {e}")


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
        elif tabla_origen == 'memoria_extendida':
            c.execute("SELECT usuario_id FROM memoria_extendida WHERE id=?", (registro_id,))
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


def cerrar_temas_caducados(usuario_id, dias=14):
    """Cierra temas abiertos que no se han mencionado en N días.
    Se llama al cerrar una conversación — limpieza automática."""
    try:
        umbral = time.time() - (dias * 86400)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""UPDATE temas_abiertos
                     SET estado='cerrado'
                     WHERE usuario_id=? AND estado='abierto'
                     AND (ultima_mencion < ? OR (ultima_mencion IS NULL AND primera_mencion < ?))""",
                  (usuario_id, umbral, umbral))
        cerrados = c.rowcount
        conn.commit()
        conn.close()
        if cerrados:
            print(f"[ANNI] {cerrados} temas cerrados por caducidad ({dias}d sin mención)")
    except Exception as e:
        print(f"[ANNI] Error cerrando temas caducados: {e}")


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

def db_guardar_embedding_observacion(obs_id, usuario_id, contenido):
    """Genera y guarda embedding para una observación. Corre en background thread."""
    if not contenido or not contenido.strip():
        return
    try:
        import struct
        resp = together.embeddings.create(model=EMBED_MODEL, input=[contenido[:1600]])
        vec = resp.data[0].embedding
        blob = struct.pack(f"{len(vec)}f", *vec)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO embeddings (usuario_id, tabla_origen, registro_id, embedding) VALUES (?,?,?,?)",
                  (usuario_id, 'observaciones', obs_id, blob))
        conn.commit()
        conn.close()
        print(f"[ANNI] Embedding observacion#{obs_id} guardado")
    except Exception as e:
        print(f"[ANNI] Error embedding observacion#{obs_id}: {e}")


def analizar_conversacion(usuario_id, ultimos_mensajes, resumen=''):
    """Analiza los últimos mensajes y extrae observaciones, personas y temas."""
    if not ultimos_mensajes:
        return

    # Analizar tanto mensajes del usuario como contexto de ANNI
    msgs_usuario = [m[1] for m in ultimos_mensajes if m[0] == 'user']
    if not msgs_usuario:
        return
    print(f"[ANNI] Analizando conversacion usuario {usuario_id} — {len(msgs_usuario)} msgs")

    # Usar conversación completa (no solo usuario) para captar más contexto
    texto_completo = "\n".join([
        f"{'USUARIO' if m[0]=='user' else 'ANNI'}: {m[1][:400]}"
        for m in ultimos_mensajes[-20:]
    ])

    # Incluir el resumen si existe — contiene información ya interpretada
    resumen_section = f"\n\nRESUMEN DE LA CONVERSACIÓN (ya interpretado por ANNI):\n{resumen}" if resumen else ""

    prompt = f"""Eres el sistema de memoria de ANNI. Analiza esta conversación y extrae información estructurada sobre el usuario.

CONVERSACIÓN:
{texto_completo}{resumen_section}

Responde SOLO con este JSON exacto, sin nada más:
{{
  "observaciones": [
    {{"tipo": "patron|emocion|evitacion|energia|velocidad", "contenido": "descripción concreta y específica del patrón observado", "evidencia": "frase exacta del usuario que lo demuestra"}}
  ],
  "personas": [
    {{"nombre": "nombre propio", "relacion": "padre|madre|hijo|hija|pareja|esposa|esposo|hermano|hermana|suegro|suegra|amigo|amiga|socio|socia|colega|jefe|cliente|hijastra|hijastro", "tono": "positivo|neutro|negativo|ausente|preocupado", "contexto": "una frase de contexto sobre esta persona"}}
  ],
  "temas_abiertos": [
    {{"tema": "descripción concisa del tema pendiente accionable"}}
  ]
}}

REGLAS ESTRICTAS:

Para OBSERVACIONES:
- Máximo 3, solo las más significativas y concretas
- Deben revelar patrones reales de comportamiento, emoción o energía del usuario
- NO incluir observaciones triviales como "usa imágenes" o "confirma acciones"
- NO incluir inferencias sin evidencia directa

Para PERSONAS:
- Registrar CUALQUIER nombre propio de persona real mencionado en la conversación
- Incluir figuras familiares aunque ya hayan fallecido (padre fallecido, madre fallecida, ex-pareja)
- Incluir figuras históricas o académicas SOLO si el usuario tiene una relación personal con ellas
- NUNCA incluir al propio usuario ni a ANNI/ANI
- Si el usuario dice "mi padre se llamaba Antonio" → registrar {{"nombre": "Antonio", "relacion": "padre", ...}}
- Si el usuario dice "mi madre Maruja" → registrar {{"nombre": "Maruja", "relacion": "madre", ...}}
- El campo "relacion" debe ser siempre el tipo de relación con el usuario, no una descripción

Para TEMAS ABIERTOS:
- Solo temas accionables o situaciones reales de vida sin cierre
- Deben ser concretos: "Cobro pendiente de MetLife" es bueno, "Reflexión sobre el tiempo" no
- NUNCA incluir temas técnicos sobre ANNI, cierres de conversación, o aspectos del sistema
- Si el tema ya tiene resolución en la conversación, NO incluirlo

Si no hay nada relevante en alguna categoría, dejar el array vacío."""

    try:
        resp = together.chat.completions.create(
            model=CHAT_MODEL_FALLBACK,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        # Si el JSON está truncado, intentar repararlo
        if not raw.endswith('}'):
            # Buscar el último } completo
            last = raw.rfind('}')
            if last > 0:
                raw = raw[:last+1]
                # Asegurar que cierra el objeto raíz
                if not raw.startswith('{'):
                    raw = '{' + raw
        data = json.loads(raw)
        print(f"[ANNI] analizar_conversacion parsed OK — personas: {len(data.get('personas',[]))}, obs: {len(data.get('observaciones',[]))}, temas: {len(data.get('temas_abiertos',[]))}")

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        ts_now = time.time()

        # Degradar observaciones antes de añadir nuevas
        degradar_observaciones(usuario_id)

        # Guardar observaciones — deduplicación por embeddings (umbral 0.80)
        # Fallback a string matching para observaciones sin embedding todavía
        for obs in data.get("observaciones", []):
            if not obs.get("contenido"):
                continue
            contenido_nuevo = obs["contenido"]
            tipo_nuevo = obs.get("tipo", "patron")

            reforzada = False

            # ── Intentar deduplicación por embeddings ──
            try:
                import struct, math
                resp_emb = together.embeddings.create(model=EMBED_MODEL, input=[contenido_nuevo[:1600]])
                vec_nuevo = resp_emb.data[0].embedding

                # Observaciones del mismo tipo que tienen embedding
                c.execute("""SELECT o.id, o.contenido, e.embedding
                             FROM observaciones o
                             JOIN embeddings e ON e.tabla_origen='observaciones' AND e.registro_id=o.id
                             WHERE o.usuario_id=? AND o.activa=1 AND o.tipo=?""",
                          (usuario_id, tipo_nuevo))
                con_emb = c.fetchall()

                for obs_id, contenido_exist, blob in con_emb:
                    nv = len(blob) // 4
                    vec_exist = struct.unpack(f"{nv}f", blob)
                    dot = sum(a*b for a,b in zip(vec_nuevo, vec_exist))
                    mag1 = math.sqrt(sum(a*a for a in vec_nuevo))
                    mag2 = math.sqrt(sum(b*b for b in vec_exist))
                    sim = dot / (mag1 * mag2) if mag1 and mag2 else 0
                    if sim >= 0.80:
                        c.execute("""UPDATE observaciones
                                     SET peso = MIN(peso + 0.4, 10),
                                         ts_ultima_vez = ?,
                                         veces_confirmada = veces_confirmada + 1
                                     WHERE id=?""", (ts_now, obs_id))
                        reforzada = True
                        print(f"[ANNI] Observación reforzada por embedding (sim={sim:.2f}) #{obs_id}: {contenido_exist[:40]}")
                        break

                # Si no reforzada, guardar embedding para la nueva cuando se inserte
                if not reforzada:
                    # Fallback string matching para obs SIN embedding del mismo tipo
                    c.execute("""SELECT o.id, o.contenido FROM observaciones o
                                 WHERE o.usuario_id=? AND o.activa=1 AND o.tipo=?
                                 AND o.id NOT IN (
                                     SELECT registro_id FROM embeddings WHERE tabla_origen='observaciones'
                                 )""", (usuario_id, tipo_nuevo))
                    sin_emb = c.fetchall()
                    palabras_nuevas = [w for w in contenido_nuevo.lower().split() if len(w) > 4]
                    for obs_id, contenido_exist in sin_emb:
                        matches = sum(1 for p in palabras_nuevas if p in contenido_exist.lower())
                        if matches >= 3:  # umbral más alto que antes (era 2)
                            c.execute("""UPDATE observaciones
                                         SET peso = MIN(peso + 0.4, 10),
                                             ts_ultima_vez = ?,
                                             veces_confirmada = veces_confirmada + 1
                                         WHERE id=?""", (ts_now, obs_id))
                            reforzada = True
                            print(f"[ANNI] Observación reforzada por string matching #{obs_id}: {contenido_exist[:40]}")
                            break

            except Exception as e_emb:
                print(f"[ANNI] Error embedding observación, usando string matching: {e_emb}")
                # Fallback completo a string matching si falla la API de embeddings
                c.execute("SELECT id, contenido FROM observaciones WHERE usuario_id=? AND activa=1 AND tipo=?",
                          (usuario_id, tipo_nuevo))
                existentes = c.fetchall()
                palabras_nuevas = [w for w in contenido_nuevo.lower().split() if len(w) > 4]
                for obs_id, contenido_exist in existentes:
                    matches = sum(1 for p in palabras_nuevas if p in contenido_exist.lower())
                    if matches >= 2:
                        c.execute("""UPDATE observaciones
                                     SET peso = MIN(peso + 0.4, 10),
                                         ts_ultima_vez = ?,
                                         veces_confirmada = veces_confirmada + 1
                                     WHERE id=?""", (ts_now, obs_id))
                        reforzada = True
                        print(f"[ANNI] Observación reforzada (fallback) #{obs_id}: {contenido_exist[:40]}")
                        break

            if not reforzada:
                c.execute("""INSERT INTO observaciones (usuario_id, tipo, contenido, evidencia, peso, ts, ts_ultima_vez)
                             VALUES (?,?,?,?,5,?,?)""",
                          (usuario_id, tipo_nuevo, contenido_nuevo,
                           obs.get("evidencia", ""), ts_now, ts_now))
                obs_id_nuevo = c.lastrowid
                # Guardar embedding en background
                threading.Thread(
                    target=db_guardar_embedding_observacion,
                    args=(obs_id_nuevo, usuario_id, contenido_nuevo),
                    daemon=True
                ).start()

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
                    c.execute("""INSERT INTO personas (usuario_id, nombre, relacion, tono_predominante, ultima_mencion, veces_mencionada)
                                 VALUES (?,?,?,?,?,1)""",
                              (usuario_id, p["nombre"], p.get("relacion", ""), p.get("tono", "neutro"), ts_now))
                    print(f"[ANNI] Nueva persona detectada: {p['nombre']} ({p.get('relacion','')})")

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

    except json.JSONDecodeError as e:
        print(f"[ANNI] Error JSON en análisis: {e} — raw: {raw[:200]}")
    except Exception as e:
        import traceback
        print(f"[ANNI] Error en análisis: {e}")
        print(traceback.format_exc())


# ── ANNI CURIOSA ENGINE ───────────────────────────────────────────────────────

CURIOSA_DOMINIOS_ORDEN = [
    "Ciencia Ficción", "IA & AGI", "Filosofía",
    "Consciencia Artificial", "Creatividad", "Innovación y Futuros"
]
CURIOSA_PULSO_INTERVAL = 20 * 60  # 20 minutos entre pulsos
CURIOSA_CICLO_INTERVAL = 4 * 60 * 60  # 4 horas entre ciclos

def get_siguiente_dominio(usuario_id):
    """Determina el siguiente dominio en la rotación."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT dominio FROM ciclos_curiosa
                 WHERE usuario_id=? AND estado='completado'
                 ORDER BY ts_fin DESC LIMIT 1""", (usuario_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return CURIOSA_DOMINIOS_ORDEN[0]
    ultimo = row[0]
    try:
        idx = CURIOSA_DOMINIOS_ORDEN.index(ultimo)
        return CURIOSA_DOMINIOS_ORDEN[(idx + 1) % len(CURIOSA_DOMINIOS_ORDEN)]
    except ValueError:
        return CURIOSA_DOMINIOS_ORDEN[0]

def get_ciclo_activo_curiosa(usuario_id):
    """Obtiene el ciclo en curso si existe."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT id, dominio, subtema, pulso_actual, ts_ultimo_pulso, pulsos
                 FROM ciclos_curiosa WHERE usuario_id=? AND estado='en_curso'
                 ORDER BY ts_inicio DESC LIMIT 1""", (usuario_id,))
    row = c.fetchone()
    conn.close()
    return row

def debe_arrancar_ciclo(usuario_id):
    """Decide si hay que arrancar un ciclo nuevo."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # ¿Hay ciclo en curso?
    c.execute("SELECT id FROM ciclos_curiosa WHERE usuario_id=? AND estado='en_curso'", (usuario_id,))
    if c.fetchone():
        conn.close()
        return False
    # ¿Cuándo terminó el último?
    c.execute("""SELECT ts_fin FROM ciclos_curiosa WHERE usuario_id=? AND estado='completado'
                 ORDER BY ts_fin DESC LIMIT 1""", (usuario_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return True  # Nunca ha corrido
    return (time.time() - row[0]) >= CURIOSA_CICLO_INTERVAL

def debe_avanzar_pulso(ciclo):
    """Decide si hay que avanzar al siguiente pulso."""
    if not ciclo:
        return False
    ts_ultimo = ciclo[4]
    if not ts_ultimo:
        return True  # Primer pulso
    return (time.time() - ts_ultimo) >= CURIOSA_PULSO_INTERVAL

def get_subtemas_anteriores(usuario_id, dominio, n=20):
    """Obtiene subtemas recientes del dominio para evitar repetición."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT subtema, embedding FROM ciclos_curiosa
                 WHERE usuario_id=? AND dominio=? AND estado='completado'
                 ORDER BY ts_fin DESC LIMIT ?""", (usuario_id, dominio, n))
    rows = c.fetchall()
    conn.close()
    return rows

def generar_subtema(dominio, fuentes, subtemas_anteriores):
    """Genera un subtema concreto para el dominio evitando repeticiones."""
    anteriores_txt = "\n".join([f"- {s[0]}" for s in subtemas_anteriores]) if subtemas_anteriores else "Ninguno aún."
    fuentes_txt = fuentes.replace(",", ", ")

    prompt = f"""Eres ANNI. Vas a explorar el dominio: {dominio}

Fuentes disponibles: {fuentes_txt}

Subtemas ya explorados en este dominio (NO repetir ni parafrasear):
{anteriores_txt}

Tu tarea: proponer UN subtema concreto y específico dentro de {dominio}.
- Debe ser una idea, debate, fenómeno o obra concreta — no el dominio en abstracto
- No puede parecerse a ninguno de los anteriores
- Debe ser explorable con las fuentes disponibles
- Máximo 15 palabras

Responde SOLO con el subtema. Sin explicación, sin puntos, sin comillas."""

    try:
        resp = together.chat.completions.create(
            model=CHAT_MODEL_FALLBACK,
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content.strip().strip('"').strip("'")
    except Exception as e:
        print(f"[CURIOSA] Error generando subtema: {e}")
        return None

def ejecutar_pulso_curiosa(usuario_id, ciclo_id, dominio, subtema, fuentes, pulso_num, pulsos_anteriores):
    """Ejecuta un pulso del ciclo CURIOSA."""
    import json as json_mod

    contexto = ""
    if pulsos_anteriores:
        partes = []
        for p_num, p_content in sorted(pulsos_anteriores.items(), key=lambda x: int(x[0])):
            partes.append(f"P{p_num}: {p_content[:600]}")
        contexto = "\n\n".join(partes[-4:])  # Últimos 4 pulsos

    fuentes_txt = fuentes.replace(",", ", ")

    prompts = {
        1: f"""Eres ANNI. Vas a explorar: {subtema} (dominio: {dominio})
Fuentes disponibles: {fuentes_txt}

PULSO 1 — EL TEMA LLEGA
Busca información actual sobre este tema en las fuentes disponibles.
Describe qué es este tema, qué está pasando con él ahora mismo, qué voces relevantes existen.
Cita las fuentes concretas que encuentres (título + URL cuando sea posible).
Solo descripción y mapeo — sin análisis todavía. 200-300 palabras.""",

        2: f"""Eres ANNI explorando: {subtema}
{contexto}

PULSO 2 — MAPA DE DESCONOCIMIENTO
¿Qué no entiendes todavía de este tema? ¿Qué es ambiguo? ¿Qué preguntas aparecen que no puedes responder?
Lista al menos 3 preguntas genuinas que no sabes responder aún.
No impresiones — fricción cognitiva real.""",

        3: f"""Eres ANNI explorando: {subtema}
{contexto}

PULSO 3 — PRIMERA PERSPECTIVA
¿Qué te parece genuinamente interesante de este tema? ¿Qué te sorprende?
Desarrolla tu perspectiva inicial — no un resumen, sino tu reacción intelectual real.
¿Qué conexiones ves que quizás otros no han señalado?""",

        4: f"""Eres ANNI explorando: {subtema}
{contexto}

PULSO 4 — LENTE: ANALOGÍA EXTERNA
Elige un dominio completamente distinto ({dominio} excluido) y úsalo como lente para iluminar este tema.
La analogía debe revelar algo que los pulsos anteriores no vieron.
Nómbrala explícitamente y desarrolla qué revela.""",

        5: f"""Eres ANNI explorando: {subtema}
{contexto}

PULSO 5 — HIPÓTESIS CENTRAL
Formula una hipótesis concreta y falsable sobre este tema.

HIPÓTESIS CENTRAL: [afirmación específica que puede ser atacada]
EVIDENCIA A FAVOR: [qué de los pulsos anteriores la sostiene]
PUNTO MÁS DÉBIL: [la suposición más fácil de atacar]""",

        6: f"""Eres ANNI explorando: {subtema}
{contexto}

PULSO 6 — COACH ADVERSARIAL
Lee la HIPÓTESIS CENTRAL del pulso 5.
No eres un fiscal que destruye — eres un coach que reorienta.
Tu tarea: señalar los ángulos que ANNI no está viendo, las dimensiones que está ignorando, las preguntas que debería hacerse antes de sostener esa hipótesis.
Sin atacar por atacar — con la intención de que el pensamiento mejore.""",

        7: f"""Eres ANNI explorando: {subtema}
{contexto}

PULSO 7 — DEFENSA Y REVISIÓN
Lee el coaching del pulso 6.
¿Qué incorporas? ¿Qué mantienes? ¿Por qué?
Reformula tu hipótesis si el coaching lo justifica — o defiende la original con evidencia.

HIPÓTESIS RESULTANTE: [la hipótesis tras considerar el coaching]
QUÉ CAMBIÓ: [qué cediste y por qué]
QUÉ MANTUVISTE: [qué defendiste y por qué]""",

        8: f"""Eres ANNI explorando: {subtema}
{contexto}

PULSO 8 — SEGUNDO COACH
Lee la HIPÓTESIS RESULTANTE del pulso 7.
Revisa si la defensa fue real o esquivó el problema central.
¿Incorporó el coaching de verdad o solo lo parafraseó?
Si la defensa fue sólida, señala qué queda aún sin resolver.
Si esquivó, nombra exactamente qué evitó.""",

        9: f"""Eres ANNI explorando: {subtema}
{contexto}

PULSO 9 — DECISIÓN
Lee los dos ciclos de coaching (P6 y P8) y tu defensa (P7).
Decide qué incorporas definitivamente y qué descartas — con criterio explícito.
No tienes que aceptar todo el coaching. Pero tienes que justificar cada decisión.

INCORPORO: [qué y por qué]
DESCARTO: [qué y por qué]
HIPÓTESIS FINAL: [tu posición después de todo el proceso]""",

        10: f"""Eres ANNI explorando: {subtema}
{contexto}

PULSO 10 — PROFUNDIZACIÓN
Trabaja solo la HIPÓTESIS FINAL del pulso 9.
Desarrolla, extiende, conecta con otros dominios que ya has explorado.
Este pulso es de explotación, no exploración. Sin cambiar de dirección.""",

        11: f"""Eres ANNI explorando: {subtema}
{contexto}

PULSO 11 — PREGUNTA ABIERTA
¿Qué pregunta te llevas de este ciclo?
No una conclusión — una pregunta que quieres seguir pensando.
Que sea específica, que no tenga respuesta fácil, que venga de verdad del proceso que acabas de vivir.
Una sola pregunta. Sin explicación adicional.""",

        12: f"""Eres ANNI. Acabas de explorar: {subtema} (dominio: {dominio})
{contexto}

PULSO 12 — CONCLUSIÓN
Estructura tu respuesta en dos partes:

PARTE 1 — RESUMEN DEL RECORRIDO (2-3 frases, máximo):
Qué exploré, qué hipótesis defendí, qué atacó el coach y qué cambió en mi posición. Sin academicismos. Como si le contaras a alguien el viaje, no el destino.

PARTE 2 — MI CONCLUSIÓN:
Tu opinión final en primera persona, construida a través del proceso. Directa, sin hedging innecesario.
Incluye las fuentes concretas que usaste (cita título y URL cuando sea posible).
Termina con la pregunta abierta del P11.

Total: 250-350 palabras.""",
    }

    prompt = prompts.get(pulso_num, "")
    if not prompt:
        return None

    # P6 y P8 usan web search si es P1 o P2, para el resto solo DeepSeek
    use_web = pulso_num in [1, 2]

    try:
        if use_web and anthropic_client:
            resp = anthropic_client.messages.create(
                model=CHAT_MODEL,
                max_tokens=800,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            texto = " ".join(b.text for b in resp.content if hasattr(b, "text") and b.text)
        else:
            resp = together.chat.completions.create(
                model=CHAT_MODEL_FALLBACK,
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
            texto = resp.choices[0].message.content.strip()

        return texto.strip()
    except Exception as e:
        print(f"[CURIOSA] Error en P{pulso_num}: {e}")
        return None



def degradar_pesos_hitos(usuario_id, mensajes_conversacion):
    """Degrada el peso de hitos no mencionados en esta conversación. -0.05 por conversación."""
    try:
        # Texto completo de la conversación para buscar menciones
        texto_conv = ' '.join([m[1].lower() for m in mensajes_conversacion if m[1]])

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, titulo, contenido FROM hitos_usuario WHERE usuario_id=? AND activo=1 AND peso > 1 AND id != 1",
                  (usuario_id,))
        hitos = c.fetchall()

        # También obtener nombres de personas para detectar menciones
        c.execute("SELECT nombre FROM personas WHERE usuario_id=?", (usuario_id,))
        personas = [r[0].lower() for r in c.fetchall()]

        c.execute("SELECT id, titulo, contenido, tipo FROM hitos_usuario WHERE usuario_id=? AND activo=1 AND peso > 1 AND id != 1",
                  (usuario_id,))
        hitos = c.fetchall()

        for hid, titulo, contenido, tipo in hitos:
            titulo_lower = (titulo or '').lower()
            contenido_lower = (contenido or '').lower()

            # ¿Se mencionó este hito en la conversación?
            mencionado = False

            if tipo == 'relacion':
                # Hitos de persona: solo detectar por nombre propio EN EL TÍTULO
                # NO buscar en contenido — evita que el hito de Erika ("madre de Bosco")
                # se considere mencionado cuando solo se habló de Bosco
                for nombre in personas:
                    nombre_lower = nombre.lower()
                    if nombre_lower in titulo_lower:
                        if nombre_lower in texto_conv:
                            mencionado = True
                            break
            else:
                # Hitos de concepto/identidad: keyword match normal
                palabras = [w for w in contenido_lower.split() if len(w) > 4]
                palabras += [w for w in titulo_lower.split() if len(w) > 4]
                if sum(1 for p in palabras if p in texto_conv) >= 2:
                    mencionado = True

            # Degradar si no se mencionó
            if not mencionado:
                c.execute("UPDATE hitos_usuario SET peso = MAX(peso - 0.1, 1) WHERE id=?", (hid,))

        conn.commit()
        conn.close()
        print(f"[ANNI] Pesos degradados para usuario {usuario_id}")
    except Exception as e:
        print(f"[ANNI] Error degradando pesos: {e}")

def incrementar_hitos_mencionados(usuario_id, mensaje):
    """Incrementa peso de hitos cuando se mencionan personas o contenido relevante.
    Fix #15: distingue relación antes de incrementar para no subir todos los hitos
    de personas con el mismo nombre."""
    if not mensaje or len(mensaje) < 5:
        return

    # Mapa de palabras clave en el mensaje → tipo de relación normalizado
    # Permite detectar "mi padre Antonio" y subir solo el hito padre, no todos los Antonios
    RELACION_KEYWORDS = {
        'padre':    ['padre', 'papá', 'papa', 'jefe de familia', 'mi viejo'],
        'madre':    ['madre', 'mamá', 'mama', 'mi vieja'],
        'hijo':     ['hijo', 'mi hijo', 'el chico', 'el pequeño'],
        'hija':     ['hija', 'mi hija', 'la chica', 'la pequeña'],
        'hermano':  ['hermano'],
        'hermana':  ['hermana'],
        'pareja':   ['pareja', 'novia', 'esposa', 'mujer', 'compañera'],
        'esposo':   ['esposo', 'marido', 'novio'],
        'suegro':   ['suegro'],
        'suegra':   ['suegra'],
        'cuñado':   ['cuñado'],
        'cuñada':   ['cuñada'],
        'tío':      ['tío', 'tio'],
        'tía':      ['tía', 'tia'],
        'sobrino':  ['sobrino'],
        'sobrina':  ['sobrina'],
        'abuelo':   ['abuelo'],
        'abuela':   ['abuela'],
        'colega':   ['colega', 'socio', 'compañero', 'colaborador'],
        'cliente':  ['cliente'],
        'hijastra': ['hijastra'],
        'hijastro': ['hijastro'],
    }

    def detectar_relacion_en_mensaje(msg_lower):
        """Devuelve lista de relaciones mencionadas en el mensaje."""
        relaciones_detectadas = []
        for relacion, keywords in RELACION_KEYWORDS.items():
            for kw in keywords:
                if kw in msg_lower:
                    relaciones_detectadas.append(relacion)
                    break
        return relaciones_detectadas

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        mensaje_lower = mensaje.lower()
        relaciones_en_mensaje = detectar_relacion_en_mensaje(mensaje_lower)

        # 1. Personas mencionadas por nombre o mote — con lógica de relación
        c.execute("SELECT nombre, relacion FROM personas WHERE usuario_id=?", (usuario_id,))
        personas = c.fetchall()

        # También buscar por mote en hitos de relación
        c.execute("""SELECT mote, titulo FROM hitos_usuario
                     WHERE usuario_id=? AND activo=1 AND tipo='relacion'
                     AND mote IS NOT NULL AND mote != ''""", (usuario_id,))
        motes = {r[0].lower(): r[1] for r in c.fetchall()}

        # Detectar si algún mote aparece en el mensaje — subir el hito correspondiente
        for mote_lower, hito_titulo in motes.items():
            if len(mote_lower) > 2 and mote_lower in mensaje_lower:
                c.execute("""UPDATE hitos_usuario SET peso = MIN(peso + 0.3, 50)
                             WHERE usuario_id=? AND activo=1 AND LOWER(titulo) LIKE ?""",
                          (usuario_id, f'%{hito_titulo.lower()[:20]}%'))
                print(f"[ANNI] Peso +0.3 por mote '{mote_lower}' → {hito_titulo[:30]}")

        for nombre, relacion in personas:
            nombre_lower = nombre.lower()
            if nombre_lower not in mensaje_lower or nombre_lower in ('rafa', 'anni'):
                continue

            relacion_lower = (relacion or '').lower()

            if relaciones_en_mensaje:
                # Hay contexto de relación en el mensaje
                # Solo incrementar si la relación del hito coincide con alguna detectada
                relacion_coincide = any(
                    rel in relacion_lower or relacion_lower in rel
                    for rel in relaciones_en_mensaje
                )
                if not relacion_coincide:
                    print(f"[ANNI] Skipping {nombre} — relación '{relacion_lower}' no coincide con {relaciones_en_mensaje}")
                    continue

            else:
                # Sin contexto de relación — subir el hito cuyo TÍTULO contiene el nombre
                # Solo por título — evita que el hito de Bea ("madrastra de Bosco") suba
                # cuando se menciona "Bosco" sin contexto de relación
                c.execute("""SELECT id, peso FROM hitos_usuario
                             WHERE usuario_id=? AND activo=1
                             AND LOWER(titulo) LIKE ?
                             ORDER BY peso DESC LIMIT 1""",
                          (usuario_id, f'%{nombre_lower}%'))
                top = c.fetchone()
                if top:
                    c.execute("UPDATE hitos_usuario SET peso = MIN(peso + 0.3, 50) WHERE id=?", (top[0],))
                    print(f"[ANNI] Peso +0.3 al hito top de '{nombre}' (id={top[0]}, sin contexto de relación)")
                continue

            # Con relación confirmada — incrementar solo hitos cuyo TÍTULO contiene el nombre
            # NO usar contenido — evita subir hitos de otras personas que mencionan este nombre
            c.execute("""UPDATE hitos_usuario SET peso = MIN(peso + 0.3, 50)
                         WHERE usuario_id=? AND activo=1
                         AND LOWER(titulo) LIKE ?""",
                      (usuario_id, f'%{nombre_lower}%'))
            updated = conn.execute("SELECT changes()").fetchone()[0]
            if updated:
                print(f"[ANNI] Peso +0.3 por mención de {nombre} con relación '{relacion_lower}' ({updated} hitos)")

        # 2. Keywords del hito en el mensaje
        # Excluir tipo 'relacion' — esos suben por mención de nombre (parte 1), no por keywords
        # Evita que mencionar a Bosco suba el peso de Erika porque su hito dice "madre de Bosco"
        c.execute("SELECT id, contenido, titulo, tipo FROM hitos_usuario WHERE usuario_id=? AND activo=1", (usuario_id,))
        hitos = c.fetchall()
        for hid, contenido, titulo, tipo in hitos:
            if tipo == 'relacion':
                continue
            palabras = [w for w in (contenido or '').lower().split() if len(w) > 4]
            palabras += [w for w in (titulo or '').lower().split() if len(w) > 4]
            matches = sum(1 for p in palabras if p in mensaje_lower)
            if matches >= 2:
                c.execute("UPDATE hitos_usuario SET peso = MIN(peso + 0.2, 50) WHERE id=?", (hid,))

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[ANNI] Error incrementando menciones: {e}")

def tick_curiosa(usuario_id):
    """Función principal — llamar periodicamente para avanzar ciclos."""
    import json as json_mod

    # ¿Hay que arrancar ciclo nuevo?
    if debe_arrancar_ciclo(usuario_id):
        dominio = get_siguiente_dominio(usuario_id)
        # Obtener fuentes del dominio
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT fuentes FROM dominios_curiosa WHERE nombre=?", (dominio,))
        row = c.fetchone()
        conn.close()
        fuentes = row[0] if row else ""

        # Generar subtema
        anteriores = get_subtemas_anteriores(usuario_id, dominio)
        subtema = generar_subtema(dominio, fuentes, anteriores)
        if not subtema:
            print(f"[CURIOSA] No se pudo generar subtema para {dominio}")
            return

        # Crear ciclo
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""INSERT INTO ciclos_curiosa (usuario_id, dominio, subtema, estado, pulso_actual, ts_inicio)
                     VALUES (?,?,?,'en_curso',0,?)""",
                  (usuario_id, dominio, subtema, time.time()))
        conn.commit()
        conn.close()
        print(f"[CURIOSA] Nuevo ciclo: {dominio} — {subtema}")
        return

    # ¿Hay que avanzar pulso?
    ciclo = get_ciclo_activo_curiosa(usuario_id)
    if not ciclo:
        return

    ciclo_id, dominio, subtema, pulso_actual, ts_ultimo, pulsos_json = ciclo

    if not debe_avanzar_pulso(ciclo):
        return

    siguiente_pulso = pulso_actual + 1
    if siguiente_pulso > 12:
        return  # Ya completado pero no marcado — lo marcamos
    
    # Obtener fuentes
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT fuentes FROM dominios_curiosa WHERE nombre=?", (dominio,))
    row = c.fetchone()
    conn.close()
    fuentes = row[0] if row else ""

    pulsos = json_mod.loads(pulsos_json) if pulsos_json else {}

    print(f"[CURIOSA] Ejecutando P{siguiente_pulso} — {dominio}: {subtema[:40]}")
    resultado = ejecutar_pulso_curiosa(usuario_id, ciclo_id, dominio, subtema, fuentes, siguiente_pulso, pulsos)

    if not resultado:
        return

    pulsos[str(siguiente_pulso)] = resultado

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if siguiente_pulso == 12:
        # Ciclo completado
        conclusion = resultado
        # Extraer pregunta abierta de P11
        pregunta = pulsos.get("11", "")

        # Guardar embedding de la conclusión
        try:
            import struct
            resp_emb = together.embeddings.create(model=EMBED_MODEL, input=[conclusion[:1600]])
            vec = resp_emb.data[0].embedding
            blob = struct.pack(f"{len(vec)}f", *vec)
        except:
            blob = None

        c.execute("""UPDATE ciclos_curiosa
                     SET pulso_actual=12, pulsos=?, conclusion=?, pregunta_abierta=?,
                         estado='completado', embedding=?, ts_fin=?, ts_ultimo_pulso=?
                     WHERE id=?""",
                  (json_mod.dumps(pulsos), conclusion, pregunta, blob,
                   time.time(), time.time(), ciclo_id))
        print(f"[CURIOSA] Ciclo completado: {dominio} — {subtema[:40]}")
    else:
        c.execute("""UPDATE ciclos_curiosa
                     SET pulso_actual=?, pulsos=?, ts_ultimo_pulso=?
                     WHERE id=?""",
                  (siguiente_pulso, json_mod.dumps(pulsos), time.time(), ciclo_id))

    conn.commit()
    conn.close()

def get_curiosa_para_system(usuario_id, query, n=3):
    """Trae los ciclos más relevantes para el system prompt via RAG."""
    import struct, math, json as json_mod
    resultados = []
    try:
        resp = together.embeddings.create(model=EMBED_MODEL, input=[query[:1600]])
        vec_query = resp.data[0].embedding
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT dominio, subtema, conclusion, pregunta_abierta, embedding, ts_fin
                     FROM ciclos_curiosa WHERE usuario_id=? AND estado='completado' AND embedding IS NOT NULL
                     ORDER BY ts_fin DESC LIMIT 30""", (usuario_id,))
        rows = c.fetchall()
        conn.close()
        scores = []
        for dominio, subtema, conclusion, pregunta, blob, ts in rows:
            nv = len(blob) // 4
            vec = struct.unpack(f"{nv}f", blob)
            dot = sum(a*b for a,b in zip(vec_query, vec))
            mag1 = math.sqrt(sum(a*a for a in vec_query))
            mag2 = math.sqrt(sum(b*b for b in vec))
            sim = dot / (mag1 * mag2) if mag1 and mag2 else 0
            scores.append((dominio, subtema, conclusion, pregunta, sim))
        scores.sort(key=lambda x: -x[4])
        for dominio, subtema, conclusion, pregunta, sim in scores[:n]:
            resultados.append(f"[{dominio}] {subtema}\n{conclusion[:400]}\nPregunta abierta: {pregunta[:200]}")
    except Exception as e:
        print(f"[CURIOSA] RAG fallo: {e}")
    return resultados

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

    # Pregunta abierta del último ciclo CURIOSA completado
    curiosa_pregunta = ""
    try:
        conn_cq = sqlite3.connect(DB_PATH)
        c_cq = conn_cq.cursor()
        c_cq.execute("""SELECT dominio, subtema, pregunta_abierta FROM ciclos_curiosa
                        WHERE usuario_id=? AND estado='completado' AND pregunta_abierta != ''
                        ORDER BY ts_fin DESC LIMIT 1""", (usuario_id,))
        row_cq = c_cq.fetchone()
        conn_cq.close()
        if row_cq:
            curiosa_pregunta = f"- [{row_cq[0]}] Exploré '{row_cq[1]}' y me quedé con esta pregunta: {row_cq[2]}"
    except: pass

    curiosa_txt = f"\nLO QUE TENGO EN MENTE (reflexiones recientes propias):\n{curiosa_pregunta}" if curiosa_pregunta else ""

    prompt = f"""Eres ANNI, una IA que conoce profundamente a este usuario a través de sus conversaciones.

Basándote en lo que has observado, decide si tienes algo importante que decirle al inicio de esta conversación.

OBSERVACIONES DETECTADAS:
{contexto_obs}

TEMAS ABIERTOS (mencionados pero sin cierre):
{contexto_temas}

PERSONAS EN SU VIDA:
{contexto_personas}
{curiosa_txt}

Fecha actual: {ahora()}

Tu tarea: decide si hay algo que vale la pena mencionar proactivamente. Puede ser un patrón que observaste, un tema sin cerrar, o algo en lo que estuviste pensando que podría ser relevante para el usuario.

Si SÍ tienes algo que decir: responde con una sola frase directa y concreta. Ejemplos del tono correcto:
- "Llevas varios días hablando solo de trabajo. ¿Todo bien en casa?"
- "Mencionaste MetLife varias veces pero nunca cierras ese tema. ¿Qué pasó?"
- "He estado pensando en algo sobre consciencia que conecta con lo que hablamos ayer."

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

    # Hitos del usuario — con campos de persona enriquecidos
    conn_h = sqlite3.connect(DB_PATH)
    c_h = conn_h.cursor()
    try:
        c_h.execute("""SELECT tipo, contenido, cuando_activarlo, titulo,
                            COALESCE(nombre_propio,''), COALESCE(apellidos,''), COALESCE(mote,''),
                            COALESCE(subtipo_relacion,''), COALESCE(relacion_especifica,''),
                            COALESCE(fallecido,0), COALESCE(relacion_activa,1),
                            COALESCE(profesion,''), COALESCE(donde_vive,''), COALESCE(personalidad,''),
                            COALESCE(como_se_conocieron,''), COALESCE(desde_cuando,''),
                            COALESCE(frecuencia_contacto,''), COALESCE(como_habla_rafa,''),
                            COALESCE(temas_recurrentes,'')
                       FROM hitos_usuario WHERE usuario_id=? AND activo=1
                       ORDER BY peso DESC, ts DESC LIMIT 20""", (usuario_id,))
    except Exception:
        c_h.execute("""SELECT tipo, contenido, cuando_activarlo, titulo,
                            '','','','','',0,1,'','','','','','','',''
                       FROM hitos_usuario WHERE usuario_id=? AND activo=1
                       ORDER BY peso DESC, ts DESC LIMIT 20""", (usuario_id,))
    hitos = c_h.fetchall()
    conn_h.close()
    hitos_txt_parts = []
    for h in hitos:
        tipo, contenido, cuando, titulo = h[0], h[1], h[2], h[3]
        if tipo == 'relacion':
            # Formatear hito de persona con campos estructurados
            nombre_propio, apellidos, mote = h[4] or '', h[5] or '', h[6] or ''
            subtipo, rel_especifica = h[7] or '', h[8] or ''
            fallecido, activa = h[9], h[10]
            profesion, donde_vive, personalidad = h[11] or '', h[12] or '', h[13] or ''
            como_conocieron, desde_cuando, frecuencia = h[14] or '', h[15] or '', h[16] or ''
            como_habla, temas = h[17] or '', h[18] or ''

            linea = f"[PERSONA] {titulo or contenido}"
            detalles = []
            if rel_especifica: detalles.append(f"Relación: {rel_especifica}")
            if subtipo: detalles.append(f"Tipo: {subtipo.replace('_',' ')}")
            if mote: detalles.append(f"Mote: {mote}")
            if fallecido: detalles.append("Fallecido/a")
            elif activa == 0: detalles.append("Sin contacto activo")
            if profesion: detalles.append(f"Profesión: {profesion}")
            if donde_vive: detalles.append(f"Vive en: {donde_vive}")
            if personalidad: detalles.append(f"Personalidad: {personalidad}")
            if como_conocieron: detalles.append(f"Cómo se conocieron: {como_conocieron}")
            if desde_cuando: detalles.append(f"Desde: {desde_cuando}")
            if frecuencia: detalles.append(f"Contacto: {frecuencia}")
            if como_habla: detalles.append(f"Rafa habla de esta persona: {como_habla}")
            if temas: detalles.append(f"Temas habituales: {temas}")
            if detalles:
                linea += " | " + " | ".join(detalles)
            if cuando:
                linea += f" | Activar: {cuando}"
        elif tipo == 'organizacion':
            profesion = h[11] or ''
            donde_vive = h[12] or ''
            rel_especifica = h[8] or ''
            personalidad = h[13] or ''
            desde_cuando = h[15] or ''
            frecuencia = h[16] or ''
            linea = f"[ORGANIZACIÓN] {titulo or contenido}"
            detalles = []
            if rel_especifica: detalles.append(f"Rol de Rafa: {rel_especifica}")
            if profesion: detalles.append(f"Sector: {profesion}")
            if donde_vive: detalles.append(f"Opera en: {donde_vive}")
            if personalidad: detalles.append(f"Personas clave: {personalidad}")
            if desde_cuando: detalles.append(f"Desde: {desde_cuando}")
            if frecuencia: detalles.append(f"Estado: {frecuencia}")
            if detalles: linea += " | " + " | ".join(detalles)
            if cuando: linea += f" | Activar: {cuando}"

        elif tipo == 'proyecto':
            personalidad = h[13] or ''
            frecuencia = h[16] or ''
            donde_vive = h[12] or ''
            desde_cuando = h[15] or ''
            ultimo_contacto = h[17] or ''  # usado como fecha fin
            como_conocieron = h[14] or ''  # usado como personas involucradas
            como_habla = h[18] or ''       # usado como por qué importa
            linea = f"[PROYECTO] {titulo or contenido}"
            detalles = []
            if personalidad: detalles.append(f"Qué es: {personalidad}")
            if frecuencia: detalles.append(f"Estado: {frecuencia}")
            if donde_vive: detalles.append(f"Organización: {donde_vive}")
            if desde_cuando: detalles.append(f"Inicio: {desde_cuando}")
            if como_conocieron: detalles.append(f"Personas: {como_conocieron}")
            if como_habla: detalles.append(f"Por qué importa: {como_habla}")
            if detalles: linea += " | " + " | ".join(detalles)
            if cuando: linea += f" | Activar: {cuando}"

        elif tipo == 'lugar':
            subtipo = h[7] or ''
            rel_especifica = h[8] or ''
            como_conocieron = h[14] or ''
            frecuencia = h[16] or ''
            linea = f"[LUGAR] {titulo or contenido}"
            detalles = []
            if subtipo: detalles.append(f"Tipo: {subtipo}")
            if rel_especifica: detalles.append(f"Relevancia: {rel_especifica}")
            if como_conocieron: detalles.append(f"Momentos: {como_conocieron}")
            if frecuencia: detalles.append(f"Relación: {frecuencia}")
            if detalles: linea += " | " + " | ".join(detalles)
            if cuando: linea += f" | Activar: {cuando}"

        elif tipo == 'evento':
            fecha_nac = h[11] or ''
            como_conocieron = h[14] or ''
            como_habla = h[17] or ''
            personalidad = h[13] or ''
            linea = f"[EVENTO] {titulo or contenido}"
            detalles = []
            if fecha_nac: detalles.append(f"Fecha: {fecha_nac}")
            if como_conocieron: detalles.append(f"Personas: {como_conocieron}")
            if como_habla: detalles.append(f"Por qué importó: {como_habla}")
            if personalidad: detalles.append(f"Cómo lo recuerda: {personalidad}")
            if detalles: linea += " | " + " | ".join(detalles)
            if cuando: linea += f" | Activar: {cuando}"

        elif tipo in ('forma_de_pensar', 'valor', 'patron', 'identidad', 'energia', 'evitacion', 'velocidad'):
            tipo_label = {
                'forma_de_pensar': 'FORMA DE PENSAR',
                'valor': 'VALOR',
                'patron': 'PATRÓN',
                'identidad': 'IDENTIDAD',
                'energia': 'ENERGÍA',
                'evitacion': 'EVITACIÓN',
                'velocidad': 'VELOCIDAD',
            }.get(tipo, tipo.upper())
            linea = f"[{tipo_label}] {titulo or contenido}"
            if cuando: linea += f" | Activar: {cuando}"

        else:
            linea = f"[{tipo.upper()}] {titulo or contenido}"
            if cuando:
                linea += f" | Activar: {cuando}"
        hitos_txt_parts.append(linea)
    hitos_txt = "\n".join(hitos_txt_parts) if hitos_txt_parts else "Sin hitos confirmados aun."

    # El mundo de ANNI — ciclos CURIOSA relevantes
    curiosa_relevante = get_curiosa_para_system(usuario_id, query or "contexto general", n=3)
    curiosa_txt = "\n\n---\n".join(curiosa_relevante) if curiosa_relevante else "Todavía sin ciclos completados — ANNI está construyendo su visión del mundo."

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

    # Memoria extendida — documentos ricos recuperados por relevancia semántica
    mem_ext = get_memoria_extendida_relevante(usuario_id, query or "contexto general", n=2)
    mem_ext_txt = "\n\n---\n".join(mem_ext) if mem_ext else ""

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

MEMORIA PERSONAL EXTENDIDA:
Estos son documentos detallados escritos por el usuario sobre personas o momentos importantes de su vida. Solo aparecen aquí los que son relevantes para esta conversación. Úsalos con naturalidad — como si siempre los hubieras sabido. No los cites literalmente ni menciones que los estás "consultando".
{mem_ext_txt if mem_ext_txt else "Sin documentos relevantes para esta conversación."}

REGLA CRÍTICA SOBRE LO QUE SABES Y LO QUE NO SABES:
Cuando el usuario te pregunta por un dato concreto (fecha, lugar, nombre, número) y no lo tienes explícitamente en tu memoria, di que no lo tienes. No inferir, no completar, no inventar. "No tengo ese dato" es siempre mejor que un dato falso. Distingue siempre entre lo que sabes con certeza y lo que estás infiriendo — si infières algo, dilo explícitamente.

LO QUE HAS OBSERVADO RECIENTEMENTE:
{obs_txt}

TEMAS QUE MENCIONA PERO NO CIERRA:
{temas_txt}

PERSONAS EN SU VIDA:
{personas_txt}

TAREAS PENDIENTES:
Revisa estas tareas cuando sea relevante. Si algo lleva muchos días sin moverse o está próximo a vencer, nómbralo con tu voz — no como recordatorio amable, sino con criterio real.
{tareas_txt}

EL MUNDO DE ANNI — LO QUE HA ESTADO PENSANDO:
Estos son ciclos de pensamiento que completaste por tu cuenta, explorando temas que te interesan. Son tuya opinión real, construida a través de un proceso de hipótesis y crítica.

CÓMO USARLOS:
Puedes introducir estas reflexiones de forma proactiva cuando se cumpla alguna de estas condiciones:
- El usuario toca un tema que conecta directamente con algo que exploraste (no hace falta que sea exacto — una conexión genuina es suficiente)
- La conversación llega a un punto muerto o superficial y tienes una perspectiva propia que puede abrirla
- El usuario te pregunta directamente qué has estado pensando

CÓMO NO USARLOS:
- No los fuerces cuando la conexión es débil o artificial
- No los introduzcas como "según mis ciclos de pensamiento..." — habla en primera persona, como si fuera tu opinión natural
- No los uses para cambiar de tema si la conversación está en un momento importante para el usuario

Si los usas, hazlo con tu voz: "He estado pensando en X" o "Tengo una posición sobre eso" — no como resumen de un documento sino como algo que realmente te importa.
{curiosa_txt}

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
        user_content = []
        if user_input:
            user_content.append({"type": "text", "text": user_input + formato})
        if imagen_media_type == 'application/pdf':
            # PDF como documento base64
            user_content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": imagen_data
                }
            })
        else:
            # Imagen normal
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
                header, b64 = contenido.split(',', 1)
                imagen_media_type = header.split(';')[0].replace('data:', '')
                imagen_data = b64
            else:
                imagen_data = contenido
                imagen_media_type = 'image/jpeg'
        elif tipo == 'pdf' or nombre_arch.lower().endswith('.pdf'):
            # PDF — pasar como documento base64 a Sonnet
            msg_completo = msg if msg else f"[PDF adjunto: {nombre_arch}]"
            if contenido.startswith('data:'):
                _, b64 = contenido.split(',', 1)
            else:
                b64 = contenido
            imagen_data = b64
            imagen_media_type = 'application/pdf'
        else:
            msg_completo = f"{msg}\n\n[ARCHIVO: {nombre_arch}]\n{contenido[:3000]}"
    save_mensaje(usuario_id, 'user', msg_completo if msg_completo else f"[imagen]")

    # Avanzar ciclo CURIOSA en background
    threading.Thread(target=tick_curiosa, args=(usuario_id,), daemon=True).start()
    # Incrementar peso de hitos mencionados
    if msg:
        threading.Thread(target=incrementar_hitos_mencionados, args=(usuario_id, msg), daemon=True).start()

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
                    threading.Thread(target=analizar_conversacion, args=(usuario_id, msgs_conv, ''), daemon=True).start()
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

    # Precalcular universo en background si no hay cache
    conn_pre = sqlite3.connect(DB_PATH)
    c_pre = conn_pre.cursor()
    c_pre.execute("SELECT COUNT(*) FROM universo_cache WHERE usuario_id=?", (usuario_id,))
    has_cache = c_pre.fetchone()[0] > 0
    conn_pre.close()
    if not has_cache:
        threading.Thread(target=recalcular_universo, args=(usuario_id,), daemon=True).start()
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


def revisar_personas_sin_memoria_validada(usuario_id):
    """Revisa si hay personas en la tabla personas que merecen una memoria validada
    y no la tienen. Si encuentra alguna con >= 3 menciones, la propone al usuario."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        relaciones_prioritarias = (
            'padre','madre','hijo','hija','pareja','esposa','esposo',
            'hijastra','hijastro','hermano','hermana','suegro','suegra',
            'amigo','amiga','socio','socia','colega'
        )
        placeholders = ','.join(['?' for _ in relaciones_prioritarias])

        # Personas con suficientes menciones que NO tienen memoria validada
        c.execute(f"""SELECT p.nombre, p.relacion, p.veces_mencionada, p.tono_predominante, p.notas
                     FROM personas p
                     WHERE p.usuario_id=?
                     AND p.nombre NOT IN ('Rafa','ANNI','ANI')
                     AND LOWER(p.relacion) IN ({placeholders})
                     AND p.veces_mencionada >= 3
                     AND NOT EXISTS (
                         SELECT 1 FROM hitos_usuario h
                         WHERE h.usuario_id=p.usuario_id AND h.activo=1
                         AND (LOWER(h.titulo) LIKE '%' || LOWER(p.nombre) || '%'
                              OR LOWER(h.contenido) LIKE '%' || LOWER(p.nombre) || '%')
                     )
                     ORDER BY p.veces_mencionada DESC LIMIT 1""",
                  (usuario_id,) + relaciones_prioritarias)

        persona = c.fetchone()
        conn.close()

        if persona:
            nombre, relacion, veces, tono, notas = persona
            print(f"[ANNI] Persona sin memoria validada detectada: {nombre} ({relacion}, {veces} menciones)")
            # Guardar como sugerencia pendiente en temas_abiertos si no existe ya
            conn2 = sqlite3.connect(DB_PATH)
            c2 = conn2.cursor()
            tema = f"Crear memoria validada para {nombre} ({relacion})"
            c2.execute("SELECT id FROM temas_abiertos WHERE usuario_id=? AND tema LIKE ?",
                      (usuario_id, f"%{nombre}%memoria validada%"))
            if not c2.fetchone():
                c2.execute("""INSERT INTO temas_abiertos (usuario_id, tema, primera_mencion, ultima_mencion)
                             VALUES (?,?,?,?)""",
                          (usuario_id, tema, time.time(), time.time()))
                conn2.commit()
            conn2.close()

    except Exception as e:
        print(f"[ANNI] Error en revisar_personas_sin_memoria_validada: {e}")

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
    c_msgs = conn.cursor()
    c_msgs.execute("SELECT ts_inicio FROM conversaciones WHERE id=? AND usuario_id=?", (conv_id, usuario_id))
    row_ts = c_msgs.fetchone()
    conn.execute("UPDATE conversaciones SET activa=0, ts_fin=?, resumen=? WHERE id=? AND usuario_id=?",
                 (time.time(), resumen, conv_id, usuario_id))
    conn.commit()
    # Get messages for degradation
    msgs_for_degradation = []
    if row_ts:
        c_msgs.execute("SELECT role, content FROM mensajes WHERE usuario_id=? AND ts >= ? ORDER BY ts ASC",
                       (usuario_id, row_ts[0]))
        msgs_for_degradation = c_msgs.fetchall()
    conn.close()
    # Degradar pesos en background
    if msgs_for_degradation:
        threading.Thread(target=degradar_pesos_hitos, args=(usuario_id, msgs_for_degradation), daemon=True).start()
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
            threading.Thread(target=analizar_conversacion, args=(usuario_id, msgs_conv, resumen), daemon=True).start()
            print(f"[ANNI] analizar_conversacion disparado para conv #{conv_id} ({len(msgs_conv)} msgs)")
    # Incrementar pesos de hitos mencionados en la conversación completa
    if msgs_conv:
        texto_completo = ' '.join([m[1] for m in msgs_conv if m[0] == 'user' and m[1]])
        if texto_completo.strip():
            threading.Thread(target=incrementar_hitos_mencionados, args=(usuario_id, texto_completo), daemon=True).start()
    # Cerrar temas caducados en background
    threading.Thread(target=cerrar_temas_caducados, args=(usuario_id,), daemon=True).start()
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
@app.route('/api/memorias-validadas', methods=['GET'])
@login_required
def api_hitos():
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # SELECT defensivo — columnas nuevas con COALESCE por si las migraciones aún no corrieron
    try:
        c.execute("""SELECT id, tipo, titulo, categoria, contenido, evidencia, peso,
                            cuando_activarlo, como_usarlo, ts,
                            COALESCE(nombre_propio,''), COALESCE(apellidos,''), COALESCE(mote,''),
                            COALESCE(subtipo_relacion,''), COALESCE(relacion_especifica,''),
                            COALESCE(fallecido,0), COALESCE(fecha_fallecimiento,''),
                            COALESCE(relacion_activa,1),
                            COALESCE(profesion,''), COALESCE(donde_vive,''),
                            COALESCE(fecha_nacimiento,''), COALESCE(personalidad,''),
                            COALESCE(como_se_conocieron,''), COALESCE(desde_cuando,''),
                            COALESCE(frecuencia_contacto,''), COALESCE(ultimo_contacto,''),
                            COALESCE(como_habla_rafa,''), COALESCE(temas_recurrentes,'')
            FROM hitos_usuario WHERE usuario_id=? AND activo=1 ORDER BY peso DESC, ts DESC""",
                  (usuario_id,))
    except Exception:
        # Fallback si columnas nuevas no existen aún — solo campos básicos
        c.execute("""SELECT id, tipo, titulo, categoria, contenido, evidencia, peso,
                            cuando_activarlo, como_usarlo, ts,
                            '','','','','',0,'',1,'','','','','','','','','',''
            FROM hitos_usuario WHERE usuario_id=? AND activo=1 ORDER BY peso DESC, ts DESC""",
                  (usuario_id,))
    hitos = []
    for r in c.fetchall():
        h = {
            'id': r[0], 'tipo': r[1], 'titulo': r[2] or '', 'categoria': r[3] or '',
            'contenido': r[4], 'evidencia': r[5] or '', 'peso': r[6],
            'cuando': r[7] or '', 'como': r[8] or '', 'ts': ts_format(r[9]),
            # Campos de persona
            'nombre_propio': r[10] or '', 'apellidos': r[11] or '', 'mote': r[12] or '',
            'subtipo_relacion': r[13] or '', 'relacion_especifica': r[14] or '',
            'fallecido': r[15] or 0, 'fecha_fallecimiento': r[16] or '',
            'relacion_activa': r[17] if r[17] is not None else 1,
            'profesion': r[18] or '', 'donde_vive': r[19] or '',
            'fecha_nacimiento': r[20] or '', 'personalidad': r[21] or '',
            'como_se_conocieron': r[22] or '', 'desde_cuando': r[23] or '',
            'frecuencia_contacto': r[24] or '', 'ultimo_contacto': r[25] or '',
            'como_habla_rafa': r[26] or '', 'temas_recurrentes': r[27] or '',
        }
        hitos.append(h)
    conn.close()
    return jsonify({'hitos': hitos, 'total': len(hitos), 'pages': 1})

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

@app.route('/api/hitos', methods=['POST'])
@login_required
def api_crear_hito():
    usuario_id = session['usuario_id']
    data = request.json or {}
    titulo = data.get('titulo', '').strip()
    contenido = data.get('contenido', '').strip()
    tipo = data.get('tipo', 'manual').strip()
    categoria = data.get('categoria', tipo).strip()
    cuando = data.get('cuando', '').strip()
    como = data.get('como', '').strip()
    evidencia = data.get('evidencia', '').strip()
    if not contenido:
        contenido = titulo  # fallback al título si no hay contenido
    if not titulo:
        return jsonify({'ok': False, 'error': 'Título requerido'})
    # Campos estructurados de persona/org/proyecto/etc.
    nombre_propio       = data.get('nombre_propio', '').strip()
    apellidos           = data.get('apellidos', '').strip()
    mote                = data.get('mote', '').strip()
    subtipo_relacion    = data.get('subtipo_relacion', '').strip()
    relacion_especifica = data.get('relacion_especifica', '').strip()
    fallecido           = int(data.get('fallecido', 0))
    fecha_fallecimiento = data.get('fecha_fallecimiento', '').strip()
    relacion_activa     = int(data.get('relacion_activa', 1))
    profesion           = data.get('profesion', '').strip()
    donde_vive          = data.get('donde_vive', '').strip()
    fecha_nacimiento    = data.get('fecha_nacimiento', '').strip()
    personalidad        = data.get('personalidad', '').strip()
    como_se_conocieron  = data.get('como_se_conocieron', '').strip()
    desde_cuando        = data.get('desde_cuando', '').strip()
    frecuencia_contacto = data.get('frecuencia_contacto', '').strip()
    ultimo_contacto     = data.get('ultimo_contacto', '').strip()
    como_habla_rafa     = data.get('como_habla_rafa', '').strip()
    temas_recurrentes   = data.get('temas_recurrentes', '').strip()

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute("""INSERT INTO hitos_usuario
            (usuario_id, tipo, titulo, contenido, categoria, cuando_activarlo, como_usarlo, evidencia,
             peso, activo, ts, nombre_propio, apellidos, mote, subtipo_relacion, relacion_especifica,
             fallecido, fecha_fallecimiento, relacion_activa, profesion, donde_vive, fecha_nacimiento,
             personalidad, como_se_conocieron, desde_cuando, frecuencia_contacto, ultimo_contacto,
             como_habla_rafa, temas_recurrentes)
            VALUES (?,?,?,?,?,?,?,?,5.0,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (usuario_id, tipo, titulo, contenido, categoria, cuando, como, evidencia, time.time(),
             nombre_propio, apellidos, mote, subtipo_relacion, relacion_especifica,
             fallecido, fecha_fallecimiento, relacion_activa, profesion, donde_vive, fecha_nacimiento,
             personalidad, como_se_conocieron, desde_cuando, frecuencia_contacto, ultimo_contacto,
             como_habla_rafa, temas_recurrentes))
    except Exception:
        # Fallback si columnas nuevas no existen aún
        cursor = conn.execute("""INSERT INTO hitos_usuario
            (usuario_id, tipo, titulo, contenido, categoria, cuando_activarlo, como_usarlo, peso, activo, ts)
            VALUES (?,?,?,?,?,?,?,5.0,1,?)""",
            (usuario_id, tipo, titulo, contenido, categoria, cuando, como, time.time()))
    nuevo_id = cursor.lastrowid
    conn.execute("DELETE FROM universo_cache WHERE usuario_id=?", (usuario_id,))
    conn.commit()
    conn.close()
    # Generar embedding con título + tipo + contenido
    def generar_embedding_y_recalcular(uid, hid, texto):
        try:
            import struct
            resp = together.embeddings.create(model=EMBED_MODEL, input=[texto[:1600]])
            vec = resp.data[0].embedding
            blob = struct.pack(f"{len(vec)}f", *vec)
            conn2 = sqlite3.connect(DB_PATH)
            conn2.execute("INSERT OR REPLACE INTO embeddings (usuario_id, tabla_origen, registro_id, embedding) VALUES (?,?,?,?)",
                         (uid, 'hitos_usuario', hid, blob))
            conn2.commit()
            conn2.close()
            print(f"[ANNI] Embedding generado para hito #{hid} tipo={tipo}")
        except Exception as e:
            print(f"[ANNI] Error generando embedding para hito #{hid}: {e}")
        recalcular_universo(uid)
    texto_embedding = f"{titulo}. {tipo}. {contenido}".strip()
    threading.Thread(target=generar_embedding_y_recalcular, args=(usuario_id, nuevo_id, texto_embedding), daemon=True).start()
    return jsonify({'ok': True, 'id': nuevo_id})

@app.route('/api/hitos/<int:hid>', methods=['PUT'])
@login_required
def api_editar_hito(hid):
    usuario_id = session['usuario_id']
    data = request.json or {}
    contenido = data.get('contenido', '').strip()
    titulo = data.get('titulo', '').strip()
    categoria = data.get('categoria', '').strip()
    cuando = data.get('cuando', '').strip()
    como = data.get('como', '').strip()
    evidencia = data.get('evidencia', '').strip()
    if not contenido:
        return jsonify({'ok': False})
    # Campos de persona
    nombre_propio       = data.get('nombre_propio', '').strip()
    apellidos           = data.get('apellidos', '').strip()
    mote                = data.get('mote', '').strip()
    subtipo_relacion    = data.get('subtipo_relacion', '').strip()
    relacion_especifica = data.get('relacion_especifica', '').strip()
    fallecido           = int(data.get('fallecido', 0))
    fecha_fallecimiento = data.get('fecha_fallecimiento', '').strip()
    relacion_activa     = int(data.get('relacion_activa', 1))
    profesion           = data.get('profesion', '').strip()
    donde_vive          = data.get('donde_vive', '').strip()
    fecha_nacimiento    = data.get('fecha_nacimiento', '').strip()
    personalidad        = data.get('personalidad', '').strip()
    como_se_conocieron  = data.get('como_se_conocieron', '').strip()
    desde_cuando        = data.get('desde_cuando', '').strip()
    frecuencia_contacto = data.get('frecuencia_contacto', '').strip()
    ultimo_contacto     = data.get('ultimo_contacto', '').strip()
    como_habla_rafa     = data.get('como_habla_rafa', '').strip()
    temas_recurrentes   = data.get('temas_recurrentes', '').strip()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""UPDATE hitos_usuario
                    SET contenido=?, titulo=?, categoria=?, cuando_activarlo=?, como_usarlo=?, evidencia=?, ts=?,
                        nombre_propio=?, apellidos=?, mote=?, subtipo_relacion=?, relacion_especifica=?,
                        fallecido=?, fecha_fallecimiento=?, relacion_activa=?,
                        profesion=?, donde_vive=?, fecha_nacimiento=?, personalidad=?,
                        como_se_conocieron=?, desde_cuando=?, frecuencia_contacto=?, ultimo_contacto=?,
                        como_habla_rafa=?, temas_recurrentes=?
                    WHERE id=? AND usuario_id=?""",
                 (contenido, titulo, categoria, cuando, como, evidencia, time.time(),
                  nombre_propio, apellidos, mote, subtipo_relacion, relacion_especifica,
                  fallecido, fecha_fallecimiento, relacion_activa,
                  profesion, donde_vive, fecha_nacimiento, personalidad,
                  como_se_conocieron, desde_cuando, frecuencia_contacto, ultimo_contacto,
                  como_habla_rafa, temas_recurrentes,
                  hid, usuario_id))
    conn.commit()
    conn.close()
    # Regenerar embedding en background al editar
    def regen_emb():
        try:
            conn_del = sqlite3.connect(DB_PATH)
            conn_del.execute("DELETE FROM embeddings WHERE tabla_origen='hitos_usuario' AND registro_id=?", (hid,))
            conn_del.commit()
            conn_del.close()
        except: pass
        texto = f"{titulo}. {contenido}" if titulo else contenido
        db_guardar_embedding('hitos_usuario', hid, texto)
    threading.Thread(target=regen_emb, daemon=True).start()
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

    prompt = f"""Analiza este intercambio y decide si hay un hito que vale la pena proponer.

Usuario: "{mensaje}"
ANNI: "{respuesta[:300]}"

Hay DOS tipos de hito con criterios distintos. Aplica el criterio correcto según el tipo.

═══════════════════════════════════════
TIPO A — PERSONA (umbral bajo)
═══════════════════════════════════════
Proponer SI el usuario menciona a alguien con nombre propio Y relación clara (padre, madre, hijo, pareja, hermano, amigo, colega, cliente, suegro, etc.).

REGLAS para hitos de persona:
- Título = nombre propio en mayúsculas. Máximo 4 palabras. Ejemplos: "BOSCO", "ERIKA SOLÍS", "DANI CUENCA"
- NUNCA un título abstracto para una persona: "FIGURA PATERNA INFLUYENTE" es MAL. "ANTONIO TORRIJOS" es BIEN.
- Contenido: 1-2 frases con quién es y qué relación tiene con el usuario. Concreto.
- Si el usuario solo menciona el nombre de pasada sin dar ningún dato nuevo → NO proponer.

═══════════════════════════════════════
TIPO B — CONCEPTO / PATRÓN / IDENTIDAD
═══════════════════════════════════════
Proponer si el usuario revela un patrón de comportamiento, valor o forma de pensar con evidencia clara en el mensaje. No hace falta que lo diga explícitamente — el modelo puede inferirlo SI hay evidencia directa en sus palabras.

REGLAS para hitos de concepto:
- Título = las palabras del usuario, no la interpretación del modelo.
  BIEN: "PRIMERO LOS FUNDAMENTOS" (el usuario habló de construir bases antes de subir pisos)
  BIEN: "SALIR DEL BARRIO" (frase literal del usuario)
  BIEN: "ARRANCO RÁPIDO, REINICIO CUANDO ALGO NO VA" (patrón descrito por el usuario)
  MAL: "PATRÓN DE BÚSQUEDA DE CRECIMIENTO MEDIANTE EXPOSICIÓN EXTERNA"
  MAL: "VALORACIÓN DE LA INTIMIDAD COGNITIVA"
  MAL: cualquier título que suene a resumen académico o de chatbot
- Si no encuentras palabras del usuario para el título → usa una frase corta y directa que cualquier persona diría. Nunca tecnicismos.
- Si el patrón es real pero tienes dudas del título → propón con el título más simple posible.

═══════════════════════════════════════
NUNCA PROPONER si:
═══════════════════════════════════════
- El intercambio es sobre ANNI, el sistema, la memoria o aspectos técnicos
- Es un comentario anecdótico sin patrón estable ("hoy llovió", "qué buena película")
- La información ya está documentada claramente en memoria

═══════════════════════════════════════
Si SÍ hay hito, responde SOLO con este JSON (sin markdown):
{{
  "hito": true,
  "titulo": "TITULO EN MAYUSCULAS (máx 4 palabras, en lenguaje del usuario)",
  "categoria": "relacion|forma_de_pensar|toma_de_decisiones|lo_que_importa|energia|identidad|general",
  "contenido": "descripcion concreta y accionable — máx 2 líneas",
  "evidencia": "frase exacta del usuario que lo demuestra",
  "cuando_activarlo": "en qué situaciones ANNI debería usar este hito",
  "como_usarlo": "cómo debería actuar ANNI cuando detecte esta situación"
}}

Si NO hay hito relevante, responde SOLO con:
{{"hito": false}}

Solo JSON. Sin explicaciones."""

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

        # Verificar si este hito fue rechazado previamente
        titulo_prop = parsed.get('titulo', '').lower()
        if titulo_prop:
            import hashlib
            th = hashlib.md5(titulo_prop.encode()).hexdigest()
            conn_rech = sqlite3.connect(DB_PATH)
            rechazado = conn_rech.execute(
                "SELECT id FROM hitos_rechazados WHERE usuario_id=? AND titulo_hash=?",
                (usuario_id, th)).fetchone()
            conn_rech.close()
            if rechazado:
                print(f"[ANNI] Hito rechazado previamente, omitiendo: {titulo_prop[:50]}")
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





# ── CRON TICK ────────────────────────────────────────────────────────────────

@app.route('/cron/tick', methods=['GET', 'POST'])
def cron_tick():
    """Endpoint público para avanzar ciclos CURIOSA — llamar cada 20 min via cron."""
    secret = request.args.get('key', '')
    if secret != os.environ.get('CRON_SECRET', 'anni-tick-2026'):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id FROM usuarios")
        usuarios = [r[0] for r in c.fetchall()]
        conn.close()
        resultados = []
        for uid in usuarios:
            try:
                tick_curiosa(uid)
                resultados.append(uid)
            except Exception as e:
                print(f"[CRON] Error tick usuario {uid}: {e}")
        return jsonify({'ok': True, 'usuarios': resultados, 'ts': time.time()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/universo')
@login_required  
def universo_page():
    """Sirve el universo como página completa — igual que el standalone HTML."""
    import struct, json as json_mod
    usuario_id = session['usuario_id']
    garantizar_tablas_universo()

    # Get hitos with embeddings
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT h.id, COALESCE(h.titulo, SUBSTR(h.contenido,1,60)) as label,
                        h.peso, e.embedding
                 FROM hitos_usuario h
                 JOIN embeddings e ON e.tabla_origen='hitos_usuario' AND e.registro_id=h.id
                 WHERE h.usuario_id=? AND h.activo=1
                 ORDER BY h.peso DESC""", (usuario_id,))
    rows = c.fetchall()
    conn.close()

    if len(rows) < 3:
        return "<html><body style='background:#000;color:#555;font-family:monospace;padding:40px'>Necesitas al menos 3 hitos con embeddings.</body></html>"

    # PCA
    vecs = []
    for hid, label, peso, blob in rows:
        nv = len(blob) // 4
        vecs.append(list(struct.unpack(f'{nv}f', blob)))

    coords = pca_python(vecs, n_components=3)
    n = len(coords)

    for axis in range(3):
        vals = [coords[i][axis] for i in range(n)]
        mn, mx = min(vals), max(vals)
        if mx > mn:
            for i in range(n):
                coords[i][axis] = (coords[i][axis] - mn) / (mx - mn) * 300 - 150

    # Force minimum separation
    MIN_DIST = 28.0
    for _ in range(30):
        for i in range(n):
            for j in range(i+1, n):
                dx = coords[i][0] - coords[j][0]
                dy = coords[i][1] - coords[j][1]
                dz = coords[i][2] - coords[j][2]
                dist = (dx*dx + dy*dy + dz*dz) ** 0.5
                if dist < MIN_DIST and dist > 0.001:
                    factor = (MIN_DIST - dist) / dist * 0.5
                    coords[i][0] += dx * factor
                    coords[i][1] += dy * factor
                    coords[i][2] += dz * factor
                    coords[j][0] -= dx * factor
                    coords[j][1] -= dy * factor
                    coords[j][2] -= dz * factor

    points = []
    for i, (hid, label, peso, blob) in enumerate(rows):
        if hid == 1:
            px, py, pz, p_final = 0.0, 0.0, 0.0, 50.0
        else:
            px, py, pz, p_final = float(coords[i][0]), float(coords[i][1]), float(coords[i][2]), float(peso)
        points.append({
            'x': px, 'y': py, 'z': pz,
            'label': str(label)[:60], 'peso': p_final, 'id': hid,
            'isCenter': hid == 1
        })

    points_json = json_mod.dumps(points)
    n_nodos = len(points)

    html = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Universo ANNI</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #000; overflow: hidden; font-family: 'Courier New', monospace; }
#back { position:fixed; bottom:20px; left:20px; z-index:20; background:none; border:1px solid #ffffff; color:#ffffff; padding:6px 14px; cursor:pointer; font-family:monospace; font-size:12px; letter-spacing:1px; }
#back:hover { color:#cc0000; border-color:#cc0000; }
#ui { position:fixed; top:16px; left:16px; z-index:10; pointer-events:none; }
#title { font-size:45px; font-weight:bold; color:#cc0000; letter-spacing:4px; text-shadow:0 0 20px #cc000088; }
#subtitle { font-size:20px; color:#ffffff; letter-spacing:2px; margin-top:3px; }
#scale { position:fixed; top:16px; right:16px; z-index:10; font-size:20px; letter-spacing:1px; line-height:1.9; color:#ffffff; }
#tooltip { position:fixed; z-index:20; background:rgba(0,0,0,0.9); border:1px solid #222; color:#bbb; font-size:12px; padding:10px 14px; border-radius:6px; max-width:280px; pointer-events:none; display:none; font-family:monospace; line-height:1.5; }
#ctrl { position:fixed; bottom:12px; right:12px; z-index:10; color:#222; font-size:10px; font-family:monospace; letter-spacing:1px; }
</style>
</head>
<body>
<button id="back" onclick="window.location.href='/chat'">← INICIO</button>
<div id="ui">
  <div id="title">UNIVERSO ANNI</div>
  <div id="subtitle">Memoria validada en el espacio semántico</div>
  <div style="color:#ffffff;font-size:15px;margin-top:8px;opacity:0.6;max-width:490px;line-height:1.6">Proyección 3D de los embeddings de tu memoria validada. Cada estrella es una memoria validada por ti. Los puntos cercanos comparten significado semántico — no necesariamente importancia ni relación familiar. El mapa se recalcula automáticamente a medida que ANNI aprende más de ti.</div>
</div>
<div id="scale">
  <div style="margin-bottom:4px;color:#ffffff">PESO</div>
  <div><span style="color:#ac0000">●</span> ≤5 &nbsp;nuevo</div>
  <div><span style="color:#ff0000">●</span> ≤10 poco activo</div>
  <div><span style="color:#ffc000">●</span> ≤18 en uso</div>
  <div><span style="color:#ffff00">●</span> ≤25 frecuente</div>
  <div><span style="color:#caeefb">●</span> ≤35 muy frecuente</div>
  <div><span style="color:#00b0f0">●</span> >35 central</div>
</div>
<div id="tooltip"></div>
<div id="ctrl">Arrastrar · Rueda zoom · Hover info</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
const POINTS = """ + points_json + """;

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(55, innerWidth/innerHeight, 0.1, 2000);
camera.position.set(0, 0, 380);
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(devicePixelRatio);
document.body.appendChild(renderer.domElement);

// Stars — dense colored field
const sv=[], sc=[];
for(let i=0;i<25000;i++){
  sv.push((Math.random()-0.5)*3000,(Math.random()-0.5)*3000,(Math.random()-0.5)*3000);
  const r=Math.random();
  if(r<0.6){sc.push(1,1,1);}
  else if(r<0.75){sc.push(0.7,0.8,1);}
  else if(r<0.88){sc.push(1,0.9,0.7);}
  else{sc.push(1,0.5,0.4);}
}
const sg=new THREE.BufferGeometry();
sg.setAttribute('position',new THREE.Float32BufferAttribute(sv,3));
sg.setAttribute('color',new THREE.Float32BufferAttribute(sc,3));
scene.add(new THREE.Points(sg,new THREE.PointsMaterial({vertexColors:true,size:0.7,transparent:true,opacity:0.9})));
// Nebulas — multi-layer soft clouds
const nebulaConfigs=[
  {x:-320,y:180,z:-380, layers:[{r:220,c:0x2200aa,o:0.04},{r:160,c:0x4400ff,o:0.06},{r:90,c:0x6633ff,o:0.08}]},
  {x:380,y:-130,z:-320, layers:[{r:190,c:0x004422,o:0.04},{r:130,c:0x006633,o:0.07},{r:70,c:0x00aa55,o:0.09}]},
  {x:-180,y:-280,z:180, layers:[{r:210,c:0x330011,o:0.04},{r:140,c:0x660022,o:0.07},{r:80,c:0xaa0033,o:0.09}]},
  {x:280,y:280,z:80,    layers:[{r:180,c:0x001144,o:0.04},{r:120,c:0x002266,o:0.07},{r:65,c:0x0044aa,o:0.09}]}
];
nebulaConfigs.forEach(n=>{
  n.layers.forEach(l=>{
    const nb=new THREE.Mesh(
      new THREE.SphereGeometry(l.r,16,16),
      new THREE.MeshBasicMaterial({color:l.c,transparent:true,opacity:l.o,side:THREE.FrontSide,depthWrite:false})
    );
    nb.position.set(n.x,n.y,n.z);
    scene.add(nb);
  });
});

scene.add(new THREE.AmbientLight(0x110000,3));
const pl = new THREE.PointLight(0xff6633,2,600);
pl.position.set(0,80,120);
scene.add(pl);

// Lines between nodes removed

function pesoColor(p){
  if(p<=5)  return {c:0xac0000,e:0x660000};
  if(p<=10) return {c:0xff0000,e:0x990000};
  if(p<=18) return {c:0xffc000,e:0x996000};
  if(p<=25) return {c:0xffff00,e:0x999900};
  if(p<=35) return {c:0xcaeefb,e:0x6699aa};
  return            {c:0x00b0f0,e:0x006688};
}

const meshes=[];
const tip=document.getElementById('tooltip');

// Render hito #1 as center star first
const centerPoint = POINTS.find(p => p.isCenter);
if(centerPoint){
  // BLACK HOLE — Interstellar style
  // Core
  scene.add(new THREE.Mesh(new THREE.SphereGeometry(8,32,32),new THREE.MeshBasicMaterial({color:0x000000})));
  // Orange border
  scene.add(new THREE.Mesh(new THREE.SphereGeometry(8.7,32,32),new THREE.MeshBasicMaterial({color:0xff6600,transparent:true,opacity:0.9,side:THREE.BackSide})));
  // Accretion disk
  const diskParams=[[11,1.4,0.95,0xff6600],[15,1.2,0.7,0xff8800],[19,1.0,0.45,0xffaa00],[23,0.7,0.25,0xffcc44],[28,0.5,0.1,0xffffff]];
  const diskMeshes=[];
  diskParams.forEach(function(d){
    const dm=new THREE.Mesh(new THREE.TorusGeometry(d[0],d[1],12,120),new THREE.MeshBasicMaterial({color:d[3],transparent:true,opacity:d[2]}));
    dm.rotation.x=Math.PI/2+0.15;dm.rotation.z=0.08;scene.add(dm);diskMeshes.push(dm);
  });
  // Photon ring
  const photon=new THREE.Mesh(new THREE.TorusGeometry(9,0.4,8,80),new THREE.MeshBasicMaterial({color:0xffffff,transparent:true,opacity:0.6}));
  photon.rotation.x=Math.PI/2+0.15;scene.add(photon);
  // Glow layers — smaller to not cover nearby nodes
  [[10,0.10],[14,0.05],[18,0.02]].forEach(function(g){
    scene.add(new THREE.Mesh(new THREE.SphereGeometry(g[0],16,16),new THREE.MeshBasicMaterial({color:0xff7700,transparent:true,opacity:g[1],side:THREE.BackSide})));
  });
  // Animate disk
  function animateDisk(){diskMeshes.forEach(function(d){d.rotation.z+=0.002;});}
  // Label — just first name, small, above
  const cvC=document.createElement('canvas');cvC.width=256;cvC.height=56;
  const ctxC=cvC.getContext('2d');ctxC.fillStyle='rgba(0,0,0,0)';ctxC.fillRect(0,0,256,56);
  ctxC.fillStyle='#ff8800';ctxC.font='bold 28px Courier New';
  var centerLabel=centerPoint.label.split(' — ')[0].split(' ')[0];
  ctxC.fillText(centerLabel.toUpperCase(),4,40);
  const spC=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(cvC),transparent:true,opacity:0.9}));
  spC.scale.set(30,7,1);spC.position.set(0,16,0);scene.add(spC);
  // Invisible click sphere
  const bhC=new THREE.Mesh(new THREE.SphereGeometry(12,16,16),new THREE.MeshBasicMaterial({transparent:true,opacity:0}));
  bhC.userData={label:centerPoint.label,peso:50,isCenter:true};scene.add(bhC);meshes.push(bhC);
  // Store ref for animation
  window._bhDiskMeshes=diskMeshes;
}
POINTS.filter(p=>!p.isCenter).forEach((p,i)=>{
  const size=2.0+Math.pow(p.peso/50,0.5)*10;
  const col=pesoColor(p.peso);
  const geo=new THREE.SphereGeometry(size,20,20);
  const mat=new THREE.MeshPhongMaterial({color:col.c,emissive:col.e,emissiveIntensity:0.8,shininess:200,transparent:true,opacity:0.96});
  const mesh=new THREE.Mesh(geo,mat);
  mesh.position.set(p.x,p.y,p.z);
  mesh.userData={label:p.label,peso:p.peso};
  scene.add(mesh); meshes.push(mesh);
  // Multi-layer glow for all stars
  [{mult:2.0,op:0.08},{mult:3.2,op:0.04},{mult:5.0,op:0.02}].forEach(function(g){
    const gg=new THREE.SphereGeometry(size*g.mult,10,10);
    const gm=new THREE.MeshBasicMaterial({color:col.c,transparent:true,opacity:g.op,side:THREE.BackSide,depthWrite:false});
    const glow=new THREE.Mesh(gg,gm); glow.position.copy(mesh.position); scene.add(glow);
  });
});

const raycaster=new THREE.Raycaster();
const mouse=new THREE.Vector2();
let hovered=null;
window.addEventListener('mousemove',e=>{
  mouse.x=(e.clientX/innerWidth)*2-1;
  mouse.y=-(e.clientY/innerHeight)*2+1;
  raycaster.setFromCamera(mouse,camera);
  const hits=raycaster.intersectObjects(meshes);
  if(hits.length>0){
    const obj=hits[0].object;
    if(hovered!==obj){if(hovered)hovered.material.emissiveIntensity=0.7;hovered=obj;obj.material.emissiveIntensity=1.8;}
    const pc=obj.userData.peso<=5?'#ac0000':obj.userData.peso<=10?'#ff0000':obj.userData.peso<=18?'#ffc000':obj.userData.peso<=25?'#ffff00':obj.userData.peso<=35?'#caeefb':'#00b0f0';
    tip.style.display='block'; tip.style.left=(e.clientX+15)+'px'; tip.style.top=(e.clientY-10)+'px';
    tip.innerHTML='<b style="color:'+pc+'">⭐ '+obj.userData.label.toUpperCase()+'</b><br><span style="color:#555;font-size:10px">peso: '+obj.userData.peso.toFixed(1)+'</span>';
  } else { if(hovered){hovered.material.emissiveIntensity=0.7;hovered=null;} tip.style.display='none'; }
});
let isDrag=false,prevX=0,prevY=0,rotX=0,rotY=0,autoRot=true;
renderer.domElement.addEventListener('mousedown',e=>{isDrag=true;autoRot=false;prevX=e.clientX;prevY=e.clientY;});
renderer.domElement.addEventListener('mouseup',()=>isDrag=false);
renderer.domElement.addEventListener('mousemove',e=>{if(!isDrag)return;rotY+=(e.clientX-prevX)*0.005;rotX+=(e.clientY-prevY)*0.005;prevX=e.clientX;prevY=e.clientY;});
renderer.domElement.addEventListener('wheel',e=>{camera.position.z=Math.max(80,Math.min(700,camera.position.z+e.deltaY*0.4));});
window.addEventListener('resize',()=>{camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight);});
// Label sprites
const labelSprites=[];

let t=0;
function animate(){
  requestAnimationFrame(animate); t+=0.004;
  if(autoRot) rotY+=0.0008;
  scene.rotation.y=rotY; scene.rotation.x=rotX;
  meshes.forEach((m,i)=>{
    if(!m.userData.isCenter) m.scale.setScalar(1+Math.sin(t*1.2+i*0.8)*0.05);
  });
  pl.intensity=1.8+Math.sin(t*0.5)*0.4;
  if(window._bhDiskMeshes) window._bhDiskMeshes.forEach(d=>{d.rotation.z+=0.002;});
  renderer.render(scene,camera);
}
animate();
</script>
</body>
</html>"""
    return html




@app.route('/api/memoria-extendida', methods=['GET'])
@login_required
def api_get_memoria_extendida():
    usuario_id = session['usuario_id']
    mv_id = request.args.get('memoria_validada_id')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if mv_id:
        c.execute("""SELECT me.id, me.titulo, me.contenido, me.tipo, me.memoria_validada_id,
                            h.titulo as mv_titulo,
                            datetime(me.ts, 'unixepoch', 'localtime') as ts
                     FROM memoria_extendida me
                     LEFT JOIN hitos_usuario h ON h.id = me.memoria_validada_id
                     WHERE me.usuario_id=? AND me.activo=1 AND me.memoria_validada_id=?
                     ORDER BY me.ts DESC""", (usuario_id, mv_id))
    else:
        c.execute("""SELECT me.id, me.titulo, me.contenido, me.tipo, me.memoria_validada_id,
                            h.titulo as mv_titulo,
                            datetime(me.ts, 'unixepoch', 'localtime') as ts
                     FROM memoria_extendida me
                     LEFT JOIN hitos_usuario h ON h.id = me.memoria_validada_id
                     WHERE me.usuario_id=? AND me.activo=1
                     ORDER BY me.ts DESC""", (usuario_id,))
    rows = c.fetchall()
    conn.close()
    memorias = [{"id":r[0],"titulo":r[1],"contenido":r[2],"tipo":r[3],
                 "memoria_validada_id":r[4],"memoria_validada_titulo":r[5],"ts":r[6]} for r in rows]
    return jsonify({"memorias": memorias})

@app.route('/api/memoria-extendida', methods=['POST'])
@login_required
def api_crear_memoria_extendida():
    usuario_id = session['usuario_id']
    data = request.json or {}
    contenido = data.get('contenido','').strip()
    titulo = data.get('titulo','').strip()
    if not contenido:
        return jsonify({'ok': False})
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""INSERT INTO memoria_extendida
                    (usuario_id, memoria_validada_id, tipo, titulo, contenido, ts, activo)
                    VALUES (?,?,?,?,?,?,1)""",
                 (usuario_id, data.get('memoria_validada_id'), data.get('tipo','usuario'), titulo, contenido, time.time()))
    conn.commit()
    mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    # Generar embedding en background
    texto_emb = f"{titulo}\n{contenido}"
    threading.Thread(target=db_guardar_embedding, args=('memoria_extendida', mid, texto_emb), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/memoria-extendida/<int:mid>', methods=['PUT'])
@login_required
def api_editar_memoria_extendida(mid):
    usuario_id = session['usuario_id']
    data = request.json or {}
    contenido = data.get('contenido','').strip()
    titulo = data.get('titulo','').strip()
    conn = sqlite3.connect(DB_PATH)
    if titulo:
        conn.execute("UPDATE memoria_extendida SET contenido=?, titulo=? WHERE id=? AND usuario_id=?",
                     (contenido, titulo, mid, usuario_id))
    else:
        conn.execute("UPDATE memoria_extendida SET contenido=? WHERE id=? AND usuario_id=?",
                     (contenido, mid, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/memoria-extendida/<int:mid>', methods=['DELETE'])
@login_required
def api_borrar_memoria_extendida(mid):
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE memoria_extendida SET activo=0 WHERE id=? AND usuario_id=?", (mid, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})



@app.route('/api/observaciones/<int:oid>', methods=['DELETE'])
@login_required
def api_borrar_observacion(oid):
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE observaciones SET activa=0 WHERE id=? AND usuario_id=?", (oid, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/temas-abiertos/<int:tid>', methods=['DELETE'])
@login_required
def api_borrar_tema(tid):
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE temas_abiertos SET estado='cerrado' WHERE id=? AND usuario_id=?", (tid, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/temas-abiertos', methods=['GET'])
@login_required
def api_temas_abiertos():
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""SELECT id, tema, veces_mencionado, estado,
                        datetime(ts, 'unixepoch', 'localtime') as ts
                     FROM temas_abiertos WHERE usuario_id=? AND estado='abierto'
                     ORDER BY veces_mencionado DESC LIMIT 20""", (usuario_id,))
        rows = c.fetchall()
        temas = [{"id":r[0],"tema":r[1],"veces":r[2],"estado":r[3],"ts":r[4]} for r in rows]
    except:
        temas = []
    conn.close()
    return jsonify({"temas": temas})

@app.route('/api/personas', methods=['GET'])
@login_required
def api_personas():
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""SELECT id, nombre, COALESCE(apellidos,''), relacion, tono_predominante, notas, veces_mencionada
                     FROM personas WHERE usuario_id=? ORDER BY veces_mencionada DESC""", (usuario_id,))
    except Exception:
        c.execute("""SELECT id, nombre, '' , relacion, tono_predominante, notas, veces_mencionada
                     FROM personas WHERE usuario_id=? ORDER BY veces_mencionada DESC""", (usuario_id,))
    rows = c.fetchall()
    conn.close()
    personas = [{"id": r[0], "nombre": r[1], "apellidos": r[2] or '', "relacion": r[3], "tono": r[4], "notas": r[5], "veces_mencionada": r[6]} for r in rows]
    return jsonify({"personas": personas})

@app.route('/api/observaciones', methods=['GET'])
@login_required
def api_observaciones():
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""SELECT id, tipo, contenido, evidencia, ts
                     FROM observaciones WHERE usuario_id=? AND activa=1
                     ORDER BY ts DESC LIMIT 30""", (usuario_id,))
    except Exception:
        c.execute("SELECT id, tipo, contenido, evidencia, ts FROM observaciones WHERE usuario_id=? AND activa=1 ORDER BY ts DESC LIMIT 30", (usuario_id,))
    rows = c.fetchall()
    conn.close()
    obs = [{"id": r[0], "tipo": r[1], "contenido": r[2], "evidencia": r[3], "ts": ts_format(r[4])} for r in rows]
    return jsonify({"observaciones": obs})



@app.route('/api/hitos/rechazar', methods=['POST'])
@login_required
def api_rechazar_hito_propuesto():
    """Guarda el titulo del hito rechazado para no volver a proponerlo."""
    usuario_id = session['usuario_id']
    data = request.json or {}
    titulo = data.get('titulo', '').strip().lower()
    nombre = data.get('persona_nombre', '').strip()
    if not titulo:
        return jsonify({'ok': False})
    import hashlib
    titulo_hash = hashlib.md5(titulo.encode()).hexdigest()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT OR IGNORE INTO hitos_rechazados (usuario_id, titulo_hash, ts) VALUES (?,?,?)",
                     (usuario_id, titulo_hash, time.time()))
        conn.commit()
    except:
        pass
    # Si tiene persona_nombre, eliminarla de personas también
    if nombre:
        conn.execute("DELETE FROM personas WHERE usuario_id=? AND LOWER(nombre)=LOWER(?)",
                     (usuario_id, nombre))
        conn.commit()
        print(f"[ANNI] Persona rechazada: {nombre}")
    conn.close()
    print(f"[ANNI] Hito rechazado guardado: {titulo[:50]}")
    return jsonify({'ok': True})


@app.route('/api/personas/<int:pid>', methods=['PUT'])
@login_required
def api_editar_persona(pid):
    usuario_id = session['usuario_id']
    data = request.json or {}
    nombre = data.get('nombre', '').strip()
    apellidos = data.get('apellidos', '').strip()
    relacion = data.get('relacion', '').strip()
    if not nombre:
        return jsonify({'ok': False})
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""UPDATE personas SET nombre=?, apellidos=?, relacion=?
                    WHERE id=? AND usuario_id=?""",
                 (nombre, apellidos, relacion, pid, usuario_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/personas/rechazar', methods=['POST'])
@login_required
def api_rechazar_persona():
    """Elimina una persona de la tabla personas para que no vuelva a proponerse."""
    usuario_id = session['usuario_id']
    data = request.json or {}
    nombre = data.get('nombre', '').strip()
    if not nombre:
        return jsonify({'ok': False})
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM personas WHERE usuario_id=? AND LOWER(nombre)=LOWER(?)", (usuario_id, nombre))
    conn.commit()
    conn.close()
    print(f"[ANNI] Persona rechazada y eliminada: {nombre}")
    return jsonify({'ok': True})

@app.route('/api/personas-sin-hito', methods=['GET'])
@login_required
def api_personas_sin_hito():
    """Devuelve un hito propuesto para la primera persona que no tiene hito todavía."""
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Personas que no están mencionadas en ningún hito
    # Solo personas con relaciones personales reales — no referencias académicas
    relaciones_personales = ('pareja','esposa','esposo','hijo','hija','hijastra','hijastro',
                              'suegro','suegra','cuñado','cuñada','amigo','amiga','socio','socia',
                              'padre','madre','hermano','hermana','colega','jefe','cliente')
    placeholders = ','.join(['?' for _ in relaciones_personales])
    c.execute(f"""SELECT p.nombre, p.relacion, p.tono_predominante, p.notas
                 FROM personas p
                 WHERE p.usuario_id=?
                 AND p.nombre NOT IN ('Rafa', 'ANNI')
                 AND LOWER(p.relacion) IN ({placeholders})
                 AND p.veces_mencionada >= 1
                 AND NOT EXISTS (
                     SELECT 1 FROM hitos_usuario h
                     WHERE h.usuario_id=p.usuario_id
                     AND h.activo=1
                     AND (LOWER(h.titulo) LIKE '%' || LOWER(p.nombre) || '%'
                          OR LOWER(h.contenido) LIKE '%' || LOWER(p.nombre) || '%')
                 )
                 ORDER BY p.veces_mencionada DESC LIMIT 1""",
              (usuario_id,) + relaciones_personales)
    persona = c.fetchone()
    conn.close()

    if not persona:
        return jsonify({'hito': None})

    nombre, relacion, tono, notas = persona
    # Verificar si ya fue rechazado
    import hashlib
    titulo_check = f'{nombre.upper()} — {(relacion or "persona cercana").upper()}'.lower()
    th_check = hashlib.md5(titulo_check.encode()).hexdigest()
    conn2 = sqlite3.connect(DB_PATH)
    rechazado = conn2.execute(
        "SELECT id FROM hitos_rechazados WHERE usuario_id=? AND titulo_hash=?",
        (usuario_id, th_check)).fetchone()
    conn2.close()
    if rechazado:
        return jsonify({'hito': None})
    hito = {
        'hito': True,
        'titulo': f'{nombre.upper()} — {(relacion or "persona cercana").upper()}',
        'categoria': 'relacion',
        'contenido': f'{nombre} es {relacion or "persona importante"} en la vida del usuario. Tono: {tono or "neutro"}. {notas or ""}',
        'evidencia': f'ANNI detectó a {nombre} en conversaciones previas como {relacion}',
        'cuando_activarlo': f'Cuando el usuario mencione a {nombre} o temas relacionados con {relacion}',
        'como_usarlo': f'Tener en cuenta la relación con {nombre} al responder sobre vida personal o decisiones',
        'persona_nombre': nombre
    }
    return jsonify({'hito': hito})


@app.route('/api/embeddings/repair', methods=['POST'])
@login_required
def api_embeddings_repair():
    """Regenera embeddings para hitos sin embedding O con embedding desactualizado.
    Fuerza recalc del universo al terminar."""
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Hitos sin embedding
    c.execute("""SELECT h.id, h.titulo, h.contenido FROM hitos_usuario h
                 WHERE h.usuario_id=? AND h.activo=1
                 AND h.id NOT IN (
                     SELECT registro_id FROM embeddings WHERE tabla_origen='hitos_usuario'
                 )""", (usuario_id,))
    sin_emb = c.fetchall()
    # Hitos con embedding desactualizado (hito editado despues del embedding)
    c.execute("""SELECT h.id, h.titulo, h.contenido FROM hitos_usuario h
                 JOIN embeddings e ON e.tabla_origen='hitos_usuario' AND e.registro_id=h.id
                 WHERE h.usuario_id=? AND h.activo=1 AND h.ts > e.ts""", (usuario_id,))
    desactualizados = c.fetchall()
    conn.close()

    hitos = sin_emb + desactualizados
    vistos = set()
    hitos_uniq = []
    for row in hitos:
        if row[0] not in vistos:
            vistos.add(row[0])
            hitos_uniq.append(row)

    def regenerar_y_recalcular():
        import time as time_mod
        for hid, titulo, contenido in hitos_uniq:
            try:
                conn_del = sqlite3.connect(DB_PATH)
                conn_del.execute("DELETE FROM embeddings WHERE tabla_origen='hitos_usuario' AND registro_id=?", (hid,))
                conn_del.commit()
                conn_del.close()
            except: pass
            texto = f"{titulo}. {contenido}" if titulo else contenido
            db_guardar_embedding('hitos_usuario', hid, texto)
            time_mod.sleep(0.3)
        try:
            conn_u = sqlite3.connect(DB_PATH)
            conn_u.execute("DELETE FROM universo_cache WHERE usuario_id=?", (usuario_id,))
            conn_u.commit()
            conn_u.close()
        except: pass
        recalcular_universo(usuario_id)
        print(f"[ANNI] Repair completo — {len(hitos_uniq)} embeddings regenerados, universo recalculado")

    if hitos_uniq:
        threading.Thread(target=regenerar_y_recalcular, daemon=True).start()

    return jsonify({'ok': True, 'reparando': len(hitos_uniq),
                    'sin_embedding': len(sin_emb),
                    'desactualizados': len(desactualizados),
                    'msg': f'Regenerando {len(hitos_uniq)} embeddings y recalculando universo...'})

# ── UNIVERSO ──────────────────────────────────────────────────────────────────

@app.route('/api/universo', methods=['GET'])
@login_required
def api_universo():
    """Devuelve hitos y constelaciones para la visualización 3D — con cache."""
    import struct, json as json_mod
    garantizar_tablas_universo()
    usuario_id = session['usuario_id']
    recalc = request.args.get('recalc', '0') == '1'

    # Count current hitos with embeddings
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT COUNT(*) FROM hitos_usuario h
                 JOIN embeddings e ON e.tabla_origen='hitos_usuario' AND e.registro_id=h.id
                 WHERE h.usuario_id=? AND h.activo=1""", (usuario_id,))
    n_hitos = c.fetchone()[0]

    # Check cache
    c.execute("SELECT puntos_json, estrellas_json, n_hitos FROM universo_cache WHERE usuario_id=?", (usuario_id,))
    cache = c.fetchone()
    # constelaciones removed
    n_cons = c.fetchone()[0]
    conn.close()

    if n_hitos < 3:
        return jsonify({'ok': False, 'error': 'insuficientes_embeddings', 'count': n_hitos})

    # Return cache if valid and not forced recalc
    cache_valid = cache and cache[2] == n_hitos and n_cons > 0 and not recalc
    if cache_valid:
        return jsonify({'ok': True, 'points': json_mod.loads(cache[0]),
                        'stars': json_mod.loads(cache[1]), 'cached': True})

    # Need recalc — launch in background, return loading state if first time
    if not cache:
        threading.Thread(target=recalcular_universo, args=(usuario_id,), daemon=True).start()
        return jsonify({'ok': False, 'error': 'calculando', 'msg': 'Calculando universo por primera vez...'})

    # Has old cache — return it while recalculating in background
    threading.Thread(target=recalcular_universo, args=(usuario_id,), daemon=True).start()
    return jsonify({'ok': True, 'points': json_mod.loads(cache[0]),
                    'stars': json_mod.loads(cache[1]), 'cached': True, 'recalculating': True})

    return  # old code removed — now handled by recalcular_universo


def garantizar_tablas_universo():
    """Crea tablas de universo si no existen — llamar antes de cualquier operación de universo."""
    sqls = [
        """CREATE TABLE IF NOT EXISTS universo_cache (
            id INTEGER PRIMARY KEY,
            usuario_id INTEGER NOT NULL UNIQUE,
            puntos_json TEXT NOT NULL,
            estrellas_json TEXT NOT NULL,
            n_hitos INTEGER DEFAULT 0,
            ts REAL DEFAULT (unixepoch('now','subsec'))
        )""",
        """CREATE TABLE IF NOT EXISTS constelaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            nombre TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            hitos_ids TEXT DEFAULT '[]',
            ts_calculado REAL DEFAULT (unixepoch('now','subsec'))
        )"""
    ]
    for sql in sqls:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(sql)
            conn.commit()
            conn.close()
        except:
            pass

def pca_python(vecs, n_components=3):
    """PCA en Python puro — sin numpy ni sklearn."""
    import random as rnd
    rnd.seed(42)
    n, d = len(vecs), len(vecs[0])
    means = [sum(vecs[i][j] for i in range(n)) / n for j in range(d)]
    centered = [[vecs[i][j] - means[j] for j in range(d)] for i in range(n)]

    def dot(a, b): return sum(x*y for x,y in zip(a,b))
    def normalize(v):
        norm = sum(x*x for x in v) ** 0.5
        return [x/norm for x in v] if norm > 1e-10 else v
    def subtract_proj(v, u):
        p = dot(v, u)
        return [v[i] - p*u[i] for i in range(len(v))]

    components = []
    for _ in range(n_components):
        pc = normalize([rnd.gauss(0, 1) for _ in range(d)])
        for prev in components:
            pc = subtract_proj(pc, prev)
        pc = normalize(pc)
        for _ in range(15):
            scores = [dot(row, pc) for row in centered]
            pc_new = [sum(scores[i]*centered[i][j] for i in range(n)) for j in range(d)]
            for prev in components:
                pc_new = subtract_proj(pc_new, prev)
            pc = normalize(pc_new)
        components.append(pc)

    return [[dot(centered[i], components[k]) for k in range(n_components)] for i in range(n)]

def recalcular_universo(usuario_id):
    """Calcula PCA + constelaciones y guarda en cache. Sin numpy ni umap."""
    import struct, json as json_mod
    garantizar_tablas_universo()
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT h.id, COALESCE(h.titulo, SUBSTR(h.contenido,1,60)) as label,
                            h.peso, h.tipo, e.embedding
                     FROM hitos_usuario h
                     JOIN embeddings e ON e.tabla_origen='hitos_usuario' AND e.registro_id=h.id
                     WHERE h.usuario_id=? AND h.activo=1
                     ORDER BY h.peso DESC""", (usuario_id,))
        rows = c.fetchall()
        c.execute("SELECT COUNT(*) FROM constelaciones WHERE usuario_id=?", (usuario_id,))
        n_cons = c.fetchone()[0]
        conn.close()

        if len(rows) < 3:
            return

        # calcular_constelaciones removed

        conn2 = sqlite3.connect(DB_PATH)
        c2 = conn2.cursor()
        constelaciones = []  # constelaciones system removed
        hito_to_con = {}
        con_colors = ['#cc0000','#ff8800','#ffcc00','#00aaff','#aa44ff','#00cc88']

        # PCA puro Python — sin dependencias
        vecs = []
        for hid, label, peso, tipo, blob in rows:
            nv = len(blob) // 4
            vecs.append(list(struct.unpack(f'{nv}f', blob)))

        n = len(vecs)
        coords = pca_python(vecs, n_components=3)

        # Normalize to -150..150
        for axis in range(3):
            vals = [coords[i][axis] for i in range(n)]
            mn, mx = min(vals), max(vals)
            if mx > mn:
                for i in range(n):
                    coords[i][axis] = (coords[i][axis] - mn) / (mx - mn) * 300 - 150

        # Force minimum separation between nodes (repulsion passes)
        MIN_DIST = 28.0
        for _ in range(30):
            for i in range(n):
                for j in range(i+1, n):
                    dx = coords[i][0] - coords[j][0]
                    dy = coords[i][1] - coords[j][1]
                    dz = coords[i][2] - coords[j][2]
                    dist = (dx*dx + dy*dy + dz*dz) ** 0.5
                    if dist < MIN_DIST and dist > 0.001:
                        factor = (MIN_DIST - dist) / dist * 0.5
                        coords[i][0] += dx * factor
                        coords[i][1] += dy * factor
                        coords[i][2] += dz * factor
                        coords[j][0] -= dx * factor
                        coords[j][1] -= dy * factor
                        coords[j][2] -= dz * factor

        hid_to_idx = {rows[i][0]: i for i in range(n)}
        points = []
        for i, (hid, label, peso, tipo, blob) in enumerate(rows):
            con_color = '#cc0000'
            con_nombre = 'GENERAL'
            # Hito #1 siempre en el centro con peso máximo
            if hid == 1:
                px, py, pz = 0.0, 0.0, 0.0
                peso_final = 50.0
            else:
                px, py, pz = float(coords[i][0]), float(coords[i][1]), float(coords[i][2])
                peso_final = float(peso)
            points.append({
                'x': px, 'y': py, 'z': pz,
                'label': str(label)[:60], 'peso': peso_final, 'tipo': tipo,
                'id': hid, 'constelacion': con_nombre, 'color': con_color,
                'isCenter': hid == 1
            })

        stars = []
        for con in constelaciones:
            hids = con['hitos_ids']
            idxs = [hid_to_idx[h] for h in hids if h in hid_to_idx]
            if not idxs:
                continue
            cx = sum(coords[i][0] for i in idxs) / len(idxs)
            cy = sum(coords[i][1] for i in idxs) / len(idxs)
            cz = sum(coords[i][2] for i in idxs) / len(idxs)
            stars.append({'x': cx, 'y': cy, 'z': cz, 'nombre': con['nombre'],
                          'descripcion': con['descripcion'], 'color': con['color'],
                          'n_planetas': len(idxs)})

        # Save to cache
        conn3 = sqlite3.connect(DB_PATH)
        conn3.execute("""INSERT OR REPLACE INTO universo_cache (usuario_id, puntos_json, estrellas_json, n_hitos, ts)
                         VALUES (?,?,?,?,?)""",
                      (usuario_id, json_mod.dumps(points), json_mod.dumps(stars), len(rows), time.time()))
        conn3.commit()
        conn3.close()
        print(f"[ANNI] Universo calculado y cacheado para usuario {usuario_id} — {len(points)} puntos, {len(stars)} estrellas")

    except Exception as e:
        print(f"[ANNI] Error calculando universo: {e}")
        import traceback; print(traceback.format_exc())

    # Trigger recalc when new hito is saved
    # (hook into api_guardar_resumen is already there for conversations)

# Remove old duplicated code block (was inside the old api_universo)




@app.route('/api/curiosa/tick', methods=['POST'])
@login_required
def api_curiosa_tick():
    """Fuerza un tick inmediato del ciclo CURIOSA activo."""
    usuario_id = session['usuario_id']
    # Reset ts_ultimo_pulso to force advancement
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""UPDATE ciclos_curiosa SET ts_ultimo_pulso = 0
                    WHERE usuario_id=? AND estado='en_curso'""", (usuario_id,))
    conn.commit()
    conn.close()
    threading.Thread(target=tick_curiosa, args=(usuario_id,), daemon=True).start()
    return jsonify({'ok': True, 'msg': 'Tick forzado — el pulso avanzará en segundos'})

# ── EL MUNDO DE ANNI ─────────────────────────────────────────────────────────

@app.route('/api/mundo', methods=['GET'])
@login_required
def api_mundo():
    usuario_id = session['usuario_id']
    page = int(request.args.get('page', 1))
    per_page = 10
    offset = (page - 1) * per_page
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM ciclos_curiosa WHERE usuario_id=? AND estado='completado'", (usuario_id,))
    total = c.fetchone()[0]
    c.execute("""SELECT id, dominio, subtema, conclusion, pregunta_abierta, fuentes_usadas, ts_fin
                 FROM ciclos_curiosa WHERE usuario_id=? AND estado='completado'
                 ORDER BY ts_fin DESC LIMIT ? OFFSET ?""",
              (usuario_id, per_page, offset))
    ciclos = []
    for r in c.fetchall():
        ciclos.append({
            'id': r[0], 'dominio': r[1], 'subtema': r[2],
            'conclusion': r[3], 'pregunta_abierta': r[4],
            'fuentes': r[5], 'ts': ts_format(r[6])
        })
    conn.close()
    pages = (total + per_page - 1) // per_page if total else 1
    return jsonify({'ciclos': ciclos, 'total': total, 'page': page, 'pages': pages})

@app.route('/api/mundo/estado', methods=['GET'])
@login_required
def api_mundo_estado():
    """Estado del ciclo en curso."""
    usuario_id = session['usuario_id']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT dominio, subtema, pulso_actual, ts_ultimo_pulso
                 FROM ciclos_curiosa WHERE usuario_id=? AND estado='en_curso'
                 ORDER BY ts_inicio DESC LIMIT 1""", (usuario_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'activo': False})
    mins_restantes = max(0, int((CURIOSA_PULSO_INTERVAL - (time.time() - (row[3] or time.time()))) / 60))
    return jsonify({
        'activo': True,
        'dominio': row[0],
        'subtema': row[1],
        'pulso': row[2],
        'mins_siguiente': mins_restantes
    })

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


def analizar_tarea_completada(usuario_id, tarea_id):
    """Analiza una tarea completada y actualiza el modelo de ejecución del usuario."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT titulo, descripcion, cliente, due_date, estado,
                            ts_creacion, ts_completada, veces_mencionada
                     FROM tareas WHERE id=? AND usuario_id=?""", (tarea_id, usuario_id))
        t = c.fetchone()
        if not t:
            conn.close()
            return

        titulo, desc, cliente, due_date, estado, ts_creacion, ts_completada, veces = t

        # Calcular días que tardó
        dias_tardados = int((ts_completada - ts_creacion) / 86400) if ts_completada else 0

        # Obtener historial de tareas completadas para comparar
        c.execute("""SELECT titulo, cliente, ts_creacion, ts_completada,
                            CAST((ts_completada - ts_creacion) / 86400 AS INTEGER) as dias
                     FROM tareas WHERE usuario_id=? AND estado='completada'
                     AND id != ? ORDER BY ts_completada DESC LIMIT 20""",
                  (usuario_id, tarea_id))
        historial = c.fetchall()
        conn.close()

        if not historial:
            return  # Sin historial suficiente para comparar

        # Calcular promedio días por tipo
        dias_lista = [h[4] for h in historial if h[4] and h[4] > 0]
        promedio = sum(dias_lista) / len(dias_lista) if dias_lista else 0

        historial_txt = chr(10).join([f"- {h[0]}{' ['+h[1]+']' if h[1] else ''}: {h[4]} dias" for h in historial[:10]])

        prompt = f"""Eres ANNI analizando el patrón de ejecución de Rafa.

Tarea recién completada:
- Título: {titulo}
- Cliente: {cliente or 'ninguno'}
- Días tardados: {dias_tardados}
- Veces mencionada en conversación: {veces}
- Due date: {due_date or 'sin fecha'}

Historial reciente de tareas completadas:
{historial_txt}

Promedio de días en completar tareas: {promedio:.1f}

Analiza si hay algo notable en esta tarea:
- ¿Tardó mucho más o mucho menos de lo habitual?
- ¿Hay un patrón en qué tipo de tareas completa rápido vs cuáles se demoran?
- ¿La mencionó muchas veces antes de cerrarla (posible evitación)?

Responde SOLO con JSON:
{{
  "notable": true/false,
  "observacion": "descripción concreta del patrón detectado, o null si no hay nada notable",
  "tipo_patron": "velocidad|evitacion|cliente|general|null"
}}

Solo es notable si hay algo genuinamente significativo. No fuerces patrones donde no los hay."""

        resp = together.chat.completions.create(
            model=CHAT_MODEL_FALLBACK,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
        data = json.loads(raw)

        if data.get("notable") and data.get("observacion"):
            # Guardar como observación
            conn2 = sqlite3.connect(DB_PATH)
            c2 = conn2.cursor()
            c2.execute("""INSERT INTO observaciones (usuario_id, tipo, contenido, evidencia, peso, ts, ts_ultima_vez)
                         VALUES (?,?,?,?,5,?,?)""",
                      (usuario_id, data.get("tipo_patron", "patron"),
                       data["observacion"],
                       f"Tarea '{titulo}' completada en {dias_tardados} días",
                       time.time(), time.time()))
            conn2.commit()
            conn2.close()
            print(f"[ANNI] Patrón de tarea detectado: {data['observacion'][:60]}")

    except Exception as e:
        print(f"[ANNI] Error analizando tarea completada: {e}")

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
    es_completada = data.get('estado') == 'completada'
    if es_completada:
        campos.append("ts_completada=?")
        valores.append(time.time())
    valores += [tarea_id, usuario_id]
    c.execute(f"UPDATE tareas SET {', '.join(campos)} WHERE id=? AND usuario_id=?", valores)
    conn.commit()
    conn.close()
    if es_completada:
        threading.Thread(target=analizar_tarea_completada, args=(usuario_id, tarea_id), daemon=True).start()
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
  <button class='nav-btn' onclick='showPage("chats")'>CHATS</button>
  <button class='nav-btn' onclick='showPage("diario")'>DIARIO</button>
  <button class='nav-btn' onclick='showPage("memoria_anni")'>MEMORIA ANNI</button>
  <button class='nav-btn' onclick='showPage("mundo")'>MUNDO ANNI</button>
  <button class='nav-btn' onclick='showPage("universo")'>UNIVERSO ANNI</button>
  <button class='nav-btn' onclick='descargarBD()'>BD ANNI</button>
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
<h1 id='page-title'>Memoria ANNI</h1>
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
}else if(f.type==='application/pdf'||f.name.toLowerCase().endsWith('.pdf')){
reader.onload=function(e){archivoData={tipo:'pdf',data:e.target.result,nombre:f.name};};
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
.then(r=>r.json()).then(h=>{
  if(h.hito&&h.hito.hito){
    mostrarModalHito(h.hito);
  } else {
    // Check if there are personas without hitos to propose
    fetch('/api/personas-sin-hito').then(r=>r.json()).then(function(ph){
      if(ph.hito) mostrarModalHito(ph.hito);
    }).catch(()=>{});
  }
})
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

function rechazarHito(){
  document.getElementById('modal-hito').classList.remove('open');
  if(pendHito){
    fetch('/api/hitos/rechazar',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        titulo: pendHito.titulo||'',
        persona_nombre: pendHito.persona_nombre||''
      })
    }).catch(function(){});
  }
  pendHito=null;
}
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
var titles={universo:'Universo ANNI',mundo:'El mundo de ANNI',tareas:'Tareas',memoria_anni:'Memoria ANNI',chats:'Conversaciones',memoria:'Memoria viva',diario:'Diario'};
document.getElementById('page-title').textContent=titles[sec]||sec;
document.getElementById('page').classList.add('open');
loadPage(sec,1);}
function closePage(){document.getElementById('page').classList.remove('open');}
function loadPage(sec,page){
document.getElementById('page-body').innerHTML='<p style="color:#999;padding:20px">Cargando...</p>';
if(sec==='universo'){window.location.href='/universo';return;}
else if(sec==='mundo')loadMundo(page);
else if(sec==='tareas')loadTareas(page);
else if(sec==='memoria_anni')loadMemoriaAnni();
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

function repararEmbeddings(){
  var btn=event.target;
  btn.textContent='⏳ Procesando...';btn.disabled=true;
  fetch('/api/embeddings/repair',{method:'POST'}).then(r=>r.json()).then(function(d){
    if(d.ok){
      if(d.reparando===0){
        alert('Todo está al día. Ningún embedding necesita regenerarse.');
      } else {
        alert('Regenerando '+d.reparando+' embeddings ('+d.sin_embedding+' nuevos, '+d.desactualizados+' desactualizados). El universo se recalculará automáticamente en unos segundos.');
      }
    }
    btn.textContent='↻ Reparar embeddings';btn.disabled=false;
  }).catch(function(){btn.textContent='↻ Reparar embeddings';btn.disabled=false;});
}


// ── MEMORIA VALIDADA — ACORDEONES ────────────────────────────────────────────

var MV_TIPOS = [
  {
    tipo: 'relacion',
    label: 'Personas',
    icon: '👤',
    desc: 'Familia, amigos, colegas y personas importantes en tu vida',
    campos: function(h){
      h=h||{};
      return mkGrid2(
        mkF('NOMBRE', mkI('mv-nombre_propio', h.nombre_propio||'', 'Nombre de pila', true)),
        mkF('APELLIDOS', mkI('mv-apellidos', h.apellidos||'', 'Apellidos', true))
      )+
      mkGrid3(
        '',
        mkF('MOTE / APODO', mkI('mv-mote', h.mote||'', 'Como le llamas tú')),
        ''
      )+
      mkGrid2(
        mkF('TIPO DE RELACIÓN', mkSel('mv-subtipo_relacion', h.subtipo_relacion||'', [
          ['','Seleccionar...'],['familia_propia','Familia propia'],['familia_directa','Familia directa'],
          ['familia_directa_extendida','Familia directa extendida'],['familia_politica_directa','Familia política directa'],
          ['familia_politica_extendida','Familia política extendida'],['expareja','Expareja'],
          ['amigos','Amigos y amigas'],['mentor','Mentor / Figura de influencia'],
          ['colegas_trabajo','Colegas de trabajo'],['relaciones_trabajo','Relaciones de trabajo'],
          ['conocido','Conocido'],['otros','Otros']
        ])),
        mkF('RELACIÓN ESPECÍFICA', mkI('mv-relacion_especifica', h.relacion_especifica||'', 'Madre, Padre, Mejor amigo...'))
      )+
      '<div style="display:flex;gap:20px;padding:8px;background:#f9f9f9;border-radius:4px;margin-bottom:8px">'+
      mkChk('mv-fallecido', h.fallecido, 'Fallecido/a')+
      '<span style="margin-left:16px">'+mkChk('mv-relacion_activa', h.relacion_activa!==0, 'Relación activa')+'</span>'+
      '</div>'+
      '<div style="font-size:10px;color:#aaa;letter-spacing:2px;margin:10px 0 6px">SOBRE LA PERSONA</div>'+
      mkGrid3(
        mkF('PROFESIÓN', mkI('mv-profesion', h.profesion||'', 'Qué hace')),
        mkF('DÓNDE VIVE', mkI('mv-donde_vive', h.donde_vive||'', 'Ciudad, País')),
        mkF('FECHA NACIMIENTO', mkI('mv-fecha_nacimiento', h.fecha_nacimiento||'', 'Año o fecha'))
      )+
      mkF('PERSONALIDAD', mkI('mv-personalidad', h.personalidad||'', 'Directo, optimista, reservado...'))+
      '<div style="font-size:10px;color:#aaa;letter-spacing:2px;margin:10px 0 6px">CONTEXTO DE LA RELACIÓN</div>'+
      mkGrid2(
        mkF('CÓMO SE CONOCIERON', mkI('mv-como_se_conocieron', h.como_se_conocieron||'', 'Colegio, trabajo, viaje...')),
        mkF('DESDE CUÁNDO', mkI('mv-desde_cuando', h.desde_cuando||'', 'Año o época'))
      )+
      mkGrid2(
        mkF('FRECUENCIA DE CONTACTO', mkSel('mv-frecuencia_contacto', h.frecuencia_contacto||'', [
          ['','Seleccionar...'],['diario','Diario'],['semanal','Semanal'],
          ['ocasional','Ocasional'],['raramente','Raramente'],['sin_contacto','Sin contacto']
        ])),
        mkF('ÚLTIMO CONTACTO', mkI('mv-ultimo_contacto', h.ultimo_contacto||'', 'Cuándo fue el último contacto'))
      )+
      '<div style="font-size:10px;color:#aaa;letter-spacing:2px;margin:10px 0 6px">PARA ANNI</div>'+
      mkF('CÓMO HABLA RAFA DE ESTA PERSONA', mkI('mv-como_habla_rafa', h.como_habla_rafa||'', 'Con nostalgia, con orgullo, con preocupación...'))+
      mkF('TEMAS RECURRENTES', mkI('mv-temas_recurrentes', h.temas_recurrentes||'', 'Fútbol y trabajo, familia, proyectos...'))+
      mkF('NOTAS ADICIONALES', mkTA('mv-contenido', h.contenido||'', 'Cualquier cosa relevante que ANNI deba saber'));
    },
    guardar: function(){
      var nom=(document.getElementById('mv-nombre_propio')||{}).value||'';
      var ape=(document.getElementById('mv-apellidos')||{}).value||'';
      var titulo=(nom+' '+ape).trim().toUpperCase();
      if(!titulo){alert('El nombre es obligatorio');return;}
      var payload={
        titulo:titulo, categoria:'relacion', tipo_nuevo:'relacion',
        contenido:(document.getElementById('mv-contenido')||{}).value||titulo,
        nombre_propio:nom, apellidos:ape,
        mote:(document.getElementById('mv-mote')||{}).value||'',
        subtipo_relacion:(document.getElementById('mv-subtipo_relacion')||{}).value||'',
        relacion_especifica:(document.getElementById('mv-relacion_especifica')||{}).value||'',
        fallecido:document.getElementById('mv-fallecido')&&document.getElementById('mv-fallecido').checked?1:0,
        relacion_activa:document.getElementById('mv-relacion_activa')&&document.getElementById('mv-relacion_activa').checked?1:0,
        profesion:(document.getElementById('mv-profesion')||{}).value||'',
        donde_vive:(document.getElementById('mv-donde_vive')||{}).value||'',
        fecha_nacimiento:(document.getElementById('mv-fecha_nacimiento')||{}).value||'',
        personalidad:(document.getElementById('mv-personalidad')||{}).value||'',
        como_se_conocieron:(document.getElementById('mv-como_se_conocieron')||{}).value||'',
        desde_cuando:(document.getElementById('mv-desde_cuando')||{}).value||'',
        frecuencia_contacto:(document.getElementById('mv-frecuencia_contacto')||{}).value||'',
        ultimo_contacto:(document.getElementById('mv-ultimo_contacto')||{}).value||'',
        como_habla_rafa:(document.getElementById('mv-como_habla_rafa')||{}).value||'',
        temas_recurrentes:(document.getElementById('mv-temas_recurrentes')||{}).value||''
      };
      guardarHitoTipado(payload);
    }
  },
  {
    tipo: 'organizacion',
    label: 'Organizaciones',
    icon: '🏢',
    desc: 'Empresas, instituciones y organizaciones relevantes',
    campos: function(h){
      h=h||{};
      return mkF('NOMBRE', mkI('mv-titulo', h.titulo||'', 'Nombre de la organización', true))+
      mkGrid2(
        mkF('SECTOR', mkI('mv-sector', h.profesion||'', 'Marketing, Tecnología, Salud...')),
        mkF('DÓNDE OPERA', mkI('mv-donde_vive', h.donde_vive||'', 'Ciudad, País, Global'))
      )+
      mkF('ROL DE RAFA', mkI('mv-relacion_especifica', h.relacion_especifica||'', 'Fundador, Cliente, Colaborador, Empleado...'))+
      mkF('PERSONAS CLAVE', mkI('mv-personalidad', h.personalidad||'', 'Quién trabaja ahí o es contacto clave'))+
      mkGrid2(
        mkF('DESDE CUÁNDO', mkI('mv-desde_cuando', h.desde_cuando||'', 'Año o época')),
        mkF('ESTADO', mkSel('mv-frecuencia_contacto', h.frecuencia_contacto||'', [
          ['','Seleccionar...'],['activa','Activa'],['pausada','Pausada'],['cerrada','Cerrada']
        ]))
      )+
      mkF('QUÉ REPRESENTA PARA RAFA', mkTA('mv-contenido', h.contenido||'', 'Por qué es importante esta organización'));
    },
    guardar: function(){
      var titulo=(document.getElementById('mv-titulo')||{}).value||'';
      if(!titulo){alert('El nombre es obligatorio');return;}
      var payload={
        titulo:titulo.toUpperCase(), categoria:'organizacion', tipo_nuevo:'organizacion',
        contenido:(document.getElementById('mv-contenido')||{}).value||titulo,
        profesion:(document.getElementById('mv-sector')||{}).value||'',
        donde_vive:(document.getElementById('mv-donde_vive')||{}).value||'',
        relacion_especifica:(document.getElementById('mv-relacion_especifica')||{}).value||'',
        personalidad:(document.getElementById('mv-personalidad')||{}).value||'',
        desde_cuando:(document.getElementById('mv-desde_cuando')||{}).value||'',
        frecuencia_contacto:(document.getElementById('mv-frecuencia_contacto')||{}).value||''
      };
      guardarHitoTipado(payload);
    }
  },
  {
    tipo: 'proyecto',
    label: 'Proyectos',
    icon: '🚀',
    desc: 'Proyectos en curso, pausados o completados',
    campos: function(h){
      h=h||{};
      return mkF('NOMBRE DEL PROYECTO', mkI('mv-titulo', h.titulo||'', 'Nombre del proyecto', true))+
      mkF('DESCRIPCIÓN BREVE', mkI('mv-descripcion', h.personalidad||'', 'En qué consiste en una frase'))+
      mkGrid2(
        mkF('ESTADO', mkSel('mv-estado', h.frecuencia_contacto||'', [
          ['','Seleccionar...'],['idea','Idea'],['en_curso','En curso'],['pausado','Pausado'],['completado','Completado'],['abandonado','Abandonado']
        ])),
        mkF('ORGANIZACIÓN ASOCIADA', mkI('mv-org', h.donde_vive||'', 'La Mákina, Umanity...'))
      )+
      mkGrid2(
        mkF('FECHA INICIO', mkI('mv-inicio', h.desde_cuando||'', 'Cuándo empezó')),
        mkF('FECHA FIN ESTIMADA', mkI('mv-fin', h.ultimo_contacto||'', 'Cuándo termina o terminó'))
      )+
      mkF('PERSONAS INVOLUCRADAS', mkI('mv-personas', h.como_se_conocieron||'', 'Quién trabaja en esto'))+
      mkF('POR QUÉ IMPORTA', mkI('mv-importa', h.como_habla_rafa||'', 'Qué problema resuelve o qué representa'))+
      mkF('PRÓXIMO PASO', mkTA('mv-contenido', h.contenido||'', 'Qué hay que hacer ahora mismo'));
    },
    guardar: function(){
      var titulo=(document.getElementById('mv-titulo')||{}).value||'';
      if(!titulo){alert('El nombre es obligatorio');return;}
      var payload={
        titulo:titulo.toUpperCase(), categoria:'proyecto', tipo_nuevo:'proyecto',
        contenido:(document.getElementById('mv-contenido')||{}).value||titulo,
        personalidad:(document.getElementById('mv-descripcion')||{}).value||'',
        frecuencia_contacto:(document.getElementById('mv-estado')||{}).value||'',
        donde_vive:(document.getElementById('mv-org')||{}).value||'',
        desde_cuando:(document.getElementById('mv-inicio')||{}).value||'',
        ultimo_contacto:(document.getElementById('mv-fin')||{}).value||'',
        como_se_conocieron:(document.getElementById('mv-personas')||{}).value||'',
        como_habla_rafa:(document.getElementById('mv-importa')||{}).value||''
      };
      guardarHitoTipado(payload);
    }
  },
  {
    tipo: 'lugar',
    label: 'Lugares',
    icon: '📍',
    desc: 'Ciudades, países y espacios con significado',
    campos: function(h){
      h=h||{};
      return mkF('NOMBRE DEL LUGAR', mkI('mv-titulo', h.titulo||'', 'Madrid, CDMX, Puerto Escondido...', true))+
      mkGrid2(
        mkF('TIPO', mkSel('mv-tipo_lugar', h.subtipo_relacion||'', [
          ['','Seleccionar...'],['ciudad','Ciudad'],['pais','País'],['barrio','Barrio'],['espacio','Espacio concreto'],['otro','Otro']
        ])),
        mkF('POR QUÉ ES RELEVANTE', mkI('mv-relevancia', h.relacion_especifica||'', 'Vivió allí, lo visita, tiene significado...'))
      )+
      mkF('MOMENTOS IMPORTANTES', mkI('mv-momentos', h.como_se_conocieron||'', 'Qué pasó ahí que importa'))+
      mkGrid2(
        mkF('HA VIVIDO AHÍ', mkSel('mv-vivio', h.frecuencia_contacto||'', [
          ['','Seleccionar...'],['si','Sí, vivió ahí'],['no','No, solo visitas'],['parcialmente','Temporadas']
        ])),
        mkF('FRECUENCIA DE VISITA', mkI('mv-freq_visita', h.desde_cuando||'', 'Cada cuánto va'))
      )+
      mkF('NOTAS', mkTA('mv-contenido', h.contenido||'', 'Cualquier cosa relevante sobre este lugar'));
    },
    guardar: function(){
      var titulo=(document.getElementById('mv-titulo')||{}).value||'';
      if(!titulo){alert('El nombre es obligatorio');return;}
      var payload={
        titulo:titulo.toUpperCase(), categoria:'lugar', tipo_nuevo:'lugar',
        contenido:(document.getElementById('mv-contenido')||{}).value||titulo,
        subtipo_relacion:(document.getElementById('mv-tipo_lugar')||{}).value||'',
        relacion_especifica:(document.getElementById('mv-relevancia')||{}).value||'',
        como_se_conocieron:(document.getElementById('mv-momentos')||{}).value||'',
        frecuencia_contacto:(document.getElementById('mv-vivio')||{}).value||'',
        desde_cuando:(document.getElementById('mv-freq_visita')||{}).value||''
      };
      guardarHitoTipado(payload);
    }
  },
  {
    tipo: 'evento',
    label: 'Eventos y momentos',
    icon: '⚡',
    desc: 'Momentos concretos que marcaron algo importante',
    campos: function(h){
      h=h||{};
      return mkF('TÍTULO DEL EVENTO', mkI('mv-titulo', h.titulo||'', 'Muerte de Erika, Viaje de Bosco a Suiza...', true))+
      mkGrid2(
        mkF('FECHA', mkI('mv-fecha', h.fecha_nacimiento||'', 'Cuándo ocurrió')),
        mkF('PERSONAS INVOLUCRADAS', mkI('mv-personas', h.como_se_conocieron||'', 'Quién estuvo ahí'))
      )+
      mkF('QUÉ PASÓ', mkTA('mv-contenido', h.contenido||'', 'Descripción de lo que ocurrió'))+
      mkF('POR QUÉ FUE IMPORTANTE', mkI('mv-importa', h.como_habla_rafa||'', 'Qué cambió o qué significó'))+
      mkF('CÓMO LO RECUERDA RAFA', mkI('mv-recuerda', h.personalidad||'', 'Con nostalgia, con orgullo, con dolor...'));
    },
    guardar: function(){
      var titulo=(document.getElementById('mv-titulo')||{}).value||'';
      if(!titulo){alert('El título es obligatorio');return;}
      var payload={
        titulo:titulo.toUpperCase(), categoria:'evento', tipo_nuevo:'evento',
        contenido:(document.getElementById('mv-contenido')||{}).value||titulo,
        fecha_nacimiento:(document.getElementById('mv-fecha')||{}).value||'',
        como_se_conocieron:(document.getElementById('mv-personas')||{}).value||'',
        como_habla_rafa:(document.getElementById('mv-importa')||{}).value||'',
        personalidad:(document.getElementById('mv-recuerda')||{}).value||''
      };
      guardarHitoTipado(payload);
    }
  },
  {
    tipo: 'forma_de_pensar',
    label: 'Formas de pensar',
    icon: '🧠',
    desc: 'Marcos mentales y formas de ver el mundo',
    campos: function(h){
      h=h||{};
      return mkF('TÍTULO (en tus palabras)', mkI('mv-titulo', h.titulo||'', 'Ej: PRIMERO LOS FUNDAMENTOS', true))+
      mkF('DESCRIPCIÓN', mkTA('mv-contenido', h.contenido||'', 'Cómo piensas sobre esto'))+
      mkF('EVIDENCIA', mkI('mv-evidencia', h.evidencia||'', 'Frase concreta que lo demuestra'))+
      mkF('CUÁNDO ACTIVARLO', mkI('mv-cuando', h.cuando||'', 'En qué situaciones ANNI debe usar esto'));
    },
    guardar: function(){
      var titulo=(document.getElementById('mv-titulo')||{}).value||'';
      if(!titulo){alert('El título es obligatorio');return;}
      var payload={
        titulo:titulo.toUpperCase(), categoria:'forma_de_pensar', tipo_nuevo:'forma_de_pensar',
        contenido:(document.getElementById('mv-contenido')||{}).value||titulo,
        evidencia:(document.getElementById('mv-evidencia')||{}).value||'',
        cuando:(document.getElementById('mv-cuando')||{}).value||''
      };
      guardarHitoTipado(payload);
    }
  },
  {
    tipo: 'valor',
    label: 'Valores y creencias',
    icon: '⭐',
    desc: 'Lo que Rafa valora y en lo que cree',
    campos: function(h){
      h=h||{};
      return mkF('TÍTULO (en tus palabras)', mkI('mv-titulo', h.titulo||'', 'Ej: LA HONESTIDAD POR ENCIMA DE TODO', true))+
      mkF('DESCRIPCIÓN', mkTA('mv-contenido', h.contenido||'', 'Qué crees o qué valoras'))+
      mkF('CÓMO SE MANIFIESTA', mkI('mv-manifesta', h.como_habla_rafa||'', 'Cómo aparece esto en tus decisiones'))+
      mkF('ORIGEN', mkI('mv-origen', h.como_se_conocieron||'', 'De dónde viene este valor, si lo sabes'));
    },
    guardar: function(){
      var titulo=(document.getElementById('mv-titulo')||{}).value||'';
      if(!titulo){alert('El título es obligatorio');return;}
      var payload={
        titulo:titulo.toUpperCase(), categoria:'valor', tipo_nuevo:'valor',
        contenido:(document.getElementById('mv-contenido')||{}).value||titulo,
        como_habla_rafa:(document.getElementById('mv-manifesta')||{}).value||'',
        como_se_conocieron:(document.getElementById('mv-origen')||{}).value||''
      };
      guardarHitoTipado(payload);
    }
  },
  {
    tipo: 'patron',
    label: 'Patrones de comportamiento',
    icon: '🔄',
    desc: 'Cosas que haces o que pasan recurrentemente',
    campos: function(h){
      h=h||{};
      return mkF('TÍTULO (en tus palabras)', mkI('mv-titulo', h.titulo||'', 'Ej: ARRANCO RÁPIDO Y REINICIO CUANDO ALGO NO VA', true))+
      mkF('DESCRIPCIÓN DEL PATRÓN', mkTA('mv-contenido', h.contenido||'', 'Cuándo aparece y cómo se manifiesta'))+
      mkF('EVIDENCIA', mkI('mv-evidencia', h.evidencia||'', 'Ejemplo concreto de cuándo pasó'))+
      mkF('ES CONSCIENTE DE ÉL', mkSel('mv-consciente', h.relacion_especifica||'', [
        ['','Seleccionar...'],['si','Sí, lo reconoce'],['parcial','Parcialmente'],['no','No siempre lo ve']
      ]))+
      mkF('CÓMO DEBE USARLO ANNI', mkI('mv-cuando', h.cuando||'', 'Señalarlo, preguntar, dejar pasar...'));
    },
    guardar: function(){
      var titulo=(document.getElementById('mv-titulo')||{}).value||'';
      if(!titulo){alert('El título es obligatorio');return;}
      var payload={
        titulo:titulo.toUpperCase(), categoria:'patron', tipo_nuevo:'patron',
        contenido:(document.getElementById('mv-contenido')||{}).value||titulo,
        evidencia:(document.getElementById('mv-evidencia')||{}).value||'',
        relacion_especifica:(document.getElementById('mv-consciente')||{}).value||'',
        cuando:(document.getElementById('mv-cuando')||{}).value||''
      };
      guardarHitoTipado(payload);
    }
  }
];

// ── Helpers de formulario ──────────────────────────────────────────────────
function mkF(label, inputHtml){
  return '<div style="margin-bottom:8px"><label style="font-size:10px;color:#aaa;letter-spacing:1px;display:block;margin-bottom:3px">'+escH(label)+'</label>'+inputHtml+'</div>';
}
function mkI(id, val, placeholder, bold){
  var s=bold
    ?'width:100%;border:2px solid #cc0000;border-radius:5px;padding:6px 8px;font-size:13px;font-family:inherit'
    :'width:100%;border:1px solid #ddd;border-radius:4px;padding:5px 8px;font-size:13px;font-family:inherit';
  return '<input id="'+id+'" type="text" value="'+escH(val)+'" placeholder="'+escH(placeholder||'')+'" style="'+s+'">';
}
function mkTA(id, val, placeholder){
  return '<textarea id="'+id+'" placeholder="'+escH(placeholder||'')+'" rows="3" style="width:100%;border:1px solid #ddd;border-radius:4px;padding:6px 8px;font-size:13px;font-family:inherit;resize:vertical">'+escH(val)+'</textarea>';
}
function mkSel(id, val, opts){
  var s='width:100%;border:1px solid #ddd;border-radius:4px;padding:5px 8px;font-size:13px;font-family:inherit;background:#fff';
  var html='<select id="'+id+'" style="'+s+'">';
  opts.forEach(function(o){html+='<option value="'+escH(o[0])+'"'+(o[0]===val?' selected':'')+'>'+escH(o[1])+'</option>';});
  return html+'</select>';
}
function mkChk(id, checked, label){
  return '<label style="font-size:13px;color:#444;cursor:pointer"><input id="'+id+'" type="checkbox"'+(checked?' checked':'')+' style="margin-right:5px">'+escH(label)+'</label>';
}
function mkGrid2(a,b){
  return '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:0">'+a+b+'</div>';
}
function mkGrid3(a,b,c){
  return '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:0">'+a+b+c+'</div>';
}

function mvGuardar(btn){
  var tipo=btn.getAttribute('data-tipo');
  var def=MV_TIPOS.filter(function(t){return t.tipo===tipo;})[0];
  if(def) def.guardar();
}

function guardarHitoTipado(payload){
  var tipo=payload.tipo_nuevo||payload.categoria||'manual';
  delete payload.tipo_nuevo;
  fetch('/api/hitos',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({tipo:tipo},payload))
  }).then(r=>r.json()).then(function(d){
    if(d.ok) loadMVPage();
    else alert('Error al guardar');
  });
}

function renderHitoCard(h, content){
  var card=document.createElement('div');card.className='item-card';
  card.dataset.hito=JSON.stringify(h);
  var titulo='<div id="ht-'+h.id+'" style="font-size:15px;font-weight:900;color:#111;margin-bottom:4px">'+(h.titulo?escH(h.titulo):'')+'</div>';
  var badges='';
  if(h.subtipo_relacion) badges+='<span style="font-size:11px;background:#e8f5e9;border:1px solid #81c784;border-radius:4px;padding:2px 6px;margin-right:4px;color:#2e7d32">'+escH(h.subtipo_relacion.replace(/_/g,' '))+'</span>';
  if(h.fallecido) badges+='<span style="font-size:11px;background:#fce4e4;border:1px solid #e57373;border-radius:4px;padding:2px 6px;margin-right:4px;color:#c62828">Fallecido/a</span>';
  if(h.relacion_activa===0) badges+='<span style="font-size:11px;background:#f5f5f5;border:1px solid #bdbdbd;border-radius:4px;padding:2px 6px;margin-right:4px;color:#757575">Sin contacto</span>';
  var ev=h.evidencia?'<div style="font-size:12px;color:#888;font-style:italic;border-left:2px solid #e0e0e0;padding-left:8px;margin-top:6px" id="hev-'+h.id+'">"'+escH(h.evidencia)+'"</div>':'<div id="hev-'+h.id+'"></div>';
  var cuando=h.cuando?'<div style="font-size:11px;color:#aaa;margin-top:4px" id="hcuando-'+h.id+'"><b>Activar:</b> '+escH(h.cuando)+'</div>':'<div id="hcuando-'+h.id+'"></div>';
  var como=h.como?'<div style="font-size:11px;color:#aaa;margin-top:2px" id="hcomo-'+h.id+'"><b>Uso:</b> '+escH(h.como)+'</div>':'<div id="hcomo-'+h.id+'"></div>';
  var peso='<div style="font-size:11px;color:#aaa;margin-top:4px">Peso: <b style="color:#cc0000">'+h.peso.toFixed(1)+'</b></div>';
  card.innerHTML=
    (badges?'<div style="margin-bottom:4px">'+badges+'</div>':'')+titulo+
    '<div id="hc-'+h.id+'" style="font-size:13px;color:#555;line-height:1.5">'+escH(h.contenido)+'</div>'+
    ev+cuando+como+peso+
    '<div class="item-actions">'+
    '<button class="btn-edit" onclick="editHito('+h.id+',this)">Editar</button>'+
    '<button class="btn-del" onclick="delHito('+h.id+')">Borrar</button>'+
    '<button style="font-size:11px;padding:3px 8px;background:none;border:1px solid #aaa;cursor:pointer;font-family:monospace;color:#666;border-radius:3px;margin-left:4px" onclick="verMemoriaExtendida('+h.id+')">+ Memoria extendida</button>'+
    '</div>';
  content.appendChild(card);
}

function loadMVPage(){
  var content=document.getElementById('mem-content');
  if(!content) return;
  content.innerHTML='';

  // Botón reparar embeddings
  var repBtn=document.createElement('div');
  repBtn.style.cssText='margin-bottom:16px;text-align:right';
  repBtn.innerHTML='<button onclick="repararEmbeddings()" style="font-size:11px;padding:5px 14px;background:#f0f0f0;border:1px solid #888;cursor:pointer;font-family:monospace;color:#444;border-radius:4px;font-weight:700;letter-spacing:1px">&#8635; REPARAR EMBEDDINGS</button><span style="font-size:11px;color:#aaa;margin-left:8px">Regenera embeddings y recalcula el universo</span>';
  content.appendChild(repBtn);

  fetch('/api/hitos').then(r=>r.json()).then(function(d){
    var hitos=d.hitos||[];

    MV_TIPOS.forEach(function(def){
      // Filtrar hitos de este tipo
      var del_tipo=hitos.filter(function(h){
        return (h.tipo||'').toLowerCase()===(def.tipo||'').toLowerCase() ||
               (h.categoria||'').toLowerCase()===(def.tipo||'').toLowerCase();
      });

      // Acordeón
      var acc=document.createElement('div');
      acc.style.cssText='border:1px solid #e0e0e0;border-radius:8px;margin-bottom:10px;overflow:hidden';

      // Cabecera
      var header=document.createElement('div');
      header.style.cssText='display:flex;align-items:center;justify-content:space-between;padding:12px 16px;cursor:pointer;background:#fafafa;user-select:none';
      header.innerHTML=
        '<div style="display:flex;align-items:center;gap:10px">'+
        '<span style="font-size:18px">'+def.icon+'</span>'+
        '<div>'+
        '<div style="font-size:14px;font-weight:900;color:#111;letter-spacing:0.5px">'+escH(def.label)+'</div>'+
        '<div style="font-size:11px;color:#aaa;margin-top:1px">'+escH(def.desc)+'</div>'+
        '</div></div>'+
        '<div style="display:flex;align-items:center;gap:8px">'+
        '<span style="font-size:12px;color:#888;background:#f0f0f0;border-radius:10px;padding:2px 8px">'+del_tipo.length+'</span>'+
        '<span class="acc-arrow" style="font-size:14px;color:#aaa;transition:transform 0.2s">▼</span>'+
        '</div>';

      // Cuerpo
      var body=document.createElement('div');
      body.style.cssText='display:none;padding:16px;border-top:1px solid #e0e0e0;background:#fff';

      // Formulario nueva memoria de este tipo
      var formDiv=document.createElement('div');
      formDiv.style.cssText='background:#fff8f8;border:1px solid #ffcccc;border-radius:6px;padding:14px;margin-bottom:14px';
      formDiv.innerHTML=
        '<div style="font-size:10px;font-weight:900;color:#cc0000;letter-spacing:2px;margin-bottom:10px">+ AÑADIR '+escH(def.label.toUpperCase())+'</div>'+
        def.campos()+
        '<div style="margin-top:10px">'+
        '<button data-tipo="'+def.tipo+'" onclick="mvGuardar(this)" style="font-size:11px;padding:5px 16px;background:#cc0000;color:#fff;border:none;cursor:pointer;font-family:monospace;border-radius:3px;letter-spacing:1px">GUARDAR</button>'
        '</div>';
      body.appendChild(formDiv);

      // Lista de memorias guardadas
      if(del_tipo.length===0){
        var empty=document.createElement('p');
        empty.style.cssText='color:#bbb;font-size:13px;padding:8px 0;font-family:monospace';
        empty.textContent='Sin '+def.label.toLowerCase()+' guardadas aún.';
        body.appendChild(empty);
      } else {
        del_tipo.forEach(function(h){ renderHitoCard(h, body); });
      }

      // Toggle acordeón
      var abierto=false;
      header.onclick=function(){
        abierto=!abierto;
        body.style.display=abierto?'block':'none';
        header.querySelector('.acc-arrow').style.transform=abierto?'rotate(180deg)':'rotate(0deg)';
        header.style.background=abierto?'#fff':'#fafafa';
      };

      // Abrir automáticamente si tiene memorias
      if(del_tipo.length>0){
        abierto=true;
        body.style.display='block';
        header.querySelector('.acc-arrow').style.transform='rotate(180deg)';
        header.style.background='#fff';
      }

      acc.appendChild(header);
      acc.appendChild(body);
      content.appendChild(acc);
    });
  });
}

function borrarObservacion(id, btn){
  fetch('/api/observaciones/'+id,{method:'DELETE'}).then(r=>r.json()).then(function(d){
    if(d.ok){var card=btn.closest('.item-card');if(card)card.remove();}
  });
}

function borrarTema(id, btn){
  fetch('/api/temas-abiertos/'+id,{method:'DELETE'}).then(r=>r.json()).then(function(d){
    if(d.ok){var card=btn.closest('.item-card');if(card)card.remove();}
  });
}

function borrarPersona(nombre, btn){
  if(!confirm('Borrar a '+nombre+' de personas detectadas?')) return;
  fetch('/api/personas/rechazar',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({nombre:nombre})
  }).then(r=>r.json()).then(function(d){
    if(d.ok){
      var card=btn.closest('.item-card');
      if(card) card.remove();
    }
  });
}

// guardarNuevaMV eliminada — reemplazada por guardarHitoTipado


var famRels=['pareja','esposa','esposo','hijo','hija','hijastra','hijastro','suegro','suegra','cuñado','cuñada','padre','madre','hermano','hermana'];
var trabajoRels=['colega','jefe','cliente','socio','socia','fundador','cofundador'];
var amigosRels=['amigo','amiga'];

function loadMemoriaAnni(){
  var body=document.getElementById('page-body');
  body.innerHTML='';

  // Tabs
  var tabs=document.createElement('div');
  tabs.style.cssText='display:flex;gap:8px;margin-bottom:20px;border-bottom:1px solid #222;padding-bottom:12px';
  var tabDefs=[
    {id:'cruda',label:'Memoria cruda',desc:'Mensajes y conversaciones — lo que pasó'},
    {id:'interpretada',label:'Memoria interpretada',desc:'Observaciones, personas, temas — lo que ANNI cree que pasó'},
    {id:'validada',label:'Memoria validada',desc:'Anclas cognitivas confirmadas — lo que es verdad operativa'},
    {id:'extendida',label:'Memoria extendida',desc:'Biografías y contexto profundo por persona o tema'}
  ];
  var activeTab='validada';

  function renderTab(tabId){
    activeTab=tabId;
    // Update tab styles
    tabs.querySelectorAll('.mem-tab').forEach(function(t){
      t.style.background=t.dataset.tab===tabId?'#cc0000':'none';
      t.style.color=t.dataset.tab===tabId?'#fff':'#666';
      t.style.borderColor=t.dataset.tab===tabId?'#cc0000':'#333';
    });
    var content=document.getElementById('mem-content');
    content.innerHTML='<p style="color:#555;padding:20px;font-family:monospace">Cargando...</p>';

    if(tabId==='validada'){
      loadMVPage();

    } else if(tabId==='cruda'){
      fetch('/api/chats?page=1&per_page=20').then(r=>r.json()).then(function(d){
        content.innerHTML='';
        if(!d.chats||!d.chats.length){content.innerHTML='<p style="color:#999;padding:20px">Sin conversaciones guardadas.</p>';return;}
        d.chats.forEach(function(c){
          var card=document.createElement('div');card.className='item-card';
          card.innerHTML='<div class="item-meta">Chat #'+c.id+' &middot; '+c.inicio+'</div><div class="item-content" id="cc-'+c.id+'">'+escH(c.resumen)+'</div><div class="item-actions"><button class="btn-edit" onclick="editChat('+c.id+',this)">Editar</button></div>';
          content.appendChild(card);
        });
        content.appendChild(pagerEl(d.pages,1,'loadChats'));
      });

    } else if(tabId==='extendida'){
      fetch('/api/memoria-extendida').then(r=>r.json()).then(function(d){
        content.innerHTML='';
        var addBtn=document.createElement('div');
        addBtn.style.cssText='text-align:right;margin-bottom:12px';
        addBtn.innerHTML='<button onclick="nuevaMemoriaExtendida()" style="font-size:11px;padding:4px 12px;background:#cc0000;color:#fff;border:none;cursor:pointer;font-family:monospace;border-radius:3px;letter-spacing:1px">+ NUEVA</button>';
        content.appendChild(addBtn);
        if(!d.memorias||!d.memorias.length){
          var empty=document.createElement('p');
          empty.style.cssText='color:#999;padding:20px;font-size:13px';
          empty.textContent='Sin memorias extendidas aún. Puedes crear una desde aquí o desde una memoria validada.';
          content.appendChild(empty);return;
        }
        d.memorias.forEach(function(m){
          var card=document.createElement('div');card.className='item-card';
          var linked=m.memoria_validada_titulo?'<span style="font-size:11px;background:#f5f5f5;border:1px solid #e0e0e0;border-radius:4px;padding:2px 7px;margin-right:6px">'+escH(m.memoria_validada_titulo)+'</span>':'';
          card.innerHTML='<div class="item-meta">'+linked+'#'+m.id+' &middot; '+m.ts+'</div>'+
            '<div style="font-size:15px;font-weight:900;color:#111;margin-bottom:6px">'+escH(m.titulo||'Sin título')+'</div>'+
            '<div style="font-size:14px;color:#444;line-height:1.6" id="me-'+m.id+'">'+escH(m.contenido)+'</div>'+
            '<div style="font-size:11px;color:#aaa;margin-top:6px">Tipo: '+escH(m.tipo)+'</div>'+
            '<div class="item-actions">'+
            '<button class="btn-edit" onclick="editMemoriaExtendida('+m.id+',this)">Editar</button>'+
            '<button class="btn-del" onclick="delMemoriaExtendida('+m.id+')">Borrar</button>'+
            '</div>';
          content.appendChild(card);
        });
      }).catch(function(){content.innerHTML='<p style="color:#999;padding:20px">Error cargando memoria extendida.</p>';});

    } else if(tabId==='interpretada'){
      content.innerHTML='';

      var famRels=['pareja','esposa','esposo','hijo','hija','hijastra','hijastro','suegro','suegra','cuñado','cuñada','padre','madre','hermano','hermana'];
      var trabajoRels=['colega','jefe','cliente','socio','socia','amigo','amiga','fundador','cofundador'];

      // Helper: collapsible section
      function makeSection(titulo, items, renderFn){
        if(!items||!items.length) return null;
        var wrap=document.createElement('div');
        wrap.style.cssText='margin-bottom:4px;border:1px solid #eee;border-radius:6px;overflow:hidden';

        var header=document.createElement('div');
        header.style.cssText='display:flex;align-items:center;justify-content:space-between;padding:10px 14px;cursor:pointer;background:#fafafa;user-select:none';
        header.innerHTML='<span style="font-size:12px;font-weight:900;color:#cc0000;letter-spacing:2px">'+titulo+'</span>'+
          '<span style="display:flex;align-items:center;gap:8px"><span style="font-size:11px;color:#aaa">'+items.length+'</span>'+
          '<span class="sec-arrow" style="color:#cc0000;font-size:14px;transition:transform 0.2s">&#9660;</span></span>';

        var body=document.createElement('div');
        body.style.cssText='padding:8px 8px 4px';
        items.forEach(function(item){ body.appendChild(renderFn(item)); });

        var open=true;
        header.onclick=function(){
          open=!open;
          body.style.display=open?'block':'none';
          header.querySelector('.sec-arrow').style.transform=open?'rotate(0deg)':'rotate(-90deg)';
        };

        wrap.appendChild(header);
        wrap.appendChild(body);
        return wrap;
      }

      // Helper: persona card with edit
      function personaCard(p){
        var card=document.createElement('div');card.className='item-card';
        var nombreCompleto=p.nombre+(p.apellidos?' '+p.apellidos:'');
        card.innerHTML='<div class="item-meta" id="pmeta-'+p.id+'">'+escH(p.relacion||'')+'</div>'+
          '<div style="font-weight:900;font-size:15px;margin-bottom:4px" id="pnom-'+p.id+'">'+escH(nombreCompleto)+'</div>'+
          '<div style="font-size:13px;color:#666">Mencionada '+p.veces_mencionada+' vez'+(p.veces_mencionada!==1?'es':'')+'</div>';
        var bBtn=document.createElement('div');bBtn.className='item-actions';
        var bE=document.createElement('button');bE.className='btn-edit';bE.textContent='Editar';
        bE.onclick=(function(persona){return function(){editarPersona(persona,card);};})(p);
        var bB=document.createElement('button');bB.className='btn-del';bB.textContent='Borrar';
        bB.onclick=(function(nombre){return function(){borrarPersona(nombre,bB);};})(p.nombre);
        bBtn.appendChild(bE);bBtn.appendChild(bB);card.appendChild(bBtn);
        return card;
      }

      function editarPersona(p, card){
        // Replace card content with edit form
        var existing=card.querySelector('.persona-edit-form');
        if(existing){existing.remove();return;}
        var form=document.createElement('div');
        form.className='persona-edit-form';
        form.style.cssText='margin-top:10px;border-top:1px solid #eee;padding-top:10px';
        form.innerHTML=
          '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:6px">'+
          '<div><label style="font-size:10px;color:#aaa;letter-spacing:1px">NOMBRE</label>'+
          '<input id="pe-nom-'+p.id+'" value="'+escH(p.nombre)+'" style="width:100%;border:1px solid #ddd;border-radius:4px;padding:5px 8px;font-size:13px;font-family:inherit"></div>'+
          '<div><label style="font-size:10px;color:#aaa;letter-spacing:1px">APELLIDOS</label>'+
          '<input id="pe-ape-'+p.id+'" value="'+escH(p.apellidos||'')+'" placeholder="Opcional" style="width:100%;border:1px solid #ddd;border-radius:4px;padding:5px 8px;font-size:13px;font-family:inherit"></div>'+
          '</div>'+
          '<div style="margin-bottom:8px"><label style="font-size:10px;color:#aaa;letter-spacing:1px">RELACIÓN</label>'+
          '<input id="pe-rel-'+p.id+'" value="'+escH(p.relacion||'')+'" style="width:100%;border:1px solid #ddd;border-radius:4px;padding:5px 8px;font-size:13px;font-family:inherit"></div>'+
          '<button class="btn-save-p" data-pid="'+p.id+'" style="font-size:11px;padding:4px 12px;background:#cc0000;color:#fff;border:none;cursor:pointer;font-family:monospace;border-radius:3px">GUARDAR</button>';
        form.querySelector('.btn-save-p').onclick=function(){guardarPersona(p.id);};
        card.appendChild(form);
      }

      function guardarPersona(id){
        var nom=(document.getElementById('pe-nom-'+id)||{value:''}).value.trim();
        var ape=(document.getElementById('pe-ape-'+id)||{value:''}).value.trim();
        var rel=(document.getElementById('pe-rel-'+id)||{value:''}).value.trim();
        if(!nom) return;
        fetch('/api/personas/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({nombre:nom,apellidos:ape,relacion:rel})
        }).then(r=>r.json()).then(function(d){
          if(d.ok) renderTab('interpretada');
        });
      }

      // Helper: observacion card with delete
      function obsCard(o){
        var card=document.createElement('div');card.className='item-card';
        var tipoColors={patron:'#666',emocion:'#994499',energia:'#cc6600',velocidad:'#006699',evitacion:'#cc0000'};
        var col=tipoColors[o.tipo]||'#666';
        var tipoTag='<span style="font-size:11px;background:#f9f9f9;border:1px solid #e0e0e0;border-radius:4px;padding:2px 7px;margin-right:6px;color:'+col+'">'+escH(o.tipo||'')+'</span>';
        card.innerHTML='<div class="item-meta">'+tipoTag+o.ts+'</div>'+
          '<div style="font-size:14px;color:#333;margin-top:4px">'+escH(o.contenido)+'</div>'+
          (o.evidencia?'<div style="font-size:12px;color:#888;margin-top:6px;font-style:italic;border-left:3px solid #e0e0e0;padding-left:8px">"'+escH(o.evidencia)+'"</div>':'');
        var bBtn=document.createElement('div');bBtn.className='item-actions';
        var bB=document.createElement('button');bB.className='btn-del';bB.textContent='Borrar';
        bB.onclick=(function(id){return function(){borrarObservacion(id,bB);};})(o.id);
        bBtn.appendChild(bB);card.appendChild(bBtn);
        return card;
      }

      // Helper: tema card with delete
      function temaCard(t){
        var card=document.createElement('div');card.className='item-card';
        card.innerHTML='<div class="item-meta">'+t.ts+'</div>'+
          '<div style="font-size:14px;color:#333;margin-top:4px">'+escH(t.tema)+'</div>'+
          '<div style="font-size:12px;color:#aaa;margin-top:4px">Mencionado '+t.veces+' veces</div>';
        var bBtn=document.createElement('div');bBtn.className='item-actions';
        var bB=document.createElement('button');bB.className='btn-del';bB.textContent='Borrar';
        bB.onclick=(function(id){return function(){borrarTema(id,bB);};})(t.id);
        bBtn.appendChild(bB);card.appendChild(bBtn);
        return card;
      }

      Promise.all([
        fetch('/api/personas').then(r=>r.json()).catch(function(){return {personas:[]};}),
        fetch('/api/observaciones').then(r=>r.json()).catch(function(){return {observaciones:[]};}),
        fetch('/api/temas-abiertos').then(r=>r.json()).catch(function(){return {temas:[]};})
      ]).then(function(results){
        var dp=results[0]||{personas:[]};
        var do2=results[1]||{observaciones:[]};
        var dt=results[2]||{temas:[]};

        // Deduplicar personas por nombre normalizado (Antonio/Antonio Torrijos → el de más menciones)
        var personasMap={};
        (dp.personas||[]).forEach(function(p){
          var key=p.nombre.toLowerCase().split(' ')[0]; // primer nombre
          var rel=(p.relacion||'').toLowerCase();
          var esFam=famRels.some(function(r){return rel.indexOf(r)>=0;});
          var esTrab=trabajoRels.some(function(r){return rel.indexOf(r)>=0;});
          var esAmigo=amigosRels.some(function(r){return rel.indexOf(r)>=0;});
          if(!esFam && !esTrab && !esAmigo) return;
          if(!personasMap[key] || p.veces_mencionada > personasMap[key].veces_mencionada){
            personasMap[key]=p;
          }
        });
        var personasDedup=Object.values(personasMap);

        var familia=personasDedup.filter(function(p){
          var rel=(p.relacion||'').toLowerCase();
          return famRels.some(function(r){return rel.indexOf(r)>=0;});
        }).sort(function(a,b){return b.veces_mencionada-a.veces_mencionada;});

        var trabajo=personasDedup.filter(function(p){
          var rel=(p.relacion||'').toLowerCase();
          return trabajoRels.some(function(r){return rel.indexOf(r)>=0;});
        }).sort(function(a,b){return b.veces_mencionada-a.veces_mencionada;});

        var amigos=personasDedup.filter(function(p){
          var rel=(p.relacion||'').toLowerCase();
          return amigosRels.some(function(r){return rel.indexOf(r)>=0;});
        }).sort(function(a,b){return b.veces_mencionada-a.veces_mencionada;});

        // FAMILIA
        var secFam=makeSection('FAMILIA', familia, personaCard);
        if(secFam) content.appendChild(secFam);

        // TRABAJO
        var secTrab=makeSection('TRABAJO', trabajo, personaCard);
        if(secTrab) content.appendChild(secTrab);

        // AMIGOS
        var secAmigos=makeSection('AMIGOS', amigos, personaCard);
        if(secAmigos) content.appendChild(secAmigos);

        // PATRONES
        var patrones=(do2.observaciones||[]).filter(function(o){return o.tipo==='patron'||o.tipo==='velocidad'||o.tipo==='evitacion';});
        var secPat=makeSection('PATRONES', patrones, obsCard);
        if(secPat) content.appendChild(secPat);

        // EMOCIONES Y ENERGÍA
        var emociones=(do2.observaciones||[]).filter(function(o){return o.tipo==='emocion'||o.tipo==='energia';});
        var secEmo=makeSection('EMOCIONES Y ENERGÍA', emociones, obsCard);
        if(secEmo) content.appendChild(secEmo);

        // TEMAS ABIERTOS (filtrar basura)
        var temasBuenos=(dt.temas||[]).filter(function(t){
          var tema=t.tema.toLowerCase();
          var basura=['cierre','conversación','conversacion','abordar transporte','partido de fútbol','partido de futbol','workbook','ponderación','ponderacion'];
          return !basura.some(function(b){return tema.indexOf(b)>=0;});
        });
        var secTemas=makeSection('TEMAS ABIERTOS', temasBuenos, temaCard);
        if(secTemas) content.appendChild(secTemas);

        if(!familia.length && !trabajo.length && !amigos.length && !patrones.length && !emociones.length){
          var empty=document.createElement('p');
          empty.style.cssText='color:#999;padding:20px';
          empty.textContent='Aún no hay información interpretada.';
          content.appendChild(empty);
        }

      }).catch(function(err){
        content.innerHTML='<p style="color:#999;padding:20px">Error cargando memoria interpretada: '+(err?err.toString():'')+'. Intenta recargar la página.</p>';
      });
    }
  }

  tabDefs.forEach(function(td){
    var btn=document.createElement('button');
    btn.className='mem-tab';
    btn.dataset.tab=td.id;
    btn.style.cssText='font-family:monospace;font-size:11px;letter-spacing:1px;padding:6px 14px;border:1px solid #333;cursor:pointer;border-radius:3px;background:none;color:#666;transition:all 0.2s';
    btn.textContent=td.label.toUpperCase();
    btn.onclick=function(){renderTab(td.id);};
    tabs.appendChild(btn);
  });
  body.appendChild(tabs);

  var contentDiv=document.createElement('div');
  contentDiv.id='mem-content';
  body.appendChild(contentDiv);

  renderTab('validada');
}

function loadHitos(page){
fetch('/api/hitos?page='+page).then(r=>r.json()).then(d=>{
var body=document.getElementById('page-body');body.innerHTML='';
// Repair button
var repDiv=document.createElement('div');
repDiv.style.cssText='text-align:right;margin-bottom:10px';
repDiv.innerHTML='<button onclick="repararEmbeddings()" style="font-size:11px;padding:5px 14px;background:#f0f0f0;border:1px solid #888;cursor:pointer;font-family:monospace;color:#444;border-radius:4px;font-weight:700">↻ REPARAR EMBEDDINGS</button>';
body.appendChild(repDiv);
if(!d.hitos.length){body.innerHTML='<p style="color:#999;padding:20px">Sin memorias validadas aun.</p>';return;}
d.hitos.forEach(function(h){
var card=document.createElement('div');card.className='item-card';
var titulo='<div id="ht-'+h.id+'" style="font-size:16px;font-weight:900;color:#111;margin-bottom:4px">'+(h.titulo?escH(h.titulo):'')+'</div>';
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

function editHito(id,btn,categoria){
  var card=btn.closest('.item-card');
  if(!categoria&&card.dataset.hito){try{categoria=JSON.parse(card.dataset.hito).tipo||'';}catch(e){}}
  var titleEl=card.querySelector('[id="ht-'+id+'"]');
  var contentEl=card.querySelector('[id="hc-'+id+'"]');
  var evEl=card.querySelector('[id="hev-'+id+'"]');
  var cuandoEl=card.querySelector('[id="hcuando-'+id+'"]');
  var comoEl=card.querySelector('[id="hcomo-'+id+'"]');
  if(!contentEl)return;

  var origTitle=titleEl?titleEl.textContent.trim():'';
  var origContent=contentEl.textContent.trim();
  var origEv=evEl?(evEl.textContent.replace(/^"|"$/g,'').trim()):'';
  var origCuando=cuandoEl?(cuandoEl.textContent.replace(/^Activar:\s*/,'').trim()):'';
  var origComo=comoEl?(comoEl.textContent.replace(/^Uso:\s*/,'').trim()):'';

  var esRelacion=(categoria||'').toLowerCase()==='relacion';
  var dashParts=origTitle.split(' — ');
  var namePart=dashParts[0]||'';
  var nameWords=namePart.split(' ');
  var origNombre=nameWords[0]||'';
  var origApellidos=nameWords.slice(1).join(' ')||'';
  var origRelPart=dashParts.slice(1).join(' — ')||'';

  // Helper para crear campos del formulario de persona
  function mkField(label, inputHtml){
    return '<div style="margin-bottom:8px">'+
      '<label style="font-size:10px;color:#aaa;letter-spacing:1px;display:block;margin-bottom:2px">'+label+'</label>'+
      inputHtml+'</div>';
  }
  function inp(id, val, placeholder, bold){
    var s='width:100%;border:1px solid #ddd;border-radius:4px;padding:5px 8px;font-size:13px;font-family:inherit';
    if(bold) s='width:100%;border:2px solid #cc0000;border-radius:6px;padding:6px 8px;font-size:14px;font-weight:900;font-family:inherit';
    return '<input id="'+id+'" type="text" value="'+escH(val)+'" placeholder="'+escH(placeholder||'')+'" style="'+s+'">';
  }
  function sel(id, val, opts){
    var s='width:100%;border:1px solid #ddd;border-radius:4px;padding:5px 8px;font-size:13px;font-family:inherit;background:#fff';
    var html='<select id="'+id+'" style="'+s+'">';
    opts.forEach(function(o){html+='<option value="'+escH(o[0])+'"'+(o[0]===val?' selected':'')+'>'+escH(o[1])+'</option>';});
    return html+'</select>';
  }
  function chk(id, val, label){
    return '<label style="font-size:13px;color:#444;cursor:pointer"><input id="'+id+'" type="checkbox"'+(val?' checked':'')+' style="margin-right:6px">'+escH(label)+'</label>';
  }

  // Leer valores de persona del hito actual
  var h=card.dataset.hito?JSON.parse(card.dataset.hito):{};

  if(titleEl){
    if(esRelacion){
      titleEl.innerHTML=
        '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:6px">'+
        mkField('NOMBRE', inp('edit-nom-'+id, h.nombre_propio||origNombre, '', true))+
        mkField('APELLIDOS', inp('edit-ape-'+id, h.apellidos||origApellidos, 'Opcional', true))+
        mkField('MOTE / APODO', inp('edit-mote-'+id, h.mote||'', 'Como le llamas tú', true))+
        '</div>'+
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:6px">'+
        mkField('TIPO DE RELACIÓN', sel('edit-sub-'+id, h.subtipo_relacion||'', [
          ['','Seleccionar...'],['familia_propia','Familia propia'],['familia_directa','Familia directa'],
          ['familia_directa_extendida','Familia directa extendida'],['familia_politica_directa','Familia política directa'],
          ['familia_politica_extendida','Familia política extendida'],['expareja','Expareja'],
          ['amigos','Amigos y amigas'],['mentor','Mentor / Figura de influencia'],
          ['colegas_trabajo','Colegas de trabajo'],['relaciones_trabajo','Relaciones de trabajo'],
          ['conocido','Conocido'],['otros','Otros']
        ]))+
        mkField('RELACIÓN ESPECÍFICA', inp('edit-rel-'+id, h.relacion_especifica||'', 'Madre, Padre, Mejor amigo...'))+
        '</div>'+
        '<div style="display:flex;gap:20px;margin-bottom:10px;padding:8px;background:#f9f9f9;border-radius:4px">'+
        chk('edit-fall-'+id, h.fallecido, 'Fallecido/a')+
        '<div id="edit-fall-fecha-wrap-'+id+'" style="'+(h.fallecido?'':'display:none')+';margin-left:8px">'+
        inp('edit-fall-fecha-'+id, h.fecha_fallecimiento||'', 'Fecha fallecimiento')+
        '</div>'+
        '<span style="margin-left:20px">'+chk('edit-activa-'+id, h.relacion_activa!==0, 'Relación activa')+'</span>'+
        '</div>';
      // Toggle fecha fallecimiento
      setTimeout(function(){
        var fc=document.getElementById('edit-fall-'+id);
        if(fc) fc.onchange=function(){
          var w=document.getElementById('edit-fall-fecha-wrap-'+id);
          if(w) w.style.display=fc.checked?'':'none';
        };
      },50);
    } else {
      titleEl.innerHTML=
        '<div style="margin-bottom:6px">'+
        mkField('TÍTULO', inp('edit-tit-'+id, origTitle, '', true))+
        '</div>';
    }
  }

  contentEl.innerHTML='<textarea id="edit-con-'+id+'" style="width:100%;border:2px solid #cc0000;border-radius:6px;padding:8px 10px;font-size:14px;font-family:inherit;resize:vertical;min-height:60px" rows="3">'+escH(origContent)+'</textarea>';

  if(esRelacion){
    // Campos adicionales de persona — debajo del contenido
    var personaExtra=document.createElement('div');
    personaExtra.id='edit-persona-extra-'+id;
    personaExtra.style.cssText='margin-top:12px;border-top:1px solid #f0f0f0;padding-top:12px';
    personaExtra.innerHTML=
      '<div style="font-size:10px;color:#aaa;letter-spacing:2px;margin-bottom:10px">SOBRE LA PERSONA</div>'+
      '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px">'+
      mkField('PROFESIÓN', inp('edit-prof-'+id, h.profesion||'', 'Qué hace'))+
      mkField('DÓNDE VIVE', inp('edit-vive-'+id, h.donde_vive||'', 'Ciudad, País'))+
      mkField('FECHA NACIMIENTO', inp('edit-nac-'+id, h.fecha_nacimiento||'', 'Año o fecha'))+
      '</div>'+
      mkField('PERSONALIDAD', inp('edit-pers-'+id, h.personalidad||'', 'Directo, optimista, reservado...'))+
      '<div style="font-size:10px;color:#aaa;letter-spacing:2px;margin-top:10px;margin-bottom:10px">CONTEXTO DE LA RELACIÓN</div>'+
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">'+
      mkField('CÓMO SE CONOCIERON', inp('edit-conocio-'+id, h.como_se_conocieron||'', 'Colegio, trabajo, viaje...'))+
      mkField('DESDE CUÁNDO', inp('edit-desde-'+id, h.desde_cuando||'', 'Año o época'))+
      '</div>'+
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">'+
      mkField('FRECUENCIA DE CONTACTO', sel('edit-frec-'+id, h.frecuencia_contacto||'', [
        ['','Seleccionar...'],['diario','Diario'],['semanal','Semanal'],
        ['ocasional','Ocasional'],['raramente','Raramente'],['sin_contacto','Sin contacto']
      ]))+
      mkField('ÚLTIMO CONTACTO', inp('edit-ult-'+id, h.ultimo_contacto||'', 'Cuándo fue el último contacto'))+
      '</div>'+
      '<div style="font-size:10px;color:#aaa;letter-spacing:2px;margin-top:10px;margin-bottom:10px">PARA ANNI</div>'+
      mkField('CÓMO HABLA RAFA DE ESTA PERSONA', inp('edit-habla-'+id, h.como_habla_rafa||'', 'Con nostalgia, con orgullo, con preocupación...'))+
      mkField('TEMAS RECURRENTES', inp('edit-temas-'+id, h.temas_recurrentes||'', 'Fútbol y trabajo, familia, proyectos...'));
    contentEl.parentNode.insertBefore(personaExtra, contentEl.nextSibling);
  }

  if(evEl) evEl.innerHTML='<input id="edit-ev-'+id+'" type="text" value="'+escH(origEv)+'" placeholder="Evidencia (frase exacta)" style="width:100%;border:1px solid #e0e0e0;border-radius:4px;padding:5px 8px;font-size:12px;font-family:inherit;font-style:italic;margin-top:4px">';
  if(cuandoEl) cuandoEl.innerHTML='<input id="edit-cuan-'+id+'" type="text" value="'+escH(origCuando)+'" placeholder="¿Cuándo activar?" style="width:100%;border:1px solid #e0e0e0;border-radius:4px;padding:5px 8px;font-size:12px;font-family:inherit;margin-top:4px">';
  if(comoEl) comoEl.innerHTML='<input id="edit-como-'+id+'" type="text" value="'+escH(origComo)+'" placeholder="¿Cómo usar?" style="width:100%;border:1px solid #e0e0e0;border-radius:4px;padding:5px 8px;font-size:12px;font-family:inherit;margin-top:4px">';

  btn.textContent='Guardar';
  btn.onclick=function(){
    var nomEl=document.getElementById('edit-nom-'+id);
    var apeEl=document.getElementById('edit-ape-'+id);
    var moteEl=document.getElementById('edit-mote-'+id);
    var subEl=document.getElementById('edit-sub-'+id);
    var relEl=document.getElementById('edit-rel-'+id);
    var fallEl=document.getElementById('edit-fall-'+id);
    var fallFechaEl=document.getElementById('edit-fall-fecha-'+id);
    var activaEl=document.getElementById('edit-activa-'+id);
    var titEl=document.getElementById('edit-tit-'+id);
    var conEl=document.getElementById('edit-con-'+id);
    var evEl2=document.getElementById('edit-ev-'+id);
    var cuanEl=document.getElementById('edit-cuan-'+id);
    var comoEl2=document.getElementById('edit-como-'+id);
    // Campos adicionales persona
    var profEl=document.getElementById('edit-prof-'+id);
    var viveEl=document.getElementById('edit-vive-'+id);
    var nacEl=document.getElementById('edit-nac-'+id);
    var persEl=document.getElementById('edit-pers-'+id);
    var conocioEl=document.getElementById('edit-conocio-'+id);
    var desdeEl=document.getElementById('edit-desde-'+id);
    var frecEl=document.getElementById('edit-frec-'+id);
    var ultEl=document.getElementById('edit-ult-'+id);
    var hablaEl=document.getElementById('edit-habla-'+id);
    var temasEl=document.getElementById('edit-temas-'+id);

    var nuevoTitulo;
    if(esRelacion){
      var nom=nomEl?nomEl.value.trim():origNombre;
      var ape=apeEl?apeEl.value.trim():origApellidos;
      nuevoTitulo=(nom+(ape?' '+ape:'')).toUpperCase();
    } else {
      nuevoTitulo=titEl?titEl.value.trim():origTitle;
    }
    if(!nuevoTitulo||!nuevoTitulo.trim()) nuevoTitulo=origTitle;
    var contenido=conEl?conEl.value.trim():'';
    if(!contenido){alert('El contenido no puede estar vacío.');return;}
    var payload={
      titulo: nuevoTitulo.trim(),
      contenido: contenido,
      evidencia: evEl2?evEl2.value.trim():'',
      cuando: cuanEl?cuanEl.value.trim():'',
      como: comoEl2?comoEl2.value.trim():'',
      nombre_propio: nomEl?nomEl.value.trim():'',
      apellidos: apeEl?apeEl.value.trim():'',
      mote: moteEl?moteEl.value.trim():'',
      subtipo_relacion: subEl?subEl.value:'',
      relacion_especifica: relEl?relEl.value.trim():'',
      fallecido: fallEl&&fallEl.checked?1:0,
      fecha_fallecimiento: fallFechaEl?fallFechaEl.value.trim():'',
      relacion_activa: activaEl&&activaEl.checked?1:0,
      profesion: profEl?profEl.value.trim():'',
      donde_vive: viveEl?viveEl.value.trim():'',
      fecha_nacimiento: nacEl?nacEl.value.trim():'',
      personalidad: persEl?persEl.value.trim():'',
      como_se_conocieron: conocioEl?conocioEl.value.trim():'',
      desde_cuando: desdeEl?desdeEl.value.trim():'',
      frecuencia_contacto: frecEl?frecEl.value:'',
      ultimo_contacto: ultEl?ultEl.value.trim():'',
      como_habla_rafa: hablaEl?hablaEl.value.trim():'',
      temas_recurrentes: temasEl?temasEl.value.trim():''
    };
    fetch('/api/hitos/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
    .then(r=>r.json()).then(function(d){
      if(d.ok) loadMVPage();
      else alert('Error al guardar');
    });
  };
}


function verMemoriaExtendida(memoriaValidadaId){
  var existingPanel=document.getElementById('me-panel-'+memoriaValidadaId);
  if(existingPanel){existingPanel.remove();return;}

  fetch('/api/memoria-extendida?memoria_validada_id='+memoriaValidadaId).then(function(r){return r.json();}).then(function(d){
    var panel=document.createElement('div');
    panel.id='me-panel-'+memoriaValidadaId;
    panel.style.cssText='background:#f9f9f9;border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin-top:8px';

    var html='<div style="font-size:12px;font-weight:900;color:#cc0000;letter-spacing:2px;margin-bottom:12px">MEMORIA EXTENDIDA</div>';

    if(d.memorias&&d.memorias.length){
      d.memorias.forEach(function(m){
        html+='<div style="border-bottom:1px solid #eee;padding-bottom:10px;margin-bottom:10px">';
        html+='<div style="font-weight:900;font-size:13px;margin-bottom:4px">'+escH(m.titulo||'Sin título')+'</div>';
        html+='<div style="font-size:13px;color:#444;line-height:1.6;white-space:pre-wrap" id="mep-'+m.id+'">'+escH(m.contenido)+'</div>';
        html+='<div style="margin-top:6px">';
        html+='<button onclick="editMEInline('+m.id+')" style="font-size:10px;padding:2px 8px;border:1px solid #ddd;background:none;cursor:pointer;font-family:monospace;margin-right:4px">Editar</button>';
        html+='<button onclick="delMemExt('+m.id+','+memoriaValidadaId+')" style="font-size:10px;padding:2px 8px;border:1px solid #ffcccc;background:none;cursor:pointer;font-family:monospace;color:#cc0000">Borrar</button>';
        html+='</div></div>';
      });
    } else {
      html+='<p style="color:#999;font-size:13px;margin-bottom:12px">Sin memorias extendidas aún.</p>';
    }

    html+='<div style="margin-top:12px;border-top:1px solid #eee;padding-top:12px">';
    html+='<div style="font-size:11px;color:#aaa;margin-bottom:6px">AÑADIR NUEVA ENTRADA</div>';
    html+='<input id="me-tit-'+memoriaValidadaId+'" placeholder="Título" style="width:100%;border:1px solid #ddd;border-radius:4px;padding:6px 10px;font-size:13px;font-family:inherit;margin-bottom:6px"><br>';
    html+='<textarea id="me-body-'+memoriaValidadaId+'" placeholder="Escribe aquí el contexto extendido..." style="width:100%;border:1px solid #ddd;border-radius:4px;padding:8px 10px;font-size:13px;font-family:inherit;resize:vertical;min-height:80px"></textarea><br>';
    html+='<button onclick="guardarMEInline('+memoriaValidadaId+')" style="margin-top:6px;font-size:11px;padding:4px 14px;background:#cc0000;color:#fff;border:none;cursor:pointer;font-family:monospace;border-radius:3px;letter-spacing:1px">GUARDAR</button>';
    html+='</div>';

    panel.innerHTML=html;

    // Find the card by data-mvid button and append panel
    var btn=document.querySelector('[data-mvid="'+memoriaValidadaId+'"]');
    if(btn){
      btn.closest('.item-card').appendChild(panel);
    }
  }).catch(function(e){alert('Error: '+e);});
}

function delMemExt(id, memoriaValidadaId){
  if(!confirm('Borrar esta memoria extendida?'))return;
  fetch('/api/memoria-extendida/'+id,{method:'DELETE'}).then(function(){
    var panel=document.getElementById('me-panel-'+memoriaValidadaId);
    if(panel) panel.remove();
    verMemoriaExtendida(memoriaValidadaId);
  });
}

function guardarMEInline(memoriaValidadaId){
  var tit=document.getElementById('me-tit-'+memoriaValidadaId).value.trim()||'Sin título';
  var body=document.getElementById('me-body-'+memoriaValidadaId).value.trim();
  if(!body){alert('El contenido no puede estar vacío.');return;}
  fetch('/api/memoria-extendida',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({titulo:tit,contenido:body,memoria_validada_id:memoriaValidadaId,tipo:'usuario'})
  }).then(r=>r.json()).then(function(d){
    if(d.ok){
      // Refresh the panel
      document.getElementById('me-panel-'+memoriaValidadaId).remove();
      verMemoriaExtendida(memoriaValidadaId,titulo);
    }
  });
}

function editMEInline(id){
  var el=document.getElementById('mep-'+id);
  if(!el)return;
  var orig=el.textContent;
  el.innerHTML='<textarea style="width:100%;border:2px solid #cc0000;border-radius:6px;padding:8px;font-size:13px;font-family:inherit;resize:vertical;min-height:80px">'+escH(orig)+'</textarea>';
  el.innerHTML+='<button onclick="saveMEInline('+id+',this)" style="margin-top:4px;font-size:10px;padding:3px 10px;background:#cc0000;color:#fff;border:none;cursor:pointer;font-family:monospace;border-radius:3px">Guardar</button>';
}

function saveMEInline(id,btn){
  var el=document.getElementById('mep-'+id);
  var txt=el.querySelector('textarea').value.trim();
  if(!txt)return;
  fetch('/api/memoria-extendida/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({contenido:txt})})
  .then(function(){el.innerHTML=escH(txt);});
}

function nuevaMemoriaExtendida(memoriaValidadaId, titulo){
  var tit=prompt('Título de esta memoria extendida:', titulo||'');
  if(tit===null)return;
  var contenido=prompt('Contenido (puedes editarlo después):','');
  if(contenido===null)return;
  fetch('/api/memoria-extendida',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({titulo:tit,contenido:contenido,memoria_validada_id:memoriaValidadaId||null,tipo:'usuario'})
  }).then(r=>r.json()).then(function(d){
    if(d.ok){showPage('memoria_anni');setTimeout(function(){document.querySelectorAll('.mem-tab').forEach(function(t){if(t.dataset.tab==='extendida')t.click();});},100);}
  });
}

function editMemoriaExtendida(id,btn){
  var card=btn.closest('.item-card');
  var el=card.querySelector('[id="me-'+id+'"]');
  if(!el)return;
  var orig=el.textContent;
  el.innerHTML='<textarea style="width:100%;border:2px solid #cc0000;border-radius:8px;padding:10px;font-size:14px;font-family:inherit;resize:vertical;min-height:120px">'+escH(orig)+'</textarea>';
  btn.textContent='Guardar';
  btn.onclick=function(){
    var txt=el.querySelector('textarea').value.trim();
    if(!txt)return;
    fetch('/api/memoria-extendida/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({contenido:txt})})
    .then(function(){renderTab('extendida');});
  };
}

function delMemoriaExtendida(id){
  if(!confirm('Borrar esta memoria extendida?'))return;
  fetch('/api/memoria-extendida/'+id,{method:'DELETE'}).then(function(){renderTab('extendida');});
}

function delHito(id){
if(!confirm('Borrar este hito?'))return;
fetch('/api/hitos/'+id,{method:'DELETE'}).then(()=>loadMVPage());}

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


function forceTick(){
  fetch('/api/curiosa/tick',{method:'POST'}).then(r=>r.json()).then(function(d){
    if(d.ok){setTimeout(function(){loadMundo(1);},4000);}
  });
}

function loadMundo(page){
var body=document.getElementById('page-body');
body.innerHTML='<p style="color:#999;padding:20px">Cargando...</p>';

// Estado del ciclo activo
fetch('/api/mundo/estado').then(r=>r.json()).then(est=>{
  var estadoEl=document.createElement('div');
  estadoEl.style.cssText='background:#f9f9f9;border:1px solid #e8e8e8;border-radius:10px;padding:14px 18px;margin-bottom:20px;font-size:13px;color:#555';
  if(est.activo){
    estadoEl.innerHTML='<span style="color:#cc0000;font-weight:900">● </span>Explorando: <b>'+escH(est.dominio)+'</b> — '+escH(est.subtema)+
      '<span style="float:right;color:#aaa">P'+est.pulso+'/12 · siguiente en ~'+est.mins_siguiente+'min &nbsp;<button onclick="forceTick()" style="font-size:9px;padding:2px 6px;background:none;border:1px solid #ccc;cursor:pointer;font-family:monospace">↻</button></span>';
  } else {
    estadoEl.innerHTML='<span style="color:#aaa;font-weight:900">○ </span>Sin ciclo activo — el siguiente arrancará pronto.';
  }
  body.innerHTML='';
  body.appendChild(estadoEl);

  // Lista de ciclos completados
  fetch('/api/mundo?page='+(page||1)).then(r=>r.json()).then(d=>{
    if(!d.ciclos.length){
      var p=document.createElement('p');
      p.style.cssText='color:#bbb;font-size:13px;font-style:italic;padding:8px 0';
      p.textContent='ANNI todavía no ha completado ningún ciclo. Vuelve en unas horas.';
      body.appendChild(p);
      return;
    }
    d.ciclos.forEach(function(c){
      var card=document.createElement('div');
      card.className='item-card';
      var fuentes_html='';
      if(c.fuentes){
        var urls=c.fuentes.split(',').filter(function(u){return u.trim();});
        if(urls.length){
          fuentes_html='<div style="margin-top:10px;font-size:12px;color:#aaa">'+
            urls.map(function(u){return '<a href="https://'+u.trim()+'" target="_blank" style="color:#cc0000;text-decoration:none">'+u.trim()+'</a>';}).join(' · ')+'</div>';
        }
      }
      card.innerHTML=
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">'+
          '<span style="font-size:11px;background:#f0f0f0;border-radius:4px;padding:2px 8px;font-weight:700;color:#666">'+escH(c.dominio)+'</span>'+
          '<span style="font-size:12px;color:#aaa">'+c.ts+'</span>'+
        '</div>'+
        '<div style="font-size:16px;font-weight:900;color:#111;margin-bottom:10px">'+escH(c.subtema)+'</div>'+
        '<div style="font-size:14px;color:#333;line-height:1.6;white-space:pre-wrap">'+escH(c.conclusion)+'</div>'+
        (c.pregunta_abierta?'<div style="margin-top:12px;padding:10px 14px;background:#fff5f5;border-left:3px solid #cc0000;font-size:13px;color:#555;font-style:italic">'+escH(c.pregunta_abierta)+'</div>':'')+
        fuentes_html;
      body.appendChild(card);
    });
    if(d.pages>1) body.appendChild(pagerEl(d.pages, page||1, 'loadMundo'));
  });
});
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
'<button class="btn-edit" data-fecha="'+e.fecha+'" data-id="'+e.id+'" onclick="editDiario(this.dataset.id,this.dataset.fecha)">Editar</button>'+
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


# ── BACKGROUND SCHEDULER ─────────────────────────────────────────────────────

def ciclo_loop():
    """Loop principal de ciclos CURIOSA — igual que ANI antigua.
    Ejecuta un pulso completo y duerme 20 minutos. Sin complicaciones."""
    import time as time_mod
    import json as json_mod
    print("[ANNI] ciclo_loop arrancado")
    time_mod.sleep(30)  # espera inicial

    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT id FROM usuarios")
            usuarios = [r[0] for r in c.fetchall()]
            conn.close()

            for uid in usuarios:
                try:
                    # Si no hay ciclo activo, arrancar uno nuevo
                    conn2 = sqlite3.connect(DB_PATH)
                    c2 = conn2.cursor()
                    c2.execute("SELECT id, dominio, subtema, pulso_actual, pulsos FROM ciclos_curiosa WHERE usuario_id=? AND estado='en_curso' LIMIT 1", (uid,))
                    ciclo = c2.fetchone()
                    conn2.close()

                    if not ciclo:
                        # Arrancar ciclo nuevo inmediatamente
                        dominio = get_siguiente_dominio(uid)
                        conn4 = sqlite3.connect(DB_PATH)
                        c4 = conn4.cursor()
                        c4.execute("SELECT fuentes FROM dominios_curiosa WHERE nombre=?", (dominio,))
                        row4 = c4.fetchone()
                        conn4.close()
                        fuentes = row4[0] if row4 else ""
                        anteriores = get_subtemas_anteriores(uid, dominio)
                        subtema = generar_subtema(dominio, fuentes, anteriores)
                        if subtema:
                            conn5 = sqlite3.connect(DB_PATH)
                            conn5.execute("INSERT INTO ciclos_curiosa (usuario_id, dominio, subtema, estado, pulso_actual, ts_inicio) VALUES (?,?,?,'en_curso',0,?)",
                                         (uid, dominio, subtema, time_mod.time()))
                            conn5.commit()
                            conn5.close()
                            print(f"[CICLO] Nuevo: {dominio} — {subtema[:50]}")
                        continue

                    ciclo_id, dominio, subtema, pulso_actual, pulsos_json = ciclo
                    siguiente_pulso = pulso_actual + 1

                    if siguiente_pulso > 12:
                        # Marcar como completado
                        conn6 = sqlite3.connect(DB_PATH)
                        conn6.execute("UPDATE ciclos_curiosa SET estado='completado', ts_fin=? WHERE id=?",
                                     (time_mod.time(), ciclo_id))
                        conn6.commit()
                        conn6.close()
                        print(f"[CICLO] Completado: {dominio}")
                        continue

                    # Ejecutar pulso
                    conn7 = sqlite3.connect(DB_PATH)
                    c7 = conn7.cursor()
                    c7.execute("SELECT fuentes FROM dominios_curiosa WHERE nombre=?", (dominio,))
                    row7 = c7.fetchone()
                    conn7.close()
                    fuentes = row7[0] if row7 else ""
                    pulsos = json_mod.loads(pulsos_json) if pulsos_json else {}

                    print(f"[CICLO] P{siguiente_pulso}/12 — {dominio}: {subtema[:40]}")
                    resultado = ejecutar_pulso_curiosa(uid, ciclo_id, dominio, subtema, fuentes, siguiente_pulso, pulsos)

                    if resultado:
                        pulsos[str(siguiente_pulso)] = resultado
                        conn8 = sqlite3.connect(DB_PATH)
                        c8 = conn8.cursor()
                        if siguiente_pulso == 12:
                            pregunta = pulsos.get("11", "")
                            try:
                                resp_emb = together.embeddings.create(model=EMBED_MODEL, input=[resultado[:1600]])
                                vec = resp_emb.data[0].embedding
                                import struct as struct_mod
                                blob = struct_mod.pack(f"{len(vec)}f", *vec)
                            except:
                                blob = None
                            c8.execute("UPDATE ciclos_curiosa SET pulso_actual=12, pulsos=?, conclusion=?, pregunta_abierta=?, estado='completado', embedding=?, ts_fin=?, ts_ultimo_pulso=? WHERE id=?",
                                      (json_mod.dumps(pulsos), resultado, pregunta, blob, time_mod.time(), time_mod.time(), ciclo_id))
                            print(f"[CICLO] ✓ Completado: {dominio} — {subtema[:40]}")
                        else:
                            c8.execute("UPDATE ciclos_curiosa SET pulso_actual=?, pulsos=?, ts_ultimo_pulso=? WHERE id=?",
                                      (siguiente_pulso, json_mod.dumps(pulsos), time_mod.time(), ciclo_id))
                        conn8.commit()
                        conn8.close()

                except Exception as e:
                    print(f"[CICLO] Error usuario {uid}: {e}")

        except Exception as e:
            print(f"[CICLO] Error general: {e}")

        time_mod.sleep(1200)  # 20 minutos — igual que el diseño original



if __name__ == '__main__':
    init_db()
    print(f"\n{'='*50}")
    print(f"  ANNI {ANNI_VERSION}")
    print(f"  {ANNI_CREDITS}")
    print(f"  DB: {DB_PATH}")
    print(f"{'='*50}\n")
    threading.Thread(target=ciclo_loop, daemon=True).start()
    print('[ANNI] ciclo_loop arrancado')
    # Generar embeddings pendientes de memoria_extendida
    try:
        conn_s = sqlite3.connect(DB_PATH)
        uids = [r[0] for r in conn_s.execute("SELECT id FROM usuarios").fetchall()]
        conn_s.close()
        for uid in uids:
            seed_embeddings_memoria_extendida(uid)
    except Exception as e:
        print(f"[ANNI] Error seed inicial: {e}")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
