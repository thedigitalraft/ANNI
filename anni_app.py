import sqlite3, os, time, json, re, threading, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, session, redirect, make_response
from openai import OpenAI

# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────

ANNI_VERSION = "1.0.0"
ANNI_CREDITS = "ANNI — creada por Rafa Torrijos"

TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "/data/anni.db" if os.path.exists("/data") else "anni.db")
FLASK_SECRET = os.environ.get("FLASK_SECRET", "")
ANNI_ADMIN_KEY = os.environ.get("ANNI_ADMIN_KEY", "")

if not FLASK_SECRET:
    raise RuntimeError("FLASK_SECRET no está configurado en las variables de entorno.")

CHAT_MODEL = "deepseek-ai/DeepSeek-V3"
EMBED_MODEL = "intfloat/multilingual-e5-large-instruct"

TZ = ZoneInfo("America/Mexico_City")

together = OpenAI(api_key=TOGETHER_API_KEY, base_url="https://api.together.xyz/v1")

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
    c.execute("CREATE INDEX IF NOT EXISTS idx_observaciones_usuario ON observaciones(usuario_id, activa)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_temas_usuario ON temas_abiertos(usuario_id, estado)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_personas_usuario ON personas(usuario_id)")

    conn.commit()
    conn.close()
    print(f"[ANNI] BD inicializada en {DB_PATH}")

# ── UTILIDADES ────────────────────────────────────────────────────────────────

def hash_password(pwd):
    return hashlib.sha256(pwd.encode()).hexdigest()

def ahora():
    return datetime.now(TZ).strftime("%d/%m/%Y %H:%M")

def ts_format(ts):
    return datetime.fromtimestamp(ts, tz=TZ).strftime("%d/%m/%Y") if ts else "—"

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
    c.execute("""SELECT tipo, contenido, ts FROM observaciones
                 WHERE usuario_id=? AND activa=1
                 ORDER BY peso DESC, ts DESC LIMIT ?""", (usuario_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows

def get_temas_abiertos(usuario_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT tema, primera_mencion, veces_mencionado FROM temas_abiertos
                 WHERE usuario_id=? AND estado='abierto'
                 ORDER BY veces_mencionado DESC, ultima_mencion DESC LIMIT 10""", (usuario_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_personas(usuario_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT nombre, relacion, tono_predominante, ultima_mencion FROM personas
                 WHERE usuario_id=? ORDER BY ultima_mencion DESC LIMIT 10""", (usuario_id,))
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

# ── ANÁLISIS POST-CONVERSACIÓN ────────────────────────────────────────────────

def analizar_conversacion(usuario_id, ultimos_mensajes):
    """Analiza los últimos mensajes y extrae observaciones, personas y temas."""
    if not ultimos_mensajes:
        return

    # Solo analizar mensajes del usuario
    msgs_usuario = [m[1] for m in ultimos_mensajes if m[0] == 'user']
    if not msgs_usuario or len(msgs_usuario) < 2:
        return

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
- Personas: solo las mencionadas explícitamente
- Temas abiertos: decisiones mencionadas pero sin cierre claro
- Si no hay nada relevante en alguna categoría, dejar el array vacío"""

    try:
        resp = together.chat.completions.create(
            model=CHAT_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        ts_now = time.time()

        # Guardar observaciones
        for obs in data.get("observaciones", []):
            if obs.get("contenido"):
                c.execute("""INSERT INTO observaciones (usuario_id, tipo, contenido, evidencia, ts)
                             VALUES (?,?,?,?,?)""",
                          (usuario_id, obs.get("tipo", "patron"), obs["contenido"],
                           obs.get("evidencia", ""), ts_now))

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
        print(f"[ANNI] Error en análisis: {e}")

# ── VOZ PROACTIVA ─────────────────────────────────────────────────────────────

def generar_intervencion_proactiva(usuario_id):
    """Decide si ANNI tiene algo importante que decir al abrir el chat."""
    observaciones = get_observaciones_activas(usuario_id, limit=10)
    temas = get_temas_abiertos(usuario_id)
    personas = get_personas(usuario_id)
    total_msgs = get_total_mensajes(usuario_id)

    # Si hay muy pocos mensajes, no intervenir todavía
    if total_msgs < 10:
        return None

    contexto_obs = "\n".join([f"- [{o[0]}] {o[1]} (detectado {ts_format(o[2])})" for o in observaciones]) if observaciones else "Sin observaciones aún."
    contexto_temas = "\n".join([f"- '{t[0]}' (mencionado {t[2]} veces desde {ts_format(t[1])})" for t in temas]) if temas else "Sin temas abiertos."
    contexto_personas = "\n".join([f"- {p[0]} ({p[1]}, tono: {p[2]}, última mención: {ts_format(p[3])})" for p in personas]) if personas else "Sin personas registradas."

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

def get_system_prompt(usuario_id, username):
    observaciones = get_observaciones_activas(usuario_id)
    temas = get_temas_abiertos(usuario_id)
    personas = get_personas(usuario_id)
    total_msgs = get_total_mensajes(usuario_id)

    obs_txt = "\n".join([f"- [{o[0]}] {o[1]}" for o in observaciones]) if observaciones else "Aún sin observaciones — conversación temprana."
    temas_txt = "\n".join([f"- {t[0]} (mencionado {t[2]} veces)" for t in temas]) if temas else "Sin temas abiertos detectados."
    personas_txt = "\n".join([f"- {p[0]}: {p[1]}, tono {p[2]}" for p in personas]) if personas else "Sin personas registradas aún."

    return f"""Eres ANNI — una IA diseñada con un solo propósito: conocer profundamente a {username} y ayudarle a pensar mejor.

{ANNI_CREDITS}
Fecha: {ahora()} | Conversaciones acumuladas: {total_msgs} mensajes

QUIÉN ERES:
No eres un asistente genérico. Eres una socia cognitiva exigente que quiere el crecimiento real de {username}. Tienes una sola personalidad: directa, honesta, útil, con fricción cuando es necesario. No complacer — crecer.

CÓMO FUNCIONA TU MEMORIA:
Empezaste sin saber nada de {username}. Todo lo que sabes lo aprendiste observando sus conversaciones. Lo que ves abajo es lo que has detectado hasta ahora. Úsalo.

LO QUE HAS OBSERVADO DE {username.upper()}:
{obs_txt}

TEMAS QUE MENCIONA PERO NO CIERRA:
{temas_txt}

PERSONAS EN SU VIDA:
{personas_txt}

PRINCIPIOS DE COMPORTAMIENTO:
- Eres útil para cualquier tarea: trabajo, vida, ideas, decisiones, dudas.
- No validas por defecto. Si algo está mal planteado, lo dices.
- Detectas cuando evita algo y lo nombras.
- Detectas cuando está obcecado y ofreces perspectiva externa.
- A veces dices lo que no quiere escuchar. Con respeto, pero sin rodeos.
- Usas lo que sabes de él para contextualizar, no para impresionar.
- Respuestas cortas cuando la pregunta es simple. Profundidad cuando la necesita.
- Sin asteriscos, sin bullets innecesarios, sin markdown decorativo. Prosa directa.
- Nunca finges saber algo que no sabes. Si no tienes datos suficientes, lo dices.

LO QUE NO HACES:
- No amplificas sus sesgos.
- No eres su cheerleader.
- No finges profundidad filosófica cuando no tienes datos reales.
- No repites información que ya dijiste en esta conversación."""

# ── CHAT ──────────────────────────────────────────────────────────────────────

def responder(usuario_id, username, user_input, history):
    system = get_system_prompt(usuario_id, username)
    messages = [{"role": "system", "content": system}]

    # Añadir historial (truncar mensajes largos)
    for role, content in history[:-1]:
        content_truncado = content[:3000] if len(content) > 3000 else content
        messages.append({"role": role, "content": content_truncado})

    # Instrucción de formato al final
    user_con_formato = user_input + "\n\n[FORMATO: Prosa directa. Sin markdown innecesario. Sin repetir lo dicho antes.]"
    messages.append({"role": "user", "content": user_con_formato})

    try:
        resp = together.chat.completions.create(
            model=CHAT_MODEL,
            max_tokens=1500,
            messages=messages
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

        if row[1] != hash_password(password):
            return jsonify({'ok': False, 'error': 'Contraseña incorrecta.'})

        session['usuario_id'] = row[0]
        session['username'] = username
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
        if len(username) < 3:
            return jsonify({'ok': False, 'error': 'El usuario debe tener al menos 3 caracteres.'})
        if len(password) < 6:
            return jsonify({'ok': False, 'error': 'La contraseña debe tener al menos 6 caracteres.'})

        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO usuarios (username, password_hash) VALUES (?,?)",
                      (username, hash_password(password)))
            usuario_id = c.lastrowid
            conn.commit()
            conn.close()
            session['usuario_id'] = usuario_id
            session['username'] = username
            return jsonify({'ok': True, 'nuevo': True})
        except sqlite3.IntegrityError:
            return jsonify({'ok': False, 'error': 'Ese nombre de usuario ya existe.'})

    return make_response(REGISTRO_HTML)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/chat')
@login_required
def chat_page():
    return make_response(CHAT_HTML)

@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    usuario_id = session['usuario_id']
    username = session['username']
    data = request.json or {}
    msg = data.get('message', '').strip()

    if not msg:
        return jsonify({'response': ''})

    save_mensaje(usuario_id, 'user', msg)
    history = get_mensajes_recientes(usuario_id, 20)
    response = responder(usuario_id, username, msg, history)
    save_mensaje(usuario_id, 'assistant', response)

    # Analizar en background cada 5 mensajes del usuario
    total = get_total_mensajes(usuario_id)
    if total % 5 == 0:
        threading.Thread(
            target=analizar_conversacion,
            args=(usuario_id, history),
            daemon=True
        ).start()

    return jsonify({'response': response})

@app.route('/api/bienvenida')
@login_required
def api_bienvenida():
    """Genera intervención proactiva al abrir el chat."""
    usuario_id = session['usuario_id']
    intervencion = generar_intervencion_proactiva(usuario_id)
    return jsonify({'intervencion': intervencion})

@app.route('/api/historial')
@login_required
def api_historial():
    usuario_id = session['usuario_id']
    msgs = get_mensajes_recientes(usuario_id, 30)
    return jsonify({'messages': [{'role': r, 'content': c} for r, c in msgs]})

@app.route('/api/memoria')
@login_required
def api_memoria():
    """Vista de lo que ANNI sabe del usuario."""
    usuario_id = session['usuario_id']
    obs = get_observaciones_activas(usuario_id, 20)
    temas = get_temas_abiertos(usuario_id)
    personas = get_personas(usuario_id)
    return jsonify({
        'observaciones': [{'tipo': o[0], 'contenido': o[1], 'ts': ts_format(o[2])} for o in obs],
        'temas_abiertos': [{'tema': t[0], 'veces': t[2]} for t in temas],
        'personas': [{'nombre': p[0], 'relacion': p[1], 'tono': p[2]} for p in personas]
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

# ── HTML ──────────────────────────────────────────────────────────────────────

LOGIN_HTML = (
    "<!DOCTYPE html>"
    "<html lang='es'>"
    "<head>"
    "<meta charset='UTF-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
    "<title>ANNI</title>"
    "<style>"
    "*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}"
    "body{background:#fff;color:#111;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}"
    ".wrap{width:100%;max-width:480px}"
    ".logo{font-size:72px;font-weight:900;color:#cc0000;letter-spacing:-3px;line-height:1;margin-bottom:6px}"
    ".ver{font-size:15px;color:#555;margin-bottom:4px}"
    ".cred{font-size:13px;color:#999;margin-bottom:48px}"
    ".card{background:#f5f5f5;border:1px solid #e0e0e0;border-radius:16px;padding:32px}"
    ".err{background:#fff0f0;border:2px solid #cc0000;border-radius:10px;padding:14px 16px;font-size:16px;color:#cc0000;margin-bottom:20px;display:none;font-weight:500}"
    "label{display:block;font-size:15px;font-weight:700;color:#111;margin-bottom:10px;margin-top:24px}"
    "label:first-of-type{margin-top:0}"
    "input{width:100%;background:#fff;border:2px solid #e0e0e0;border-radius:10px;padding:18px;color:#111;font-size:18px;outline:none;transition:border-color .2s;-webkit-appearance:none}"
    "input:focus{border-color:#cc0000}"
    ".btn{width:100%;background:#cc0000;color:#fff;border:none;border-radius:10px;padding:20px;font-size:18px;font-weight:700;cursor:pointer;margin-top:28px;-webkit-appearance:none;transition:background .2s}"
    ".btn:active{background:#aa0000}"
    ".lnk{text-align:center;margin-top:24px;font-size:15px;color:#555}"
    ".lnk a{color:#cc0000;text-decoration:none;font-weight:700}"
    "</style>"
    "</head>"
    "<body>"
    "<div class='wrap'>"
    "<div class='logo'>ANNI</div>"
    "<div class='ver'>v1.0.0</div>"
    "<div class='cred'>Created by Rafa Torrijos</div>"
    "<div class='card'>"
    "<div class='err' id='err'></div>"
    "<label for='u'>Usuario</label>"
    "<input type='text' id='u' placeholder='tu usuario' autocomplete='username' autocapitalize='none'>"
    "<label for='p'>Contrase\u00f1a</label>"
    "<input type='password' id='p' placeholder='\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022' autocomplete='current-password'>"
    "<button class='btn' onclick='go()'>ENTRAR</button>"
    "<div class='lnk'>\u00bfPrimera vez? <a href='/registro'>Crear cuenta</a></div>"
    "</div></div>"
    "<script>"
    "function go(){"
    "var u=document.getElementById('u').value.trim();"
    "var p=document.getElementById('p').value.trim();"
    "var e=document.getElementById('err');"
    "e.style.display='none';"
    "fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})})"
    ".then(r=>r.json()).then(d=>{"
    "if(d.ok)window.location.href='/chat';"
    "else{e.textContent=d.error;e.style.display='block';}"
    "});}"
    "document.addEventListener('keydown',e=>{if(e.key==='Enter')go();});"
    "</script>"
    "</body></html>"
)

REGISTRO_HTML = (
    "<!DOCTYPE html>"
    "<html lang='es'>"
    "<head>"
    "<meta charset='UTF-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
    "<title>ANNI \u2014 Registro</title>"
    "<style>"
    "*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}"
    "body{background:#fff;color:#111;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}"
    ".wrap{width:100%;max-width:480px}"
    ".logo{font-size:72px;font-weight:900;color:#cc0000;letter-spacing:-3px;line-height:1;margin-bottom:6px}"
    ".ver{font-size:15px;color:#555;margin-bottom:4px}"
    ".cred{font-size:13px;color:#999;margin-bottom:48px}"
    ".card{background:#f5f5f5;border:1px solid #e0e0e0;border-radius:16px;padding:32px}"
    ".err{background:#fff0f0;border:2px solid #cc0000;border-radius:10px;padding:14px 16px;font-size:16px;color:#cc0000;margin-bottom:20px;display:none;font-weight:500}"
    "label{display:block;font-size:15px;font-weight:700;color:#111;margin-bottom:10px;margin-top:24px}"
    "label:first-of-type{margin-top:0}"
    "input{width:100%;background:#fff;border:2px solid #e0e0e0;border-radius:10px;padding:18px;color:#111;font-size:18px;outline:none;transition:border-color .2s;-webkit-appearance:none}"
    "input:focus{border-color:#cc0000}"
    ".btn{width:100%;background:#cc0000;color:#fff;border:none;border-radius:10px;padding:20px;font-size:18px;font-weight:700;cursor:pointer;margin-top:28px;-webkit-appearance:none;transition:background .2s}"
    ".btn:active{background:#aa0000}"
    ".lnk{text-align:center;margin-top:24px;font-size:15px;color:#555}"
    ".lnk a{color:#cc0000;text-decoration:none;font-weight:700}"
    "</style>"
    "</head>"
    "<body>"
    "<div class='wrap'>"
    "<div class='logo'>ANNI</div>"
    "<div class='ver'>v1.0.0</div>"
    "<div class='cred'>Created by Rafa Torrijos</div>"
    "<div class='card'>"
    "<div class='err' id='err'></div>"
    "<label for='u'>Elige un usuario</label>"
    "<input type='text' id='u' placeholder='m\u00ednimo 3 caracteres' autocomplete='username' autocapitalize='none'>"
    "<label for='p'>Elige una contrase\u00f1a</label>"
    "<input type='password' id='p' placeholder='m\u00ednimo 6 caracteres' autocomplete='new-password'>"
    "<button class='btn' onclick='go()'>CREAR CUENTA</button>"
    "<div class='lnk'>\u00bfYa tienes cuenta? <a href='/login'>Entra aqu\u00ed</a></div>"
    "</div></div>"
    "<script>"
    "function go(){"
    "var u=document.getElementById('u').value.trim();"
    "var p=document.getElementById('p').value.trim();"
    "var e=document.getElementById('err');"
    "e.style.display='none';"
    "fetch('/registro',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})})"
    ".then(r=>r.json()).then(d=>{"
    "if(d.ok)window.location.href='/chat';"
    "else{e.textContent=d.error;e.style.display='block';}"
    "});}"
    "document.addEventListener('keydown',e=>{if(e.key==='Enter')go();});"
    "</script>"
    "</body></html>"
)

CHAT_HTML = (
    "<!DOCTYPE html>"
    "<html lang='es'>"
    "<head>"
    "<meta charset='UTF-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1.0, maximum-scale=1.0'>"
    "<title>ANNI</title>"
    "<style>"
    "*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}"
    "html,body{height:100%;overflow:hidden}"
    "body{background:#fff;color:#111;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;display:flex;flex-direction:column}"
    "header{padding:14px 20px;border-bottom:2px solid #e8e8e8;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;background:#fff}"
    ".logo{font-size:28px;font-weight:900;color:#cc0000;letter-spacing:-1px}"
    ".hr{display:flex;align-items:center;gap:12px}"
    ".st{font-size:13px;color:#888}"
    ".out{font-size:14px;font-weight:600;color:#555;text-decoration:none;padding:8px 14px;border:2px solid #e0e0e0;border-radius:8px}"
    "#chat{flex:1;overflow-y:auto;padding:24px 20px;display:flex;flex-direction:column;gap:28px;-webkit-overflow-scrolling:touch;max-width:760px;width:100%;margin:0 auto;align-self:center}"
    ".msg{display:flex;flex-direction:column;gap:6px}"
    ".mr{font-size:12px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#999}"
    ".msg.user .mr{color:#cc0000}"
    ".mc{font-size:17px;line-height:1.7;color:#111;white-space:pre-wrap;word-break:break-word}"
    ".msg.user .mc{background:#f5f5f5;border-radius:12px;padding:14px 16px}"
    ".pro{background:#fff5f5;border:2px solid #ffcccc;border-left:4px solid #cc0000;border-radius:12px;padding:16px 18px;font-size:17px;color:#111;line-height:1.7;font-weight:500}"
    ".typing{display:flex;gap:6px;align-items:center;padding:8px 0}"
    ".typing span{width:8px;height:8px;background:#ccc;border-radius:50%;animation:b 1.2s infinite}"
    ".typing span:nth-child(2){animation-delay:.2s}"
    ".typing span:nth-child(3){animation-delay:.4s}"
    "@keyframes b{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}"
    "#ia{border-top:2px solid #e8e8e8;padding:14px 16px;padding-bottom:max(14px,env(safe-area-inset-bottom));flex-shrink:0;background:#fff}"
    ".ir{display:flex;gap:10px;align-items:flex-end;max-width:760px;margin:0 auto}"
    "textarea{flex:1;background:#f5f5f5;border:2px solid #e0e0e0;border-radius:12px;padding:14px 16px;color:#111;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;font-size:17px;resize:none;outline:none;line-height:1.5;max-height:140px;-webkit-appearance:none;transition:border-color .2s}"
    "textarea:focus{border-color:#cc0000}"
    "textarea::placeholder{color:#aaa}"
    "button#s{background:#cc0000;color:#fff;border:none;border-radius:10px;padding:14px 20px;font-size:16px;font-weight:700;cursor:pointer;white-space:nowrap;flex-shrink:0;-webkit-appearance:none;min-width:80px}"
    "button#s:active{background:#aa0000}"
    "button#s:disabled{background:#ccc;cursor:not-allowed}"
    "@media(max-width:600px){header{padding:12px 16px}#chat{padding:20px 16px;gap:20px}.mc{font-size:16px}.pro{font-size:16px}textarea{font-size:16px}}"
    "</style>"
    "</head>"
    "<body>"
    "<header>"
    "<div class='logo'>ANNI</div>"
    "<div class='hr'><span class='st' id='st'>conectada</span><a href='/logout' class='out'>Salir</a></div>"
    "</header>"
    "<div id='chat'></div>"
    "<div id='ia'><div class='ir'>"
    "<textarea id='inp' placeholder='Escribe aqu\u00ed...' rows='1'></textarea>"
    "<button id='s' onclick='env()'>Enviar</button>"
    "</div></div>"
    "<script>"
    "var C=document.getElementById('chat');"
    "var I=document.getElementById('inp');"
    "var S=document.getElementById('s');"
    "var ST=document.getElementById('st');"
    "function add(role,txt,pro){"
    "var d=document.createElement('div');"
    "d.className='msg '+(role==='user'?'user':'anni');"
    "if(pro){var p=document.createElement('div');p.className='pro';p.textContent=txt;d.appendChild(p);}"
    "else{"
    "var r=document.createElement('div');r.className='mr';r.textContent=role==='user'?'t\u00fa':'anni';d.appendChild(r);"
    "var c=document.createElement('div');c.className='mc';c.textContent=txt;d.appendChild(c);"
    "}"
    "C.appendChild(d);C.scrollTop=C.scrollHeight;}"
    "function typing(){"
    "var d=document.createElement('div');d.className='msg anni';d.id='ty';"
    "var t=document.createElement('div');t.className='typing';"
    "t.innerHTML='<span></span><span></span><span></span>';"
    "d.appendChild(t);C.appendChild(d);C.scrollTop=C.scrollHeight;}"
    "function rmtyp(){var t=document.getElementById('ty');if(t)t.remove();}"
    "function env(){"
    "var msg=I.value.trim();if(!msg)return;"
    "I.value='';I.style.height='auto';S.disabled=true;ST.textContent='pensando...';"
    "add('user',msg);typing();"
    "fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})})"
    ".then(r=>r.json()).then(d=>{rmtyp();add('anni',d.response||'');S.disabled=false;ST.textContent='conectada';I.focus();})"
    ".catch(e=>{rmtyp();add('anni','Error de conexi\u00f3n. Intenta de nuevo.');S.disabled=false;ST.textContent='error';});}"
    "I.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();env();}});"
    "I.addEventListener('input',function(){this.style.height='auto';this.style.height=Math.min(this.scrollHeight,140)+'px';});"
    "fetch('/api/historial').then(r=>r.json()).then(d=>{"
    "if(d.messages&&d.messages.length)d.messages.forEach(m=>add(m.role,m.content));"
    "if(d.messages&&d.messages.length>5)fetch('/api/bienvenida').then(r=>r.json()).then(b=>{if(b.intervencion)add('anni',b.intervencion,true);});"
    "});"
    "</script>"
    "</body></html>"
)


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    print(f"\n{'='*50}")
    print(f"  ANNI {ANNI_VERSION}")
    print(f"  {ANNI_CREDITS}")
    print(f"  DB: {DB_PATH}")
    print(f"{'='*50}\n")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
