"""
Comprueba la disponibilidad en https://www.mundicolor.es/availability
(Imserso / Mundicolor) para los destinos/provincias configurados.

- LOG en consola: muestra TODOS los días disponibles de cada combo.
- EMAIL: solo se envía si algún día tiene un hotel de HOTELES_OBJETIVO,
         y en el cuerpo solo se listan las líneas de esos hoteles objetivo.

Solo CONSULTA: no reserva nada.

Instalación (una vez):
    pip install playwright
    playwright install chromium

Uso:
    python mundicolor_disponibilidad.py                     # una pasada
    python mundicolor_disponibilidad.py --watch 5           # repite cada 5 min
    python mundicolor_disponibilidad.py --solo BALEARES     # solo un destino
    python mundicolor_disponibilidad.py --debug             # navegador visible + capturas
"""
import argparse
import json
import re
import smtplib
import sys
import time
import unicodedata
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = "https://www.mundicolor.es/availability"

# ----------------------------- DATOS ---------------------------------
PASAJEROS = [
    {"dni": "40428643W", "clave": "6075"},   # Pasajero 1
    {"dni": "53072868T", "clave": "6075"},   # Pasajero 2
]
MASCOTAS = False
TRANSPORTE = "Sin transporte"
ORIGEN = "Selecciona"
LOCALIDAD = "Cualquiera"
NUM_DIAS = "Cualquiera"
# ---------------------------------------------------------------------

# -------------------- COMBOS DESTINO / PROVINCIA ---------------------
COMBOS = [
    ("BALEARES", "MALLORCA"),
    ("BALEARES", "MENORCA"),
    ("BALEARES", "IBIZA"),
    ("CANARIAS", "LANZAROTE"),
    ("CANARIAS", "TENERIFE"),
    ("CANARIAS", "FUERTEVENTURA"),
]
# ---------------------------------------------------------------------

# ----------------- HOTELES OBJETIVO (whitelist) ----------------------
HOTELES_OBJETIVO = [
    "Alua Boccaccio",
    "Alua Gran Camp de Mar",
    "Samos",
    "Indico Rock",
    "Alua Calvià",
    "AluaSun Continental Park",
    "Sol Guadalupe Magaluf",
    "Globales Almirante Farragut",
    "Bakour Fuerteventura La Pared",
    "Parque Vacacional Eden",
]
# ---------------------------------------------------------------------

# ----------------- FILTRO TEMPORAL POR DESTINO -----------------------
FILTRO_POR_DESTINO = {
    "BALEARES": {"anio": 2027, "meses": {3, 4, 5, 6, 7, 8, 9, 10}},
    "CANARIAS": None,
}
MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
# ---------------------------------------------------------------------

# ----------------------------- EMAIL ---------------------------------
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_USER = "marioparejanieto@gmail.com"
SMTP_PASS = "wnfx ldkt oomn yqoi"
EMAIL_DESTINO = "marioparejanieto@gmail.com"
EMAIL_SOLO_SI_HAY = True
# ---------------------------------------------------------------------

# --------------------------- CAPTCHA ---------------------------------
CAPTCHA_TIMEOUT_S = 300
# ---------------------------------------------------------------------

OUT_DIR = Path(__file__).parent / "mundicolor_out"
OUT_DIR.mkdir(exist_ok=True)
COOKIES_FILE = OUT_DIR / "cookies_ok.json"


def shot(page, name, debug):
    if debug:
        page.screenshot(path=str(OUT_DIR / f"{name}.png"), full_page=True)


# ---------------------------------------------------------------------
# NORMALIZACIÓN DE NOMBRES DE HOTEL
# ---------------------------------------------------------------------
def normalizar(s):
    """Minúsculas, sin acentos, espacios colapsados."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


HOTELES_OBJETIVO_NORM = [normalizar(h) for h in HOTELES_OBJETIVO]


def hotel_match(linea):
    """
    Devuelve el nombre original del hotel de la lista que aparece en `linea`,
    o None si no hay match. Comparación: minúsculas, sin acentos, palabra completa.
    """
    n = normalizar(linea)
    if not n:
        return None
    for h_orig, h_norm in zip(HOTELES_OBJETIVO, HOTELES_OBJETIVO_NORM):
        if re.search(r"(?<![a-z])" + re.escape(h_norm) + r"(?![a-z])", n):
            return h_orig
    return None


# ---------------------------------------------------------------------
# COOKIES
# ---------------------------------------------------------------------
def cookies(page, debug=False):
    def banner_visible():
        try:
            return page.locator(
                "#onetrust-banner-sdk:visible, #onetrust-consent-sdk:visible, "
                ".onetrust-pc-dark-filter:visible, "
                "div[class*='cookie']:visible, div[id*='cookie']:visible"
            ).first.is_visible(timeout=500)
        except Exception:
            return False

    page.wait_for_timeout(800)

    if not banner_visible():
        if debug:
            print("      [cookies] sin banner (ya aceptado)")
        return True

    exito = False

    for sel in ["#onetrust-accept-btn-handler",
                "button#onetrust-accept-btn-handler",
                "button[aria-label*='Aceptar' i]"]:
        try:
            btn = page.locator(sel).first
            btn.wait_for(state="visible", timeout=4000)
            btn.click()
            page.wait_for_timeout(700)
            if debug:
                print(f"      [cookies] clic en {sel}")
            if not banner_visible():
                exito = True
                break
        except Exception:
            continue

    if not exito:
        for pat in [r"ACEPTAR TODAS", r"Aceptar todas", r"Aceptar todo",
                    r"Aceptar y cerrar", r"^Aceptar$", r"^Acepto", r"De acuerdo"]:
            try:
                rx = re.compile(pat, re.I)
                btn = page.locator(
                    "button:visible, a:visible, [role='button']:visible, "
                    "input[type='button']:visible, input[type='submit']:visible"
                ).filter(has_text=rx).first
                if btn.count() > 0:
                    btn.click(timeout=3000)
                    page.wait_for_timeout(700)
                    if debug:
                        print(f"      [cookies] clic en botón '{pat}'")
                    if not banner_visible():
                        exito = True
                        break
            except Exception:
                continue

    if not exito:
        for frame in page.frames:
            try:
                fb = frame.locator(
                    "#onetrust-accept-btn-handler, "
                    "button:has-text('ACEPTAR TODAS'), "
                    "button:has-text('Aceptar todas')"
                ).first
                if fb.count() > 0:
                    fb.click(timeout=3000)
                    page.wait_for_timeout(700)
                    if not banner_visible():
                        exito = True
                        break
            except Exception:
                continue

    if not exito and banner_visible():
        page.evaluate("""() => {
            try { document.cookie = "OptanonAlertBoxClosed=" + new Date().toISOString() + "; path=/; max-age=31536000"; } catch (e) {}
            for (const sel of ['#onetrust-banner-sdk','#onetrust-consent-sdk','.onetrust-pc-dark-filter','[id^="onetrust"]','div[class*="cookie"]','div[id*="cookie"]']) {
                document.querySelectorAll(sel).forEach(e => e.remove());
            }
            document.body.style.overflow = '';
        }""")
        page.wait_for_timeout(300)
        exito = True

    if exito:
        try:
            COOKIES_FILE.write_text(
                json.dumps({"aceptadas": True, "fecha": datetime.now().isoformat()},
                           ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass
    return exito


# ---------------------------------------------------------------------
# CAPTCHA
# ---------------------------------------------------------------------
def esperar_captcha(page, debug=False, timeout_s=CAPTCHA_TIMEOUT_S):
    def hay_captcha_visible():
        try:
            for sel in [
                'iframe[src*="recaptcha"]',
                'iframe[src*="hcaptcha"]',
                'iframe[title*="captcha" i]',
                'iframe[src*="challenges.cloudflare.com"]',
                '.g-recaptcha',
                '.h-captcha',
                'div[class*="captcha" i]:visible',
            ]:
                el = page.locator(sel).first
                try:
                    if el.count() > 0 and el.is_visible(timeout=300):
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def captcha_marcado():
        try:
            return page.evaluate("""() => {
                const g = document.querySelector('textarea[name="g-recaptcha-response"]');
                const h = document.querySelector('textarea[name="h-captcha-response"]');
                const c = document.querySelector('input[name="cf-turnstile-response"]');
                if (g && g.value) return true;
                if (h && h.value) return true;
                if (c && c.value) return true;
                return false;
            }""")
        except Exception:
            return False

    if not hay_captcha_visible():
        return True

    print("   ⚠️  CAPTCHA detectado. Resuélvelo MANUALMENTE en la ventana del navegador.")
    print(f"      El script esperará hasta {timeout_s // 60} minutos...")

    t0 = time.time()
    while (time.time() - t0) < timeout_s:
        if captcha_marcado() and not hay_captcha_visible():
            print("   ✓ Captcha resuelto, continúo.")
            page.wait_for_timeout(2000)
            return True
        page.wait_for_timeout(2000)

    print("   ✗ Timeout esperando el captcha. Sigo de todas formas.")
    return False


# ---------------------------------------------------------------------
# COMBOS / SELECTS
# ---------------------------------------------------------------------
def opciones_de(page, selector):
    return page.locator(selector).first.evaluate("e => [...e.options].map(o => o.text.trim())")


def opciones_validas(page, selector):
    out = []
    for t in opciones_de(page, selector):
        if t and t.strip().lower() not in ("selecciona", "seleccione", "seleccione...",
                                           "elige", "--", "-", ""):
            out.append(t.strip())
    return out


def elegir_select(page, selector, opcion, obligatorio=True, debug=False):
    ctl = page.locator(selector).first
    if ctl.count() == 0:
        if obligatorio:
            raise RuntimeError(f"No encuentro el <select> {selector}")
        return False

    tag = ctl.evaluate("e => e.tagName")
    if tag != "SELECT":
        ctl.click()
        page.wait_for_timeout(400)
        try:
            page.get_by_role("option", name=re.compile(rf"^\s*{re.escape(opcion)}\s*$", re.I)).first.click()
            page.wait_for_timeout(800)
            return True
        except Exception:
            if obligatorio:
                raise
            return False

    textos = opciones_de(page, selector)
    rx = re.compile(rf"^\s*{re.escape(opcion)}\s*$", re.I)
    for t in textos:
        if rx.match(t):
            ctl.select_option(label=t)
            page.wait_for_timeout(1200)
            if debug:
                print(f"      ✓ {selector} = '{t}'")
            return True

    msg = f"'{opcion}' no está en {selector}: {textos}"
    if obligatorio:
        raise RuntimeError(msg)
    if debug:
        print(f"      (aviso) {msg}")
        for t in textos:
            if t and t.lower() not in ("selecciona", "seleccione", "elige", "--", ""):
                ctl.select_option(label=t)
                page.wait_for_timeout(1200)
                if debug:
                    print(f"      → fallback: {selector} = '{t}'")
                break
    return False


def esperar_opciones(page, selector, timeout_ms=8000):
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        try:
            if opciones_validas(page, selector):
                return True
        except Exception:
            pass
        page.wait_for_timeout(300)
    return False


# ---------------------------------------------------------------------
# FORMULARIO
# ---------------------------------------------------------------------
def rellenar_campo(page, etiqueta_regex, valor, fallbacks, debug=False):
    for sel in fallbacks:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(valor, timeout=2500)
                if debug:
                    print(f"      [fill] '{etiqueta_regex.pattern}' por {sel}")
                return True
        except Exception:
            continue
    try:
        loc = page.get_by_label(etiqueta_regex).first
        loc.fill(valor, timeout=2500)
        return True
    except Exception as e:
        raise RuntimeError(f"No pude rellenar '{etiqueta_regex.pattern}': {e}")


FALLBACK_DNI = ["input#addPaxIpt", 'input[name*="dni" i]', 'input[id*="dni" i]', 'input[placeholder*="dni" i]']
FALLBACK_CLAVE = ["input#addPaxPasswordIpt", 'input[type="password"]', 'input[name*="clave" i]', 'input[id*="clave" i]']


def aplicar_destino_provincia_y_buscar(page, destino, provincia, debug):
    elegir_select(page, "#destination-accreditation", destino, obligatorio=True, debug=debug)
    esperar_opciones(page, "#province-accreditation", timeout_ms=8000)

    if provincia:
        elegir_select(page, "#province-accreditation", provincia, obligatorio=True, debug=debug)
        esperar_opciones(page, "#town-accreditation", timeout_ms=5000)
        try:
            elegir_select(page, "#town-accreditation", LOCALIDAD, obligatorio=False, debug=False)
        except Exception:
            pass

    try:
        elegir_select(page, "#stay-accreditation", NUM_DIAS, obligatorio=False, debug=False)
    except Exception:
        pass

    try:
        page.locator("#product-searcher-btn-accreditation").click(timeout=5000)
    except Exception:
        page.get_by_role("button", name=re.compile(r"buscar|consultar|disponibilidad", re.I)).first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2500)


def flujo_completo(page, destino, provincia, debug, con_login=True):
    print("   → Abriendo la web…")
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(1500)

    cookies(page, debug=debug)
    esperar_captcha(page, debug=debug)
    cookies(page, debug=debug)

    if con_login:
        try:
            page.locator("#addPaxIpt").first.wait_for(state="visible", timeout=10000)
        except Exception:
            pass

        for i, p in enumerate(PASAJEROS, 1):
            print(f"      Login pasajero {i} ({p['dni']})…")
            rellenar_campo(page, re.compile(r"DNI", re.I),   p["dni"],   FALLBACK_DNI,   debug)
            rellenar_campo(page, re.compile(r"Clave", re.I), p["clave"], FALLBACK_CLAVE, debug)
            page.get_by_text("Añadir", exact=False).first.click()
            page.wait_for_timeout(600)
            err = page.get_by_text(re.compile(r"ya ha sido introducido|no es válido", re.I)).first
            if err.count() and err.is_visible():
                raise RuntimeError(f"La web rechaza al pasajero {i} ({p['dni']}): '{err.inner_text()}'")

    if not MASCOTAS:
        try:
            page.locator("input#with-pets-accreditation").uncheck(timeout=2000)
        except Exception:
            pass

    print(f"      Transporte = '{TRANSPORTE}'")
    elegir_select(page, "#transport-accreditation", TRANSPORTE, obligatorio=True, debug=debug)

    try:
        elegir_select(page, "#origin-accreditation", ORIGEN, obligatorio=False, debug=debug)
    except Exception:
        pass

    aplicar_destino_provincia_y_buscar(page, destino, provincia, debug)


# ---------------------------------------------------------------------
# EXTRACCIÓN + FILTRO
# ---------------------------------------------------------------------
JS_DIAS_VERDES = """
() => {
  const meses = /(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\\s+(de\\s+)?\\d{4}/i;
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll('td, button, a, div, span, li')) {
    if (el.children.length > 2) continue;
    const txt = (el.innerText || '').trim();
    if (!/^\\d{1,2}$/.test(txt)) continue;
    const meta = ((el.className || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.title || '')).toLowerCase();
    const bg = (getComputedStyle(el).backgroundColor.match(/\\d+/g) || []).map(Number);
    const verde = bg.length >= 3 && bg[1] > 120 && bg[1] > bg[0] + 40 && bg[1] > bg[2] + 40;
    const marcado = /dispon|avail/.test(meta) && !/no[-_ ]?dispon|unavail|not[-_ ]?avail|agotad|sold|complet/.test(meta);
    if (!(verde || marcado)) continue;
    let mes = '';
    let a = el;
    while (a && !mes) { const m = (a.innerText || '').match(meses); if (m) mes = m[0]; a = a.parentElement; }
    el.setAttribute('data-libre', String(n));
    out.push({ idx: n++, dia: txt, mes, meta: meta.slice(0, 80) });
  }
  return out;
}
"""


def mes_anio(mes_str):
    if not mes_str:
        return None
    s = mes_str.lower()
    m = re.search(r"\b(20\d{2})\b", s)
    anio = int(m.group(1)) if m else None
    mes = None
    for nombre, num in MESES_ES.items():
        if nombre in s:
            mes = num
            break
    return (anio, mes) if anio and mes else None


def descripcion_filtro(destino):
    f = FILTRO_POR_DESTINO.get(destino.upper())
    if f is None:
        return "cualquier mes"
    return f"{f['anio']} (meses {sorted(f['meses'])})"


def detalle_dia(page, idx):
    page.locator(f'[data-libre="{idx}"]').first.click()
    page.wait_for_timeout(1800)
    lineas = [l.strip() for l in page.locator("body").inner_text().splitlines() if l.strip()]
    res = []
    for i, l in enumerate(lineas):
        if "€" in l:
            res.append(" | ".join(lineas[max(0, i - 2): i + 1]))
    return res[:10]


# ---------------------------------------------------------------------
# UNA PASADA DE UN (destino, provincia)
# ---------------------------------------------------------------------
def comprobar_combo(page, destino, provincia, debug, primera=False):
    """
    Devuelve la lista COMPLETA de días (todos los del filtro temporal),
    cada uno con:
      - detalle: TODAS las líneas con €
      - hoteles_objetivo: lista de hoteles de la lista que aparecen
      - es_objetivo: True si hay al menos uno de HOTELES_OBJETIVO
    """
    try:
        if primera:
            flujo_completo(page, destino, provincia, debug, con_login=True)
        else:
            try:
                form_ok = page.locator("#destination-accreditation").first.is_visible(timeout=2000)
            except Exception:
                form_ok = False

            if form_ok:
                print("      (formulario ya cargado: solo cambio destino/provincia y BUSCAR)")
                aplicar_destino_provincia_y_buscar(page, destino, provincia, debug)
            else:
                print("      (no veo el formulario, hago flujo completo)")
                flujo_completo(page, destino, provincia, debug, con_login=False)

        dias = page.evaluate(JS_DIAS_VERDES)

        # Filtro temporal
        filtro = FILTRO_POR_DESTINO.get(destino.upper())
        dias_temporal_ok = []
        for d in dias:
            if filtro is None:
                dias_temporal_ok.append(d)
            else:
                ma = mes_anio(d.get("mes", ""))
                if ma and ma[0] == filtro["anio"] and ma[1] in filtro["meses"]:
                    dias_temporal_ok.append(d)
                elif debug:
                    print(f"      (descartado día {d['dia']} – mes '{d.get('mes') or '?'}')")

        # Cargar detalle de CADA día (todos, para el log)
        dias_todos = []
        for d in dias_temporal_ok:
            detalle = detalle_dia(page, d["idx"])
            hoteles_encontrados = set()
            for linea in detalle:
                h = hotel_match(linea)
                if h:
                    hoteles_encontrados.add(h)
            d["detalle"] = detalle   # TODAS las líneas (para el log y el JSON)
            d["destino"] = destino
            d["provincia"] = provincia
            d["hoteles_objetivo"] = sorted(hoteles_encontrados)
            d["es_objetivo"] = bool(hoteles_encontrados)
            dias_todos.append(d)

        return dias_todos
    except Exception as e:
        print(f"   [combo ERROR] {destino}/{provincia}: {e}", file=sys.stderr)
        try:
            page.screenshot(path=str(OUT_DIR / f"error_{destino}_{provincia}.png"), full_page=True)
        except Exception:
            pass
        return []


# ---------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------
def enviar_email(asunto, cuerpo, debug=False):
    if not (SMTP_USER and SMTP_PASS and EMAIL_DESTINO):
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = EMAIL_DESTINO
        msg["Subject"] = asunto
        msg.attach(MIMEText(cuerpo, "plain", "utf-8"))
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        if debug:
            print(f"[email] enviado a {EMAIL_DESTINO}")
        return True
    except Exception as e:
        print(f"[email] error: {e}", file=sys.stderr)
        return False


def construir_cuerpo(res):
    """
    El email SOLO muestra:
      - Los días que tienen al menos un hotel objetivo.
      - Dentro de cada día, SOLO las líneas con hoteles objetivo (opción A).
    """
    lineas = []
    lineas.append(f"Consulta: {res['fecha_consulta']}")
    objetivos = res.get("disponibles_objetivo", [])
    lineas.append(f"Total días con hoteles objetivo: {len(objetivos)}")
    lineas.append("")

    zonas = {}
    for d in objetivos:
        key = f"{d.get('destino','?')} / {d.get('provincia') or '(sin provincia)'}"
        zonas.setdefault(key, []).append(d)

    if not zonas:
        lineas.append("(Sin disponibilidad en hoteles objetivo.)")
        return "\n".join(lineas)

    for zona, dias in zonas.items():
        lineas.append(f"=== {zona} === ({len(dias)} día/s)")
        for d in dias:
            hoteles = ", ".join(d.get("hoteles_objetivo", []))
            lineas.append(f"  • Día {d['dia']} de {d['mes']}   [{hoteles}]")
            # Opción A: solo líneas con hotel objetivo
            for l in d["detalle"]:
                if hotel_match(l):
                    lineas.append(f"      {l}")
        lineas.append("")
    return "\n".join(lineas)


# ---------------------------------------------------------------------
# FLUJO PRINCIPAL
# ---------------------------------------------------------------------
def comprobar(debug=False, solo=None):
    combos = COMBOS
    if solo:
        combos = [(d, p) for (d, p) in COMBOS if d.upper() == solo.upper()]
        if not combos:
            print(f"[aviso] '{solo}' no está en COMBOS")
            return None

    print("→ Lanzando Chromium…")
    print(f"→ Hoteles objetivo ({len(HOTELES_OBJETIVO)}): {HOTELES_OBJETIVO}")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=False, slow_mo=150 if debug else 0)
        except Exception as e:
            print(f"[FATAL] No se pudo abrir Chromium: {e}", file=sys.stderr)
            return None

        context = browser.new_context(
            viewport={"width": 1400, "height": 1000},
            locale="es-ES",
        )

        if COOKIES_FILE.exists():
            try:
                estado = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
                if estado.get("aceptadas"):
                    context.add_cookies([{
                        "name": "OptanonAlertBoxClosed",
                        "value": datetime.now().isoformat(),
                        "domain": ".mundicolor.es",
                        "path": "/",
                    }])
                    print("→ Cookie 'cookies aceptadas' inyectada antes del primer goto")
            except Exception:
                pass

        page = context.new_page()
        try:
            print(f"\n→ {len(combos)} combinaciones a comprobar (misma pestaña)\n")
            todos = []            # TODOS los días (para el log y el JSON)
            todos_objetivo = []   # Solo días con hotel objetivo (para el email)
            for i, (dest, prov) in enumerate(combos, 1):
                print(f"[{i}/{len(combos)}] {dest} / {prov}  · filtro: {descripcion_filtro(dest)}")
                dias = comprobar_combo(page, dest, prov, debug, primera=(i == 1))

                if not dias:
                    print("      · sin días")
                else:
                    # LOG: mostrar TODOS los días, marcando cuáles son objetivo
                    for d in dias:
                        hoteles = d.get("hoteles_objetivo", [])
                        if hoteles:
                            print(f"      🟢 Día {d['dia']} de {d['mes']}  → OBJETIVO: {', '.join(hoteles)}")
                        else:
                            print(f"      ⚪ Día {d['dia']} de {d['mes']}  (sin hotel objetivo)")
                        # detalle completo en el log (para tranquilidad del usuario)
                        for l in d["detalle"]:
                            prefijo = "          · " if not hotel_match(l) else "          ★ "
                            print(f"{prefijo}{l}")

                todos.extend(dias)
                todos_objetivo.extend([d for d in dias if d.get("es_objetivo")])

            def _filtro_serializable(dest):
                f = FILTRO_POR_DESTINO.get(dest.upper())
                if f is None:
                    return None
                return {"anio": f["anio"], "meses": sorted(f["meses"])}

            resultado = {
                "fecha_consulta": datetime.now().isoformat(timespec="seconds"),
                "hoteles_objetivo": HOTELES_OBJETIVO,
                "combos_comprobados": [
                    {"destino": d, "provincia": p, "filtro": _filtro_serializable(d)}
                    for d, p in combos
                ],
                "disponibles": todos,                 # todos (log/JSON)
                "disponibles_objetivo": todos_objetivo,  # solo objetivo (email)
            }
            (OUT_DIR / "ultimo_resultado.json").write_text(
                json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
            return resultado
        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            return None
        finally:
            page.close()
            context.close()
            browser.close()


def mostrar(res, debug=False):
    if res is None:
        return
    n_total = len(res["disponibles"])
    n_obj = len(res["disponibles_objetivo"])

    print(f"\n[{res['fecha_consulta']}] Días disponibles: {n_total}  ·  con hotel objetivo: {n_obj}")

    if n_obj == 0:
        print("→ Sin hoteles objetivo: no se envía email.")
        if not EMAIL_SOLO_SI_HAY:
            enviar_email(
                "Mundicolor: sin disponibilidad en hoteles objetivo",
                construir_cuerpo(res),
                debug=debug,
            )
        return

    print(f"\n→ ¡HAY {n_obj} día(s) con hoteles objetivo! Enviando email…")
    asunto = f"🟢 Mundicolor: {n_obj} día(s) con hoteles objetivo"
    enviar_email(asunto, construir_cuerpo(res), debug=debug)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, metavar="MIN",
                    help="repetir cada N minutos (recomendado 5)")
    ap.add_argument("--debug", action="store_true",
                    help="navegador visible + capturas")
    ap.add_argument("--solo", metavar="DESTINO",
                    help="comprobar solo un destino, p.ej. --solo BALEARES")
    args = ap.parse_args()

    while True:
        mostrar(comprobar(args.debug, solo=args.solo), debug=args.debug)
        if not args.watch:
            break
        print(f"\n… esperando {args.watch} min (Ctrl-C para salir)")
        time.sleep(max(args.watch, 1) * 60)
