# -*- coding: utf-8 -*-
"""
PetFeeder IoT v4
- MySQL/MariaDB (base de datos real)
- DHT11 temperatura/humedad
- HX711 peso via Arduino USB
- PCA9685 + Servo MG995
- BI con calculo de ahorro economico
"""
import os, hashlib, hmac, time, threading, json, base64, re
import smtplib, secrets
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
try:
    import serial as pyserial
except ImportError:
    pyserial = None
import pymysql
import pymysql.cursors
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, g

BASE_DIR   = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
JWT_SECRET = os.environ.get("JWT_SECRET", "petfeeder_v4_2026")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASS = os.environ.get("GMAIL_PASS", "")
APP_URL    = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "dispensador-mascotas-production.up.railway.app")
TOKEN_DAYS = 7

# CONFIG MySQL
DB_CONFIG = {
    "host":     os.environ.get("MYSQL_HOST") or os.environ.get("MYSQLHOST", "localhost"),
    "user":     os.environ.get("MYSQLUSER", "petfeeder"),
    "password": os.environ.get("MYSQLPASSWORD", "pf2026secure"),
    "database": os.environ.get("MYSQL_DATABASE") or os.environ.get("MYSQLDATABASE", "petfeeder"),
    "port":     int(os.environ.get("MYSQLPORT", 3306)),
    "charset":  "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "autocommit": False,
}

app = Flask(__name__, static_folder=str(STATIC_DIR))
servo_state = {"estado": "cerrado", "angulo": 45}

# PCA9685 + SERVO
try:
    import board, busio
    from adafruit_pca9685 import PCA9685
    from adafruit_motor import servo as adafruit_servo
    i2c   = busio.I2C(board.SCL, board.SDA)
    pca   = PCA9685(i2c)
    pca.frequency = 50
    mg995 = adafruit_servo.Servo(pca.channels[0], min_pulse=500, max_pulse=2500, actuation_range=180)
    mg995.angle = 45
    HARDWARE_OK = True
    print("Servo OK")
except Exception as e:
    HARDWARE_OK = False; mg995 = None
    print(f"Servo sim ({e})")

# DHT11
try:
    import adafruit_dht, board as _board
    dht_sensor = adafruit_dht.DHT11(_board.D18)
    DHT_OK = True
    print("DHT11 OK")
except Exception as e:
    DHT_OK = False; dht_sensor = None
    print(f"DHT11 sim ({e})")

# PESO via Arduino
peso_serial   = None
peso_actual   = 0.0
peso_zero_ref = 0.0

try:
    peso_serial = pyserial.Serial('/dev/ttyUSB0', 9600, timeout=1)
    time.sleep(2)
    PESO_OK = True
    print("Arduino Serial OK")
except Exception as e:
    PESO_OK = False
    print(f"Peso sim ({e})")

def leer_peso_serial():
    if not peso_serial: return None
    try:
        l = peso_serial.readline().decode('utf-8', errors='ignore').strip()
        if l and l != 'LISTO': return float(l)
    except: pass
    return None

# CACHE SENSORES
sensor_cache = {
    "dht":  {"ok": False, "temperatura": None, "humedad": None},
    "peso": {"ok": False, "gramos": 0.0},
}
sensor_history = {"temperatura": [], "humedad": [], "peso": []}

def sensor_worker():
    global peso_actual
    while True:
        try:
            if DHT_OK and dht_sensor:
                t = dht_sensor.temperature
                h = dht_sensor.humidity
                if t is not None:
                    sensor_cache["dht"] = {"ok":True,"temperatura":t,"humedad":h}
                    sensor_history["temperatura"].append({"t":datetime.now().strftime("%H:%M"),"v":t})
                    sensor_history["humedad"].append({"t":datetime.now().strftime("%H:%M"),"v":h})
                    for k in ["temperatura","humedad"]:
                        if len(sensor_history[k]) > 60: sensor_history[k].pop(0)
                    if datetime.now().minute % 5 == 0 and datetime.now().second < 3:
                        try:
                            db = get_db_direct()
                            with db.cursor() as cur:
                                cur.execute("INSERT INTO lecturas_ambiente(temperatura,humedad) VALUES(%s,%s)",(t,h))
                            db.commit(); db.close()
                        except: pass
        except: pass
        try:
            raw = leer_peso_serial()
            if raw is not None:
                peso_actual = max(0.0, round(raw - peso_zero_ref, 1))
                sensor_cache["peso"] = {"ok":True,"gramos":peso_actual}
                sensor_history["peso"].append({"t":datetime.now().strftime("%H:%M:%S"),"v":peso_actual})
                if len(sensor_history["peso"]) > 60: sensor_history["peso"].pop(0)
        except: pass
        time.sleep(2)

threading.Thread(target=sensor_worker, daemon=True).start()

# DB
def get_db_direct():
    return pymysql.connect(**DB_CONFIG)

def get_db():
    if "db" not in g:
        g.db = pymysql.connect(**DB_CONFIG)
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db: db.close()

def query(sql, params=(), one=False, commit=False, lastid=False):
    db  = get_db()
    with db.cursor() as cur:
        cur.execute(sql, params)
        if commit:
            db.commit()
            return cur.lastrowid if lastid else True
        return cur.fetchone() if one else cur.fetchall()

def init_db():
    db = get_db_direct()
    with db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS usuarios (
            id INT AUTO_INCREMENT PRIMARY KEY,
            nombre VARCHAR(100) NOT NULL,
            email VARCHAR(150) NOT NULL UNIQUE,
            password_hash VARCHAR(64) NOT NULL,
            telefono VARCHAR(20),
            activo TINYINT DEFAULT 1,
            verificado TINYINT DEFAULT 0,
            token_verificacion VARCHAR(64),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS mascotas (
            id INT AUTO_INCREMENT PRIMARY KEY,
            usuario_id INT NOT NULL,
            nombre VARCHAR(100) NOT NULL,
            especie VARCHAR(50) DEFAULT 'perro',
            raza VARCHAR(100),
            fecha_nac DATE,
            peso_kg DECIMAL(5,2),
            notas TEXT,
            activo TINYINT DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS alimentos (
            id INT AUTO_INCREMENT PRIMARY KEY,
            mascota_id INT NOT NULL,
            marca VARCHAR(100),
            nombre_producto VARCHAR(150) NOT NULL,
            tipo VARCHAR(50) DEFAULT 'seco',
            tamano_croqueta VARCHAR(50),
            calorias_100g DECIMAL(6,2),
            FOREIGN KEY (mascota_id) REFERENCES mascotas(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS dispensadores (
            id INT AUTO_INCREMENT PRIMARY KEY,
            usuario_id INT NOT NULL,
            nombre VARCHAR(100) NOT NULL,
            mac_address VARCHAR(17) UNIQUE,
            modelo VARCHAR(100),
            capacidad_g DECIMAL(8,2),
            nivel_actual_g DECIMAL(8,2) DEFAULT 0,
            activo TINYINT DEFAULT 1,
            ultima_conexion DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS horarios (
            id INT AUTO_INCREMENT PRIMARY KEY,
            mascota_id INT NOT NULL,
            dispensador_id INT NOT NULL,
            nombre VARCHAR(100),
            hora TIME NOT NULL,
            porcion_g DECIMAL(6,2) NOT NULL,
            dias_semana VARCHAR(100) DEFAULT 'lunes,martes,miercoles,jueves,viernes,sabado,domingo',
            velocidad VARCHAR(20) DEFAULT 'normal',
            activo TINYINT DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (mascota_id) REFERENCES mascotas(id) ON DELETE CASCADE,
            FOREIGN KEY (dispensador_id) REFERENCES dispensadores(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS dispensaciones (
            id INT AUTO_INCREMENT PRIMARY KEY,
            horario_id INT,
            mascota_id INT NOT NULL,
            dispensador_id INT NOT NULL,
            gramos_programados DECIMAL(6,2),
            gramos_real DECIMAL(6,2),
            tipo VARCHAR(20) DEFAULT 'automatico',
            exitosa TINYINT DEFAULT 1,
            codigo_error VARCHAR(50),
            nota TEXT,
            temperatura DECIMAL(4,1),
            humedad DECIMAL(4,1),
            ejecutado_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (mascota_id) REFERENCES mascotas(id) ON DELETE CASCADE,
            FOREIGN KEY (dispensador_id) REFERENCES dispensadores(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS notificaciones (
            id INT AUTO_INCREMENT PRIMARY KEY,
            usuario_id INT NOT NULL,
            tipo VARCHAR(50) NOT NULL,
            titulo VARCHAR(200) NOT NULL,
            mensaje TEXT NOT NULL,
            leida TINYINT DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS lecturas_ambiente (
            id INT AUTO_INCREMENT PRIMARY KEY,
            temperatura DECIMAL(4,1),
            humedad DECIMAL(4,1),
            leido_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS config_bi (
            id INT AUTO_INCREMENT PRIMARY KEY,
            usuario_id INT NOT NULL,
            clave VARCHAR(50) NOT NULL,
            valor DECIMAL(10,2) NOT NULL,
            UNIQUE KEY uk_config(usuario_id, clave),
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        db.commit()
        cur.execute("SELECT COUNT(*) as c FROM usuarios")
        if cur.fetchone()["c"] == 0:
            h = hashlib.sha256("demo123".encode()).hexdigest()
            cur.execute("INSERT INTO usuarios(nombre,email,password_hash) VALUES(%s,%s,%s)",
                       ("Demo User","demo@petfeeder.com",h))
            uid = cur.lastrowid
            cur.execute("INSERT INTO mascotas(usuario_id,nombre,especie,raza,fecha_nac,peso_kg) VALUES(%s,%s,%s,%s,%s,%s)",
                       (uid,"Max","perro","Golden Retriever","2021-03-15",28.0))
            mid1 = cur.lastrowid
            cur.execute("INSERT INTO mascotas(usuario_id,nombre,especie,raza,fecha_nac,peso_kg) VALUES(%s,%s,%s,%s,%s,%s)",
                       (uid,"Luna","perro","Labrador","2019-07-22",22.5))
            mid2 = cur.lastrowid
            cur.execute("INSERT INTO mascotas(usuario_id,nombre,especie,raza,fecha_nac,peso_kg) VALUES(%s,%s,%s,%s,%s,%s)",
                       (uid,"Michi","gato","Siames","2022-01-10",4.5))
            mid3 = cur.lastrowid
            cur.execute("INSERT INTO alimentos(mascota_id,marca,nombre_producto,tipo) VALUES(%s,%s,%s,%s)",
                       (mid1,"Purina","Pro Plan Adulto","seco"))
            cur.execute("INSERT INTO alimentos(mascota_id,marca,nombre_producto,tipo) VALUES(%s,%s,%s,%s)",
                       (mid2,"Royal Canin","Labrador Adult","seco"))
            cur.execute("INSERT INTO alimentos(mascota_id,marca,nombre_producto,tipo) VALUES(%s,%s,%s,%s)",
                       (mid3,"Whiskas","Adult Indoor","humedo"))
            cur.execute("INSERT INTO dispensadores(usuario_id,nombre,modelo,capacidad_g,nivel_actual_g) VALUES(%s,%s,%s,%s,%s)",
                       (uid,"Dispensador Sala","PetFeeder Pro v2",5000.0,3200.0))
            did1 = cur.lastrowid
            cur.execute("INSERT INTO dispensadores(usuario_id,nombre,modelo,capacidad_g,nivel_actual_g) VALUES(%s,%s,%s,%s,%s)",
                       (uid,"Dispensador Jardin","PetFeeder Mini",2000.0,800.0))
            did2 = cur.lastrowid
            cur.execute("INSERT INTO horarios(mascota_id,dispensador_id,nombre,hora,porcion_g) VALUES(%s,%s,%s,%s,%s)",
                       (mid1,did1,"Desayuno Max","08:00:00",150.0))
            cur.execute("INSERT INTO horarios(mascota_id,dispensador_id,nombre,hora,porcion_g) VALUES(%s,%s,%s,%s,%s)",
                       (mid1,did1,"Cena Max","19:00:00",150.0))
            cur.execute("INSERT INTO horarios(mascota_id,dispensador_id,nombre,hora,porcion_g) VALUES(%s,%s,%s,%s,%s)",
                       (mid2,did1,"Desayuno Luna","09:30:00",120.0))
            cur.execute("INSERT INTO horarios(mascota_id,dispensador_id,nombre,hora,porcion_g,dias_semana) VALUES(%s,%s,%s,%s,%s,%s)",
                       (mid3,did2,"Almuerzo Michi","12:00:00",80.0,"lunes,martes,miercoles,jueves,viernes"))
            for i in range(40):
                fecha = (datetime.now()-timedelta(hours=i*12)).strftime("%Y-%m-%d %H:%M:%S")
                mid   = [mid1,mid2,mid3][i%3]
                did   = did1 if mid!=mid3 else did2
                temp  = round(22+(i%6)*0.8,1)
                hum   = round(55+(i%10),1)
                cur.execute("INSERT INTO dispensaciones(mascota_id,dispensador_id,gramos_programados,gramos_real,tipo,exitosa,temperatura,humedad,ejecutado_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (mid,did,150.0,145.0+i%10,"automatico",1,temp,hum,fecha))
            for i in range(96):
                fecha = (datetime.now()-timedelta(minutes=i*15)).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute("INSERT INTO lecturas_ambiente(temperatura,humedad,leido_at) VALUES(%s,%s,%s)",
                           (round(22+(i%8)*0.5,1),round(55+(i%12),1),fecha))
            defaults = [("costo_kg_alimento",27.0),("veces_manual_dia",2.0),("minutos_por_vez",5.0),
                        ("costo_hora_tiempo",50.0),("costo_proyecto",1000.0),("desperdicio_manual_pct",15.0),
                        ("consulta_vet_pesos",500.0),("visitas_vet_ahorradas",1.0)]
            for k,v in defaults:
                cur.execute("INSERT INTO config_bi(usuario_id,clave,valor) VALUES(%s,%s,%s)",(uid,k,v))
            cur.execute("INSERT INTO notificaciones(usuario_id,tipo,titulo,mensaje) VALUES(%s,%s,%s,%s)",
                       (uid,"dispensado","Max ha comido","Se dispensaron 150g a las 08:00."))
            cur.execute("INSERT INTO notificaciones(usuario_id,tipo,titulo,mensaje) VALUES(%s,%s,%s,%s)",
                       (uid,"nivel_bajo","Nivel bajo","Dispensador Jardin tiene solo 800g."))
            db.commit()
            print("Datos demo cargados")
    db.close()
    print("MySQL listo")

# JWT
def _b64(d): return base64.urlsafe_b64encode(d).rstrip(b"=").decode()
def _d64(s): return base64.urlsafe_b64decode(s+"="*(4-len(s)%4))

def create_token(payload):
    h = _b64(json.dumps({"alg":"HS256","typ":"JWT"}).encode())
    p = dict(payload); p["exp"]=(datetime.utcnow()+timedelta(days=TOKEN_DAYS)).timestamp()
    b = _b64(json.dumps(p).encode())
    sig = hmac.new(JWT_SECRET.encode(),f"{h}.{b}".encode(),hashlib.sha256).digest()
    return f"{h}.{b}.{_b64(sig)}"

def verify_token(token):
    try:
        h,b,s = token.split(".")
        exp = hmac.new(JWT_SECRET.encode(),f"{h}.{b}".encode(),hashlib.sha256).digest()
        if not hmac.compare_digest(_b64(exp).encode(),s.encode()): return None
        p = json.loads(_d64(b))
        return None if p.get("exp",0)<datetime.utcnow().timestamp() else p
    except: return None

def hp(pw): return hashlib.sha256(pw.encode()).hexdigest()
def cp(pw,h): return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(),h)

def auth_required(f):
    @wraps(f)
    def w(*a,**k):
        hdr = request.headers.get("Authorization","")
        if not hdr.startswith("Bearer "): return jsonify(ok=False,message="No autorizado"),401
        p = verify_token(hdr.split(" ",1)[1])
        if not p: return jsonify(ok=False,message="Token invalido o expirado.",expired=True),401
        g.usuario = p
        return f(*a,**k)
    return w

# DISPENSACION
def dispensar_con_peso(porcion_g, timeout=30):
    global peso_zero_ref
    if not HARDWARE_OK or not mg995:
        time.sleep(1); return porcion_g
    vals = [leer_peso_serial() for _ in range(3)]
    vals = [v for v in vals if v is not None]
    if vals: peso_zero_ref = sum(vals)/len(vals)
    mg995.angle = 90; servo_state.update(estado="abierto",angulo=90)
    inicio = time.time(); gr = 0.0
    while time.time()-inicio < timeout:
        raw = leer_peso_serial()
        if raw is not None:
            gr = max(0.0, raw-peso_zero_ref)
            sensor_cache["peso"]["gramos"] = round(gr,1)
            if gr >= porcion_g: break
        time.sleep(0.3)
    mg995.angle = 45; servo_state.update(estado="cerrado",angulo=45)
    return round(gr,1)

# CRON
def cron_loop():
    dias_map = ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"]
    while True:
        try:
            ahora = datetime.now()
            hm    = ahora.strftime("%H:%M")
            dia   = dias_map[ahora.weekday()]
            db    = get_db_direct()
            with db.cursor() as cur:
                cur.execute("""
                    SELECT h.*,d.nivel_actual_g,m.usuario_id,m.nombre AS mn
                    FROM horarios h JOIN dispensadores d ON d.id=h.dispensador_id
                    JOIN mascotas m ON m.id=h.mascota_id
                    WHERE h.activo=1 AND TIME_FORMAT(h.hora,'%%H:%%i')=%s
                      AND FIND_IN_SET(%s,h.dias_semana) AND d.activo=1 AND m.activo=1
                """,(hm,dia))
                hs = cur.fetchall()
            temp = sensor_cache["dht"].get("temperatura")
            hum  = sensor_cache["dht"].get("humedad")
            for h in hs:
                ok = float(h["nivel_actual_g"] or 0) >= float(h["porcion_g"])
                if ok:
                    def do_disp(horario=h):
                        gr = dispensar_con_peso(float(horario["porcion_g"]))
                        db2 = get_db_direct()
                        with db2.cursor() as c:
                            c.execute("INSERT INTO dispensaciones(horario_id,mascota_id,dispensador_id,gramos_programados,gramos_real,tipo,exitosa,temperatura,humedad) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                     (horario["id"],horario["mascota_id"],horario["dispensador_id"],horario["porcion_g"],gr,"automatico",1,temp,hum))
                            c.execute("UPDATE dispensadores SET nivel_actual_g=GREATEST(0,nivel_actual_g-%s) WHERE id=%s",
                                     (horario["porcion_g"],horario["dispensador_id"]))
                            c.execute("INSERT INTO notificaciones(usuario_id,tipo,titulo,mensaje) VALUES(%s,%s,%s,%s)",
                                     (horario["usuario_id"],"dispensado",f"{horario['mn']} ha comido",f"Se dispensaron {gr}g a las {hm}."))
                        db2.commit(); db2.close()
                    threading.Thread(target=do_disp,daemon=True).start()
                else:
                    db2 = get_db_direct()
                    with db2.cursor() as c:
                        c.execute("INSERT INTO dispensaciones(horario_id,mascota_id,dispensador_id,gramos_programados,gramos_real,tipo,exitosa,codigo_error,temperatura,humedad) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                 (h["id"],h["mascota_id"],h["dispensador_id"],h["porcion_g"],0,"automatico",0,"NIVEL_INSUFICIENTE",temp,hum))
                        c.execute("INSERT INTO notificaciones(usuario_id,tipo,titulo,mensaje) VALUES(%s,%s,%s,%s)",
                                 (h["usuario_id"],"sin_comida","No se pudo dispensar",f"Sin comida para {h['mn']}."))
                    db2.commit(); db2.close()
            db.close()
        except Exception as e: print(f"[Cron] {e}")
        time.sleep(60)

# RUTAS ESTATICAS
@app.route("/")
def index(): return send_from_directory(str(STATIC_DIR),"index.html")
@app.route("/<path:f>")
def static_f(f): return send_from_directory(str(STATIC_DIR),f)
def enviar_email_verificacion(email, nombre, token_ver):
    try:
        link = f"https://{APP_URL}/api/auth/verificar/{token_ver}"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "✅ Verifica tu cuenta PetFeeder IoT"
        msg["From"]    = GMAIL_USER
        msg["To"]      = email
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:500px;margin:0 auto;background:#070b12;color:#eef2ff;padding:30px;border-radius:16px">
          <h1 style="color:#6c63ff">🐾 PetFeeder IoT</h1>
          <h2>Hola {nombre}!</h2>
          <p>Gracias por registrarte. Para activar tu cuenta haz clic en el botón:</p>
          <a href="{link}" style="display:inline-block;padding:14px 28px;background:#6c63ff;color:#fff;text-decoration:none;border-radius:10px;font-weight:bold;margin:20px 0">✅ Verificar mi cuenta</a>
          <p style="color:#8899bb;font-size:12px">Si no creaste esta cuenta, ignora este correo.</p>
        </div>"""
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, email, msg.as_string())
        return True
    except Exception as e:
        print(f"[Email] Error: {e}")
        return False
# AUTH
@app.route("/api/auth/login",methods=["POST"])
def login():
    d=request.get_json() or {}
    em=d.get("email","").strip().lower(); pw=d.get("password","")
    if not em or not pw: return jsonify(ok=False,message="Email y contrasena requeridos"),400
    row=query("SELECT * FROM usuarios WHERE email=%s AND activo=1",(em,),one=True)
    if not row or not cp(pw,row["password_hash"]): return jsonify(ok=False,message="Credenciales incorrectas"),401
    if not row["verificado"] and em != "demo@petfeeder.com": return jsonify(ok=False,message="⚠️ Debes verificar tu correo antes de iniciar sesión.",no_verificado=True),403
    token=create_token({"id":row["id"],"email":row["email"],"nombre":row["nombre"]})
    return jsonify(ok=True,token=token,usuario={"id":row["id"],"nombre":row["nombre"],"email":row["email"]})


@app.route("/api/auth/verificar/<token>", methods=["GET"])
def verificar_email(token):
    row = query("SELECT id,nombre FROM usuarios WHERE token_verificacion=%s AND verificado=0",(token,),one=True)
    if not row:
        return """<html><body style="font-family:Arial;background:#070b12;color:#eef2ff;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0">
        <div style="text-align:center;padding:40px;background:#0d1420;border-radius:16px;border:1px solid #1e3048">
        <div style="font-size:3rem">❌</div>
        <h2 style="color:#ff4d6d">Enlace inválido o ya usado</h2>
        <p style="color:#8899bb">Este enlace ya fue usado o no existe.</p>
        <a href="https://dispensador-mascotas-production.up.railway.app" style="color:#6c63ff">← Ir al inicio</a>
        </div></body></html>"""
    query("UPDATE usuarios SET verificado=1, token_verificacion=NULL WHERE id=%s",(row["id"],),commit=True)
    return """<html><body style="font-family:Arial;background:#070b12;color:#eef2ff;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0">
    <div style="text-align:center;padding:40px;background:#0d1420;border-radius:16px;border:1px solid #1e3048">
    <div style="font-size:3rem">✅</div>
    <h2 style="color:#00e5a0">¡Cuenta verificada!</h2>
    <p style="color:#8899bb">Ya puedes iniciar sesión en PetFeeder IoT.</p>
    <a href="https://dispensador-mascotas-production.up.railway.app" style="display:inline-block;padding:12px 24px;background:#6c63ff;color:#fff;text-decoration:none;border-radius:10px;margin-top:16px;font-weight:bold">🐾 Ir al inicio →</a>
    </div></body></html>"""
    
@app.route("/api/auth/register",methods=["POST"])
def register():
    d=request.get_json() or {}
    nombre=d.get("nombre","").strip(); email=d.get("email","").strip().lower(); pw=d.get("password","")
    if not nombre: return jsonify(ok=False,message="Nombre requerido"),400
    if not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+",email): return jsonify(ok=False,message="Email invalido"),400
    if len(pw)<6: return jsonify(ok=False,message="Contrasena minimo 6 caracteres"),400
    if query("SELECT id FROM usuarios WHERE email=%s",(email,),one=True): return jsonify(ok=False,message="Email ya registrado"),409
    token_ver = secrets.token_hex(32)
    uid=query("INSERT INTO usuarios(nombre,email,password_hash,verificado,token_verificacion) VALUES(%s,%s,%s,%s,%s)",
              (nombre,email,hp(pw),0,token_ver),commit=True,lastid=True)
    defaults=[("costo_kg_alimento",27.0),("veces_manual_dia",2.0),("minutos_por_vez",5.0),
              ("costo_hora_tiempo",50.0),("costo_proyecto",1000.0),("desperdicio_manual_pct",15.0),
              ("consulta_vet_pesos",500.0),("visitas_vet_ahorradas",1.0)]
    for k,v in defaults:
        query("INSERT INTO config_bi(usuario_id,clave,valor) VALUES(%s,%s,%s)",(uid,k,v),commit=True)
    enviado = enviar_email_verificacion(email, nombre, token_ver)
    return jsonify(ok=True, message="Cuenta creada. Revisa tu correo para verificar.", email_enviado=enviado),201

# DASHBOARD
@app.route("/api/dashboard")
@auth_required
def dashboard():
    uid=g.usuario["id"]; hoy=datetime.now().strftime("%Y-%m-%d")
    dias_map=["lunes","martes","miercoles","jueves","viernes","sabado","domingo"]
    dia=dias_map[datetime.now().weekday()]; hora=datetime.now().strftime("%H:%M")
    total_m=query("SELECT COUNT(*) AS c FROM mascotas WHERE usuario_id=%s AND activo=1",(uid,),one=True)["c"]
    disps=[r for r in query("""SELECT id,nombre,nivel_actual_g,capacidad_g,
        CASE WHEN capacidad_g>0 THEN ROUND(nivel_actual_g*100.0/capacidad_g,1) ELSE 0 END AS nivel_pct
        FROM dispensadores WHERE usuario_id=%s AND activo=1 ORDER BY nombre""",(uid,))]
    proximas=[r for r in query("""SELECT TIME_FORMAT(h.hora,'%%H:%%i') AS hora,h.porcion_g,
        m.nombre AS mascota_nombre,m.especie,d.nombre AS dispensador_nombre
        FROM horarios h JOIN mascotas m ON m.id=h.mascota_id JOIN dispensadores d ON d.id=h.dispensador_id
        WHERE m.usuario_id=%s AND h.activo=1 AND m.activo=1
          AND FIND_IN_SET(%s,h.dias_semana) AND TIME_FORMAT(h.hora,'%%H:%%i')>%s
        ORDER BY h.hora LIMIT 5""",(uid,dia,hora))]
    stats=query("""SELECT COUNT(*) AS tomas,COALESCE(SUM(gramos_real),0) AS gramos,
        SUM(CASE WHEN exitosa=0 THEN 1 ELSE 0 END) AS fallidas
        FROM dispensaciones d JOIN mascotas m ON m.id=d.mascota_id
        WHERE m.usuario_id=%s AND DATE(d.ejecutado_at)=%s""",(uid,hoy),one=True)
    notifs=query("SELECT COUNT(*) AS c FROM notificaciones WHERE usuario_id=%s AND leida=0",(uid,),one=True)["c"]
    ultimas=[r for r in query("""SELECT d.ejecutado_at,d.gramos_real,d.exitosa,d.temperatura,d.humedad,m.nombre AS mascota_nombre
        FROM dispensaciones d JOIN mascotas m ON m.id=d.mascota_id
        WHERE m.usuario_id=%s ORDER BY d.ejecutado_at DESC LIMIT 5""",(uid,))]
    for u in ultimas:
        if u.get("ejecutado_at"): u["ejecutado_at"]=str(u["ejecutado_at"])
    return jsonify(ok=True,data={
        "total_mascotas":total_m,"dispensadores":list(disps),"proximas_tomas":list(proximas),
        "tomas_hoy":stats["tomas"],"gramos_hoy":float(stats["gramos"] or 0),
        "tomas_fallidas":stats["fallidas"],"notificaciones_no_leidas":notifs,
        "ultimas_dispensaciones":ultimas,"hardware_ok":HARDWARE_OK,"servo_estado":servo_state["estado"],
        "sensores":{"dht":sensor_cache["dht"],"peso":sensor_cache["peso"],
                    "hardware":{"servo":HARDWARE_OK,"dht":DHT_OK,"peso":PESO_OK}}
    })

@app.route("/api/sensores/estado")
@auth_required
def sensores_estado():
    # Si la Pi no ha mandado datos en los últimos 30 segundos, limpiar
    ultima = pi_sensores.get("updated_at")
    pi_conectada = ultima and (datetime.now() - ultima).seconds < 30
    if not pi_conectada:
        cache_dht  = {"ok": False, "temperatura": None, "humedad": None}
        cache_peso = {"ok": False, "gramos": None}
    else:
        cache_dht  = sensor_cache["dht"]
        cache_peso = sensor_cache["peso"]
    return jsonify(ok=True, data={
        "dht": cache_dht,
        "peso": cache_peso,
        "servo": servo_state,
        "hardware": {"servo": HARDWARE_OK, "dht": DHT_OK, "peso": PESO_OK},
        "pi_conectada": pi_conectada
    })
@app.route("/api/sensores/historial")
@auth_required
def sensores_historial():
    return jsonify(ok=True,data=sensor_history)

@app.route("/api/sensores/peso/zero",methods=["POST"])
@auth_required
def peso_zero():
    global peso_zero_ref
    vals=[leer_peso_serial() for _ in range(5)]
    vals=[v for v in vals if v is not None]
    if vals: peso_zero_ref=sum(vals)/len(vals)
    sensor_cache["peso"]["gramos"]=0.0
    return jsonify(ok=True,message="Peso en cero")

# MASCOTAS
@app.route("/api/mascotas",methods=["GET"])
@auth_required
def mascotas_listar():
    hoy=datetime.now().strftime("%Y-%m-%d")
    rows=query("""SELECT m.*,a.marca,a.nombre_producto,a.tipo AS alimento_tipo,
        (SELECT COUNT(*) FROM dispensaciones d WHERE d.mascota_id=m.id AND DATE(d.ejecutado_at)=%s) AS tomas_hoy,
        COALESCE((SELECT SUM(gramos_real) FROM dispensaciones d WHERE d.mascota_id=m.id AND DATE(d.ejecutado_at)=%s AND d.exitosa=1),0) AS gramos_hoy
        FROM mascotas m LEFT JOIN alimentos a ON a.mascota_id=m.id
        WHERE m.usuario_id=%s AND m.activo=1 ORDER BY m.nombre""",(hoy,hoy,g.usuario["id"]))
    for r in rows:
        if r.get("fecha_nac"): r["fecha_nac"]=str(r["fecha_nac"])
        if r.get("created_at"): r["created_at"]=str(r["created_at"])
    return jsonify(ok=True,data=list(rows))

@app.route("/api/mascotas",methods=["POST"])
@auth_required
def mascotas_crear():
    d=request.get_json() or {}
    nombre=d.get("nombre","").strip()
    if not nombre: return jsonify(ok=False,message="Nombre requerido"),400
    mid=query("INSERT INTO mascotas(usuario_id,nombre,especie,raza,fecha_nac,peso_kg,notas) VALUES(%s,%s,%s,%s,%s,%s,%s)",
              (g.usuario["id"],nombre,d.get("especie","perro"),d.get("raza") or None,
               d.get("fecha_nac") or None,d.get("peso_kg") or None,d.get("notas") or None),commit=True,lastid=True)
    if d.get("alimento_nombre","").strip():
        query("INSERT INTO alimentos(mascota_id,marca,nombre_producto,tipo) VALUES(%s,%s,%s,%s)",
              (mid,d.get("alimento_marca") or None,d["alimento_nombre"].strip(),d.get("alimento_tipo","seco")),commit=True)
    return jsonify(ok=True,message="Mascota creada",id=mid),201

@app.route("/api/mascotas/<int:mid>",methods=["GET"])
@auth_required
def mascotas_obtener(mid):
    row=query("SELECT m.*,a.marca,a.nombre_producto FROM mascotas m LEFT JOIN alimentos a ON a.mascota_id=m.id WHERE m.id=%s AND m.usuario_id=%s AND m.activo=1",(mid,g.usuario["id"]),one=True)
    if not row: return jsonify(ok=False,message="No encontrada"),404
    if row.get("fecha_nac"): row["fecha_nac"]=str(row["fecha_nac"])
    return jsonify(ok=True,data=dict(row))

@app.route("/api/mascotas/<int:mid>",methods=["PUT"])
@auth_required
def mascotas_actualizar(mid):
    d=request.get_json() or {}
    if not query("SELECT id FROM mascotas WHERE id=%s AND usuario_id=%s AND activo=1",(mid,g.usuario["id"]),one=True):
        return jsonify(ok=False,message="No encontrada"),404
    query("UPDATE mascotas SET nombre=%s,especie=%s,raza=%s,fecha_nac=%s,peso_kg=%s,notas=%s WHERE id=%s",
          (d.get("nombre"),d.get("especie","perro"),d.get("raza") or None,d.get("fecha_nac") or None,
           d.get("peso_kg") or None,d.get("notas") or None,mid),commit=True)
    return jsonify(ok=True,message="Actualizada")

@app.route("/api/mascotas/<int:mid>",methods=["DELETE"])
@auth_required
def mascotas_eliminar(mid):
    if not query("SELECT id FROM mascotas WHERE id=%s AND usuario_id=%s AND activo=1",(mid,g.usuario["id"]),one=True):
        return jsonify(ok=False,message="No encontrada"),404
    query("UPDATE mascotas SET activo=0 WHERE id=%s",(mid,),commit=True)
    return jsonify(ok=True,message="Eliminada")

# DISPENSADORES
@app.route("/api/dispensadores",methods=["GET"])
@auth_required
def disps_listar():
    rows=query("""SELECT d.*,
        CASE WHEN d.capacidad_g>0 THEN ROUND(d.nivel_actual_g*100.0/d.capacidad_g,1) ELSE 0 END AS nivel_pct,
        (SELECT COUNT(*) FROM horarios h WHERE h.dispensador_id=d.id AND h.activo=1) AS horarios_activos
        FROM dispensadores d WHERE d.usuario_id=%s AND d.activo=1 ORDER BY d.nombre""",(g.usuario["id"],))
    for r in rows:
        if r.get("ultima_conexion"): r["ultima_conexion"]=str(r["ultima_conexion"])
        if r.get("created_at"): r["created_at"]=str(r["created_at"])
    return jsonify(ok=True,data=list(rows))

@app.route("/api/dispensadores",methods=["POST"])
@auth_required
def disps_crear():
    d=request.get_json() or {}
    if not d.get("nombre","").strip(): return jsonify(ok=False,message="Nombre requerido"),400
    try:
        did=query("INSERT INTO dispensadores(usuario_id,nombre,mac_address,modelo,capacidad_g,nivel_actual_g) VALUES(%s,%s,%s,%s,%s,0)",
                  (g.usuario["id"],d["nombre"].strip(),d.get("mac_address") or None,d.get("modelo") or None,d.get("capacidad_g") or None),commit=True,lastid=True)
    except pymysql.IntegrityError:
        return jsonify(ok=False,message="MAC ya registrada"),409
    return jsonify(ok=True,message="Creado",id=did),201

@app.route("/api/dispensadores/<int:did>",methods=["GET"])
@auth_required
def disps_obtener(did):
    row=query("SELECT * FROM dispensadores WHERE id=%s AND usuario_id=%s AND activo=1",(did,g.usuario["id"]),one=True)
    if not row: return jsonify(ok=False,message="No encontrado"),404
    return jsonify(ok=True,data=dict(row))

@app.route("/api/dispensadores/<int:did>",methods=["PUT"])
@auth_required
def disps_actualizar(did):
    d=request.get_json() or {}
    if not query("SELECT id FROM dispensadores WHERE id=%s AND usuario_id=%s AND activo=1",(did,g.usuario["id"]),one=True):
        return jsonify(ok=False,message="No encontrado"),404
    query("UPDATE dispensadores SET nombre=%s,modelo=%s,capacidad_g=%s WHERE id=%s",
          (d.get("nombre"),d.get("modelo") or None,d.get("capacidad_g") or None,did),commit=True)
    return jsonify(ok=True,message="Actualizado")

@app.route("/api/dispensadores/<int:did>/nivel",methods=["PATCH"])
@auth_required
def disps_nivel(did):
    nivel=float((request.get_json() or {}).get("nivel_actual_g",-1))
    if nivel<0: return jsonify(ok=False,message="Nivel invalido"),400
    if not query("SELECT id FROM dispensadores WHERE id=%s AND usuario_id=%s AND activo=1",(did,g.usuario["id"]),one=True):
        return jsonify(ok=False,message="No encontrado"),404
    query("UPDATE dispensadores SET nivel_actual_g=%s WHERE id=%s",(nivel,did),commit=True)
    return jsonify(ok=True,message="Nivel actualizado",nivel_actual_g=nivel)

@app.route("/api/dispensadores/<int:did>",methods=["DELETE"])
@auth_required
def disps_eliminar(did):
    if not query("SELECT id FROM dispensadores WHERE id=%s AND usuario_id=%s AND activo=1",(did,g.usuario["id"]),one=True):
        return jsonify(ok=False,message="No encontrado"),404
    query("UPDATE dispensadores SET activo=0 WHERE id=%s",(did,),commit=True)
    return jsonify(ok=True,message="Eliminado")

# HORARIOS
@app.route("/api/horarios",methods=["GET"])
@auth_required
def horarios_listar():
    rows=query("""SELECT h.*,TIME_FORMAT(h.hora,'%%H:%%i') AS hora_fmt,
        m.nombre AS mascota_nombre,m.especie,d.nombre AS dispensador_nombre,d.nivel_actual_g
        FROM horarios h JOIN mascotas m ON m.id=h.mascota_id JOIN dispensadores d ON d.id=h.dispensador_id
        WHERE m.usuario_id=%s AND m.activo=1 ORDER BY h.hora""",(g.usuario["id"],))
    result=[]
    for r in rows:
        r=dict(r); r["hora"]=r.pop("hora_fmt",""); result.append(r)
    return jsonify(ok=True,data=result)

@app.route("/api/horarios",methods=["POST"])
@auth_required
def horarios_crear():
    d=request.get_json() or {}
    if not all([d.get("mascota_id"),d.get("dispensador_id"),d.get("hora"),d.get("porcion_g")]):
        return jsonify(ok=False,message="Campos requeridos"),400
    hid=query("INSERT INTO horarios(mascota_id,dispensador_id,nombre,hora,porcion_g,dias_semana,velocidad) VALUES(%s,%s,%s,%s,%s,%s,%s)",
              (d["mascota_id"],d["dispensador_id"],d.get("nombre") or None,d["hora"][:5],
               float(d["porcion_g"]),d.get("dias_semana","lunes,martes,miercoles,jueves,viernes,sabado,domingo"),
               d.get("velocidad","normal")),commit=True,lastid=True)
    return jsonify(ok=True,message="Horario creado",id=hid),201

@app.route("/api/horarios/<int:hid>",methods=["GET"])
@auth_required
def horarios_obtener(hid):
    row=query("""SELECT h.*,TIME_FORMAT(h.hora,'%%H:%%i') AS hora_fmt,
        m.nombre AS mascota_nombre,d.nombre AS dispensador_nombre
        FROM horarios h JOIN mascotas m ON m.id=h.mascota_id JOIN dispensadores d ON d.id=h.dispensador_id
        WHERE h.id=%s AND m.usuario_id=%s""",(hid,g.usuario["id"]),one=True)
    if not row: return jsonify(ok=False,message="No encontrado"),404
    row=dict(row); row["hora"]=row.pop("hora_fmt","")
    return jsonify(ok=True,data=row)

@app.route("/api/horarios/<int:hid>",methods=["PUT"])
@auth_required
def horarios_actualizar(hid):
    d=request.get_json() or {}
    if not query("SELECT h.id FROM horarios h JOIN mascotas m ON m.id=h.mascota_id WHERE h.id=%s AND m.usuario_id=%s",(hid,g.usuario["id"]),one=True):
        return jsonify(ok=False,message="No encontrado"),404
    query("UPDATE horarios SET nombre=%s,hora=%s,porcion_g=%s,dias_semana=%s,velocidad=%s,activo=%s WHERE id=%s",
          (d.get("nombre") or None,d.get("hora","")[:5],float(d.get("porcion_g",0)),
           d.get("dias_semana","lunes,martes,miercoles,jueves,viernes,sabado,domingo"),
           d.get("velocidad","normal"),1 if d.get("activo",True) else 0,hid),commit=True)
    return jsonify(ok=True,message="Actualizado")

@app.route("/api/horarios/<int:hid>/toggle",methods=["PATCH"])
@auth_required
def horarios_toggle(hid):
    row=query("SELECT h.id,h.activo FROM horarios h JOIN mascotas m ON m.id=h.mascota_id WHERE h.id=%s AND m.usuario_id=%s",(hid,g.usuario["id"]),one=True)
    if not row: return jsonify(ok=False,message="No encontrado"),404
    nuevo=0 if row["activo"] else 1
    query("UPDATE horarios SET activo=%s WHERE id=%s",(nuevo,hid),commit=True)
    return jsonify(ok=True,activo=nuevo)

@app.route("/api/horarios/<int:hid>",methods=["DELETE"])
@auth_required
def horarios_eliminar(hid):
    if not query("SELECT h.id FROM horarios h JOIN mascotas m ON m.id=h.mascota_id WHERE h.id=%s AND m.usuario_id=%s",(hid,g.usuario["id"]),one=True):
        return jsonify(ok=False,message="No encontrado"),404
    query("DELETE FROM horarios WHERE id=%s",(hid,),commit=True)
    return jsonify(ok=True,message="Eliminado")

# DISPENSACIONES
@app.route("/api/dispensaciones",methods=["GET"])
@auth_required
def disps_hist():
    uid=g.usuario["id"]; args=request.args
    limit=min(max(int(args.get("limit",50)),1),200)
    offset=max(int(args.get("offset",0)),0)
    sql="""SELECT d.*,m.nombre AS mascota_nombre,dp.nombre AS dispensador_nombre
        FROM dispensaciones d JOIN mascotas m ON m.id=d.mascota_id
        JOIN dispensadores dp ON dp.id=d.dispensador_id WHERE m.usuario_id=%s"""
    params=[uid]
    if args.get("mascota_id"): sql+=" AND d.mascota_id=%s"; params.append(args["mascota_id"])
    if args.get("fecha_desde"): sql+=" AND DATE(d.ejecutado_at)>=%s"; params.append(args["fecha_desde"])
    if args.get("fecha_hasta"): sql+=" AND DATE(d.ejecutado_at)<=%s"; params.append(args["fecha_hasta"])
    sql+=" ORDER BY d.ejecutado_at DESC LIMIT %s OFFSET %s"; params+=[limit,offset]
    rows=query(sql,params)
    result=[]
    for r in rows:
        r=dict(r)
        if r.get("ejecutado_at"): r["ejecutado_at"]=str(r["ejecutado_at"])
        result.append(r)
    return jsonify(ok=True,data=result)

@app.route("/api/dispensaciones/manual",methods=["POST"])
@auth_required
def disp_manual():
    d=request.get_json() or {}; uid=g.usuario["id"]
    mid,did,gramos=d.get("mascota_id"),d.get("dispensador_id"),float(d.get("gramos",0))
    if not all([mid,did,gramos]): return jsonify(ok=False,message="Faltan datos"),400
    mascota=query("SELECT id,nombre FROM mascotas WHERE id=%s AND usuario_id=%s AND activo=1",(mid,uid),one=True)
    disp=query("SELECT id,nivel_actual_g FROM dispensadores WHERE id=%s AND usuario_id=%s AND activo=1",(did,uid),one=True)
    if not mascota or not disp: return jsonify(ok=False,message="No encontrado"),403
    if float(disp["nivel_actual_g"])<gramos: return jsonify(ok=False,message=f"Nivel insuficiente. Disponible: {disp['nivel_actual_g']}g"),422
    temp=sensor_cache["dht"].get("temperatura"); hum=sensor_cache["dht"].get("humedad")
    def do_disp():
        gr=dispensar_con_peso(gramos)
        db2=get_db_direct()
        with db2.cursor() as c:
            c.execute("INSERT INTO dispensaciones(mascota_id,dispensador_id,gramos_programados,gramos_real,tipo,exitosa,nota,temperatura,humedad) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (mid,did,gramos,gr,"manual",1,d.get("nota") or "Manual",temp,hum))
            c.execute("UPDATE dispensadores SET nivel_actual_g=GREATEST(0,nivel_actual_g-%s) WHERE id=%s",(gramos,did))
            c.execute("INSERT INTO notificaciones(usuario_id,tipo,titulo,mensaje) VALUES(%s,%s,%s,%s)",
                      (uid,"dispensado",f"{mascota['nombre']} ha comido",f"Se dispensaron {gr}g manualmente."))
        db2.commit(); db2.close()
    threading.Thread(target=do_disp,daemon=True).start()
    return jsonify(ok=True,message="Dispensando..."),201

# SERVO
@app.route("/api/compuerta", methods=["POST"])
@auth_required
def compuerta():
    global pi_orden_id
    accion = (request.get_json() or {}).get("accion", "cerrar")
    angulo = 90 if accion == "abrir" else 45
    pi_orden_id += 1
    pi_ordenes.append({"id": pi_orden_id, "tipo": "servo", "angulo": angulo, "ejecutada": False})
    return jsonify(ok=True, estado="abierto" if accion=="abrir" else "cerrado", angulo=angulo)

@app.route("/api/compuerta/estado")
@auth_required
def compuerta_estado():
    return jsonify(**servo_state,hardware=HARDWARE_OK)

@app.route("/api/servo/<int:angulo>",methods=["POST"])
@auth_required
def servo_mover(angulo):
    if not (0<=angulo<=180): return jsonify(ok=False,message="Angulo 0-180"),400
    servo_state["angulo"]=angulo
    if HARDWARE_OK and mg995:
        try: mg995.angle=angulo
        except Exception as e: return jsonify(ok=False,message=str(e)),500
    return jsonify(ok=True,angulo=angulo,simulado=not HARDWARE_OK)

@app.route("/api/servo/sweep",methods=["POST"])
@auth_required
def servo_sweep():
    def do():
        if not HARDWARE_OK or not mg995: return
        for a in range(0,181,10): mg995.angle=a; servo_state["angulo"]=a; time.sleep(0.05)
        for a in range(180,-1,-10): mg995.angle=a; servo_state["angulo"]=a; time.sleep(0.05)
        mg995.angle=90; servo_state["angulo"]=90
    threading.Thread(target=do,daemon=True).start()
    return jsonify(ok=True)

# NOTIFICACIONES
@app.route("/api/notificaciones")
@auth_required
def notifs():
    rows=query("SELECT * FROM notificaciones WHERE usuario_id=%s ORDER BY created_at DESC LIMIT 20",(g.usuario["id"],))
    result=[]
    for r in rows:
        r=dict(r)
        if r.get("created_at"): r["created_at"]=str(r["created_at"])
        result.append(r)
    return jsonify(ok=True,data=result)

@app.route("/api/notificaciones/leer",methods=["POST"])
@auth_required
def notifs_leer():
    query("UPDATE notificaciones SET leida=1 WHERE usuario_id=%s",(g.usuario["id"],),commit=True)
    return jsonify(ok=True)

# GRAFICAS
@app.route("/api/graficas/consumo_diario")
@auth_required
def grafica_consumo():
    rows=query("""SELECT DATE(d.ejecutado_at) AS fecha,
        COALESCE(SUM(d.gramos_real),0) AS total_gramos,COUNT(*) AS tomas,
        COALESCE(AVG(d.temperatura),0) AS temp_prom
        FROM dispensaciones d JOIN mascotas m ON m.id=d.mascota_id
        WHERE m.usuario_id=%s AND d.exitosa=1 AND d.ejecutado_at>=DATE_SUB(NOW(),INTERVAL 14 DAY)
        GROUP BY DATE(d.ejecutado_at) ORDER BY fecha ASC""",(g.usuario["id"],))
    result=[]
    for r in rows:
        r=dict(r); r["fecha"]=str(r["fecha"]); result.append(r)
    return jsonify(ok=True,data=result)

@app.route("/api/graficas/consumo_por_mascota")
@auth_required
def grafica_mascota():
    rows=query("""SELECT m.nombre,m.especie,COALESCE(SUM(d.gramos_real),0) AS total_gramos,COUNT(*) AS tomas
        FROM dispensaciones d JOIN mascotas m ON m.id=d.mascota_id
        WHERE m.usuario_id=%s AND d.exitosa=1 AND d.ejecutado_at>=DATE_SUB(NOW(),INTERVAL 30 DAY)
        GROUP BY m.id ORDER BY total_gramos DESC""",(g.usuario["id"],))
    return jsonify(ok=True,data=list(rows))

@app.route("/api/graficas/tomas_por_hora")
@auth_required
def grafica_hora():
    rows=query("""SELECT HOUR(d.ejecutado_at) AS hora,COUNT(*) AS tomas,COALESCE(SUM(d.gramos_real),0) AS gramos
        FROM dispensaciones d JOIN mascotas m ON m.id=d.mascota_id
        WHERE m.usuario_id=%s AND d.exitosa=1 AND d.ejecutado_at>=DATE_SUB(NOW(),INTERVAL 30 DAY)
        GROUP BY HOUR(d.ejecutado_at) ORDER BY hora""",(g.usuario["id"],))
    return jsonify(ok=True,data=list(rows))

@app.route("/api/graficas/nivel_dispensadores")
@auth_required
def grafica_nivel():
    rows=query("""SELECT nombre,nivel_actual_g,capacidad_g,
        CASE WHEN capacidad_g>0 THEN ROUND(nivel_actual_g*100.0/capacidad_g,1) ELSE 0 END AS pct
        FROM dispensadores WHERE usuario_id=%s AND activo=1""",(g.usuario["id"],))
    return jsonify(ok=True,data=list(rows))

@app.route("/api/graficas/temperatura")
@auth_required
def grafica_temp():
    rows=query("""SELECT DATE_FORMAT(leido_at,'%%H:%%i') AS hora,temperatura,humedad
        FROM lecturas_ambiente WHERE leido_at>=DATE_SUB(NOW(),INTERVAL 24 HOUR)
        ORDER BY leido_at ASC LIMIT 100""",())
    return jsonify(ok=True,data=list(rows),actual=sensor_cache["dht"],
                   historial_live=sensor_history["temperatura"][-20:])

# BI
@app.route("/api/bi/config",methods=["GET"])
@auth_required
def bi_config_get():
    rows=query("SELECT clave,valor FROM config_bi WHERE usuario_id=%s",(g.usuario["id"],))
    return jsonify(ok=True,data={r["clave"]:float(r["valor"]) for r in rows})

@app.route("/api/bi/config",methods=["PUT"])
@auth_required
def bi_config_put():
    d=request.get_json() or {}; uid=g.usuario["id"]
    for k,v in d.items():
        query("INSERT INTO config_bi(usuario_id,clave,valor) VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE valor=%s",
              (uid,k,float(v),float(v)),commit=True)
    return jsonify(ok=True,message="Configuracion guardada")

@app.route("/api/bi/ahorro")
@auth_required
def bi_ahorro():
    uid=g.usuario["id"]
    cfg_rows=query("SELECT clave,valor FROM config_bi WHERE usuario_id=%s",(uid,))
    cfg={r["clave"]:float(r["valor"]) for r in cfg_rows}
    cfg.setdefault("costo_kg_alimento",27.0); cfg.setdefault("veces_manual_dia",2.0)
    cfg.setdefault("minutos_por_vez",5.0); cfg.setdefault("costo_hora_tiempo",50.0)
    cfg.setdefault("costo_proyecto",1000.0); cfg.setdefault("desperdicio_manual_pct",15.0)
    cfg.setdefault("consulta_vet_pesos",500.0); cfg.setdefault("visitas_vet_ahorradas",1.0)
    stats=query("""SELECT COUNT(*) AS total_disp,SUM(gramos_real) AS total_gramos,
        SUM(gramos_programados) AS total_programado,MIN(ejecutado_at) AS primera_disp,
        AVG(gramos_programados-gramos_real) AS desperdicio_prom
        FROM dispensaciones d JOIN mascotas m ON m.id=d.mascota_id
        WHERE m.usuario_id=%s AND d.exitosa=1""",(uid,),one=True)
    total_gramos=float(stats["total_gramos"] or 0)
    total_programado=float(stats["total_programado"] or 0)
    primera_disp=stats["primera_disp"]
    dias_uso=max(1,(datetime.now()-primera_disp).days) if primera_disp else 1
    desperdicio_manual_kg=(total_gramos/1000)*(cfg["desperdicio_manual_pct"]/100)
    ahorro_comida_pesos=desperdicio_manual_kg*cfg["costo_kg_alimento"]*1000
    gramos_ahorrados=desperdicio_manual_kg*1000
    horas_ahorradas=dias_uso*cfg["veces_manual_dia"]*cfg["minutos_por_vez"]/60
    ahorro_tiempo_pesos=horas_ahorradas*cfg["costo_hora_tiempo"]
    ahorro_vet_pesos=cfg["visitas_vet_ahorradas"]*cfg["consulta_vet_pesos"]
    total_ahorrado=ahorro_comida_pesos+ahorro_tiempo_pesos+ahorro_vet_pesos
    roi_pct=((total_ahorrado-cfg["costo_proyecto"])/cfg["costo_proyecto"])*100
    meses_retorno=cfg["costo_proyecto"]/max(1,total_ahorrado/max(1,dias_uso)*30)
    ahorro_mensual=total_ahorrado/max(1,dias_uso)*30
    precision_pct=round((1-abs(total_gramos-total_programado)/max(1,total_programado))*100,1) if total_programado>0 else 100
    return jsonify(ok=True,data={
        "dias_uso":dias_uso,"total_dispensaciones":int(stats["total_disp"] or 0),
        "total_gramos":round(total_gramos,1),"precision_pct":precision_pct,
        "ahorro":{"comida_gramos":round(gramos_ahorrados,1),"comida_pesos":round(ahorro_comida_pesos,2),
                  "tiempo_horas":round(horas_ahorradas,1),"tiempo_pesos":round(ahorro_tiempo_pesos,2),
                  "veterinario_pesos":round(ahorro_vet_pesos,2),"total_pesos":round(total_ahorrado,2)},
        "roi":{"costo_proyecto":cfg["costo_proyecto"],"total_ahorrado":round(total_ahorrado,2),
               "roi_pct":round(roi_pct,1),"meses_retorno":round(meses_retorno,1),
               "recuperado":total_ahorrado>=cfg["costo_proyecto"]},
        "proyecciones":{"ahorro_mensual":round(ahorro_mensual,2),"ahorro_anual":round(ahorro_mensual*12,2),
                        "costo_comida_dia":round((total_gramos/max(1,dias_uso)/1000)*cfg["costo_kg_alimento"],2)},
        "config":cfg,
    })

@app.route("/api/bi/resumen")
@auth_required
def bi_resumen():
    uid=g.usuario["id"]; hoy=datetime.now().strftime("%Y-%m-%d")
    mes=(datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d")
    hoy_s=query("""SELECT COALESCE(SUM(gramos_real),0) AS gramos,COUNT(*) AS tomas,
        SUM(CASE WHEN exitosa=0 THEN 1 ELSE 0 END) AS fallidas
        FROM dispensaciones d JOIN mascotas m ON m.id=d.mascota_id
        WHERE m.usuario_id=%s AND DATE(d.ejecutado_at)=%s""",(uid,hoy),one=True)
    top=query("""SELECT m.nombre,COALESCE(SUM(d.gramos_real),0) AS total
        FROM dispensaciones d JOIN mascotas m ON m.id=d.mascota_id
        WHERE m.usuario_id=%s AND d.ejecutado_at>=%s AND d.exitosa=1
        GROUP BY m.id ORDER BY total DESC LIMIT 1""",(uid,mes),one=True)
    hora_pico=query("""SELECT HOUR(d.ejecutado_at) AS hora,COUNT(*) AS cnt
        FROM dispensaciones d JOIN mascotas m ON m.id=d.mascota_id
        WHERE m.usuario_id=%s AND d.ejecutado_at>=%s
        GROUP BY HOUR(d.ejecutado_at) ORDER BY cnt DESC LIMIT 1""",(uid,mes),one=True)
    nivel_bajo=query("""SELECT COUNT(*) AS c FROM dispensadores
        WHERE usuario_id=%s AND activo=1 AND capacidad_g>0 AND (nivel_actual_g*100.0/capacidad_g)<20""",(uid,),one=True)
    ef=query("""SELECT COUNT(*) AS total,SUM(CASE WHEN exitosa=1 THEN 1 ELSE 0 END) AS exitosas
        FROM dispensaciones d JOIN mascotas m ON m.id=d.mascota_id
        WHERE m.usuario_id=%s AND d.ejecutado_at>=%s""",(uid,mes),one=True)
    ef_pct=round((ef["exitosas"]/ef["total"])*100,1) if ef["total"]>0 else 100
    return jsonify(ok=True,data={
        "hoy":{"gramos":float(hoy_s["gramos"] or 0),"tomas":hoy_s["tomas"],"fallidas":hoy_s["fallidas"]},
        "top_mascota":dict(top) if top else None,
        "hora_pico":f"{hora_pico['hora']:02d}:00" if hora_pico else None,
        "dispensadores_nivel_bajo":nivel_bajo["c"],
        "eficiencia_pct":ef_pct,
        "sensor_actual":sensor_cache["dht"],
    })

@app.route("/api/bi/alertas")
@auth_required
def bi_alertas():
    uid=g.usuario["id"]; hoy=datetime.now().strftime("%Y-%m-%d"); alertas=[]
    bajos=query("""SELECT nombre,nivel_actual_g,capacidad_g,ROUND(nivel_actual_g*100.0/capacidad_g,1) AS pct
        FROM dispensadores WHERE usuario_id=%s AND activo=1 AND capacidad_g>0 AND (nivel_actual_g*100.0/capacidad_g)<20""",(uid,))
    for d in bajos:
        alertas.append({"tipo":"warning","titulo":f"Nivel bajo: {d['nombre']}","msg":f"Solo {d['nivel_actual_g']}g ({d['pct']}%)."})
    sin=query("""SELECT m.nombre FROM mascotas m WHERE m.usuario_id=%s AND m.activo=1
        AND m.id NOT IN (SELECT DISTINCT mascota_id FROM dispensaciones WHERE DATE(ejecutado_at)=%s AND exitosa=1)""",(uid,hoy))
    for m in sin:
        alertas.append({"tipo":"info","titulo":f"{m['nombre']} no ha comido hoy","msg":"Sin tomas exitosas hoy."})
    t=sensor_cache["dht"].get("temperatura")
    if t and t>35: alertas.append({"tipo":"error","titulo":"Temperatura alta","msg":f"{t}C puede afectar la comida."})
    return jsonify(ok=True,alertas=alertas,total=len(alertas))

@app.route("/api/health")
def health():
    return jsonify(ok=True,app="PetFeeder IoT v4",db="MySQL",hardware=HARDWARE_OK,dht=DHT_OK,peso=PESO_OK)

# ENDPOINTS RASPBERRY PI
pi_sensores = {"temperatura": None, "humedad": None, "peso": None, "hardware": {}, "updated_at": None}
pi_ordenes  = []
pi_orden_id = 0

@app.route("/api/pi/sensores", methods=["POST"])
@auth_required
def pi_recibir_sensores():
    global pi_sensores
    d = request.get_json() or {}
    pi_sensores.update(d)
    pi_sensores["updated_at"] = datetime.now()
    if d.get("temperatura"):
        sensor_cache["dht"] = {"ok": True, "temperatura": d["temperatura"], "humedad": d.get("humedad")}
    if d.get("peso") is not None:
        sensor_cache["peso"] = {"ok": True, "gramos": d["peso"]}
    return jsonify(ok=True)

@app.route("/api/pi/ordenes", methods=["GET"])
@auth_required
def pi_get_ordenes():
    pendientes = [o for o in pi_ordenes if not o.get("ejecutada")]
    return jsonify(ok=True, ordenes=pendientes)

@app.route("/api/pi/ordenes/<int:oid>/confirmar", methods=["POST"])
@auth_required
def pi_confirmar_orden(oid):
    for o in pi_ordenes:
        if o["id"] == oid:
            o["ejecutada"] = True
            break
    return jsonify(ok=True)

@app.route("/api/pi/servo", methods=["POST"])
@auth_required
def pi_pedir_servo():
    global pi_orden_id
    d = request.get_json() or {}
    pi_orden_id += 1
    pi_ordenes.append({"id": pi_orden_id, "tipo": "servo", "angulo": d.get("angulo", 45), "ejecutada": False})
    return jsonify(ok=True, orden_id=pi_orden_id)



# INICIALIZAR
try:
    init_db()
except Exception as e:
    print(f"init_db fallo: {e}")

threading.Thread(target=cron_loop, daemon=True).start()

if __name__=="__main__":
    PORT = int(os.environ.get("PORT", 5001))
    print(f"PetFeeder IoT v4 en http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
