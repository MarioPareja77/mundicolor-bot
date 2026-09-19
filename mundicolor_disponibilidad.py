"""
Comprueba la disponibilidad en:
  1) https://www.mundicolor.es/availability      (Imserso / Mundicolor)
  2) https://www.turismosocial.es/availability   (Turismo Social)

Uso:
    python mundicolor_disponibilidad.py                     # una pasada
    python mundicolor_disponibilidad.py --watch 5           # repite cada 5 min
    python mundicolor_disponibilidad.py --debug             # navegador visible + capturas
"""
import argparse
import json
import os
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

URL_MUNDICOLOR     = "https://www.mundicolor.es/availability"
URL_TURISMOSOCIAL  = "https://www.turismosocial.es/availability"
URL = URL_MUNDICOLOR  # alias legacy

# ----------------------------- DATOS ---------------------------------
PASAJEROS = [
    {"dni": "40428643W", "clave": "6075"},
    {"dni": "53072868T", "clave": "6075"},
]
MASCOTAS = False
TRANSPORTE = "Sin transporte"
ORIGEN = "Selecciona"
LOCALIDAD = "Cualquiera"
NUM_DIAS = "Cualquiera"
# ---------------------------------------------------------------------

# -------------------- COMBOS MUNDICOLOR ------------------------------
COMBOS = [
    ("BALEARES", "MALLORCA"),
    ("BALEARES", "MENORCA"),
    ("BALEARES", "IBIZA"),
    ("CANARIAS", "LANZAROTE"),
    ("CANARIAS", "TENERIFE"),
    ("CANARIAS", "FUERTEVENTURA"),
]
# ---------------------------------------------------------------------

# -------------------- COMBOS TURISMOSOCIAL ---------------------------
ZONAS_TURISMOSOCIAL = ["Capitales de provincia", "Costas"]
PROVINCIAS_TURISMOSOCIAL = [
    "Alicante",
    "Almería",
    "Cadiz",
    "Gran Canaria",
    "Granada",
    "Huelva",
    "I. Mallorca",
    "Madrid",
    "Málaga",
    "Murcia",
    "Santa Cruz de Tenerife",
]
MESES_TURISMOSOCIAL = {3, 4, 5, 6}
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

PROBAR_EMAIL_SIN_FILTRO = True
HACER_RESERVA = True
PROBAR_RESERVA_SIN_FILTRO = True
# ---------------------------------------------------------------------

FILTRO_POR_DESTINO = {
    "BALEARES": {"anio": 2027, "meses": {3, 4, 5, 6, 7, 8, 9, 10}},
    "CANARIAS": None,
}

FILTRO_RESERVA_POR_DESTINO = {
    "BALEARES": {"anio": 2027, "meses": {4, 5, 6}},
    "CANARIAS": {"anio": 2027, "meses": {3, 4, 5, 6, 10, 11}},
}

FILTRO_RESERVA_POR_PROVINCIA = {
    ("BALEARES", "IBIZA"):   {"anio": 2027, "meses": {4, 5, 6}},
    ("BALEARES", "MENORCA"): {"anio": 2027, "meses": {4, 5, 6}},
}
# ---------------------------------------------------------------------

MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_USER     = os.environ.get("SMTP_USER", "marioparejanieto@gmail.com")
SMTP_PASS     = os.environ.get("SMTP_PASS", "wnfx ldkt oomn yqoi")
EMAIL_DESTINO = os.environ.get("EMAIL_DESTINO", "marioparejanieto@gmail.com")
EMAIL_SOLO_SI_HAY = True

CAPTCHA_TIMEOUT_S = 300

OUT_DIR = Path(__file__).parent / "mundicolor_out"
OUT_DIR.mkdir(exist_ok=True)
COOKIES_FILE = OUT_DIR / "cookies_ok.json"


def shot(page, name, debug):
    if debug:
        try:
            page.screenshot(path=str(OUT_DIR / f"{name}.png"), full_page=True)
        except Exception:
            pass


# ---------------------------------------------------------------------
# DEBUG DUMP
# ---------------------------------------------------------------------
def _dump_debug(page, name):
    """Guarda HTML + screenshot + lista de todos los <select> de todos los frames."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = OUT_DIR / f"debug_{name}_{ts}"
        try:
            base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
            print(f"      [debug] HTML guardado en {base}.html")
        except Exception as e:
            print(f"      [debug] no pude guardar HTML: {e}")
        try:
            page.screenshot(path=str(base) + ".png", full_page=True)
            print(f"      [debug] PNG guardado en {base}.png")
        except Exception:
            pass

        for fi, frame in enumerate(page.frames):
            try:
                info = frame.evaluate("""() => [...document.querySelectorAll('select')].map(s => ({
                    id: s.id, name: s.name,
                    options: [...s.options].map(o => o.text.trim()).slice(0,40),
                    visible: !!(s.offsetWidth || s.offsetHeight)
                }))""")
                if info:
                    print(f"      [debug] frame {fi} ({frame.url}):")
                    for s in info:
                        print(f"        select id={s['id']!r} name={s['name']!r} "
                              f"visible={s['visible']} opts={s['options']}")
                else:
                    print(f"      [debug] frame {fi} ({frame.url}): sin <select>")
            except Exception as e:
                print(f"      [debug] no pude volcar frame {fi}: {e}")
    except Exception as e:
        print(f"      [debug] dump falló: {e}")


# ---------------------------------------------------------------------
# HOTELES
# ---------------------------------------------------------------------
def normalizar(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


HOTELES_OBJETIVO_NORM = [normalizar(h) for h in HOTELES_OBJETIVO]


def hotel_match(linea):
    if PROBAR_EMAIL_SIN_FILTRO:
        return "PRUEBA"
    n = normalizar(linea)
    if not n:
        return None
    for h_orig, h_norm in zip(HOTELES_OBJETIVO, HOTELES_OBJETIVO_NORM):
        if re.search(r"(?<![a-z])" + re.escape(h_norm) + r"(?![a-z])", n):
            return h_orig
    return None


def hotel_match_estricto(linea):
    n = normalizar(linea)
    if not n:
        return None
    for h_orig, h_norm in zip(HOTELES_OBJETIVO, HOTELES_OBJETIVO_NORM):
        if re.search(r"(?<![a-z])" + re.escape(h_norm) + r"(?![a-z])", n):
            return h_orig
    return None


def debe_reservar(destino, provincia, mes_str):
    f = FILTRO_RESERVA_POR_PROVINCIA.get((destino.upper(), (provincia or "").upper()))
    if f is None:
        f = FILTRO_RESERVA_POR_DESTINO.get(destino.upper())
    if f is None:
        return False
    ma = mes_anio(mes_str)
    if not ma:
        return False
    anio, mes = ma
    return anio == f["anio"] and mes in f["meses"]


def debe_reservar_turismosocial(mes_str):
    ma = mes_anio(mes_str)
    if not ma:
        return False
    return ma[1] in MESES_TURISMOSOCIAL


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
            for sel in ['iframe[src*="recaptcha"]','iframe[src*="hcaptcha"]',
                        'iframe[title*="captcha" i]','iframe[src*="challenges.cloudflare.com"]',
                        '.g-recaptcha','.h-captcha','div[class*="captcha" i]:visible']:
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
# SELECTS (helpers básicos)
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
# SELECTS robustos: buscar por contenido de las opciones (para TS)
# ---------------------------------------------------------------------
def _iter_selects(page):
    """Genera (frame, locator_select, index) para todos los <select> visibles."""
    for frame in page.frames:
        try:
            n = frame.locator("select").count()
        except Exception:
            continue
        for i in range(n):
            yield frame, frame.locator("select").nth(i), i


def encontrar_select_por_opcion(page, opcion,
                                regex_fallback=None, timeout_ms=10000):
    """
    Busca un <select> en cualquier frame cuya lista de <option> contenga
    una coincidencia exacta (case-insensitive) con `opcion`, o una
    coincidencia por regex si se pasa `regex_fallback`.
    Devuelve (locator, texto_exacto_opcion) o (None, None).
    """
    rx_exact = re.compile(rf"^\s*{re.escape(opcion)}\s*$", re.I)
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        for _frame, sel, _i in _iter_selects(page):
            try:
                if not sel.is_visible():
                    continue
                opts = sel.evaluate("e => [...e.options].map(o => o.text.trim())")
            except Exception:
                continue
            for t in opts:
                if rx_exact.match(t):
                    return sel, t
            if regex_fallback:
                for t in opts:
                    if re.search(regex_fallback, t, re.I):
                        return sel, t
        page.wait_for_timeout(400)
    return None, None


def elegir_por_contenido(page, opcion, etiqueta,
                         obligatorio=True, debug=False, regex_fallback=None):
    sel, texto = encontrar_select_por_opcion(
        page, opcion, regex_fallback=regex_fallback, timeout_ms=10000)
    if sel is None:
        if obligatorio:
            print(f"      ✗ No encuentro ningún <select> con opción '{opcion}' ({etiqueta})")
            _dump_debug(page, f"no_select_{etiqueta}")
            raise RuntimeError(
                f"No encuentro ningún <select> con la opción '{opcion}' ({etiqueta})")
        return False
    try:
        sel.select_option(label=texto)
        page.wait_for_timeout(1200)
        if debug:
            print(f"      ✓ {etiqueta} = '{texto}'")
        return True
    except Exception as e:
        if obligatorio:
            _dump_debug(page, f"select_fail_{etiqueta}")
            raise RuntimeError(f"Fallo al seleccionar '{texto}' en {etiqueta}: {e}")
        return False


# ---------------------------------------------------------------------
# BOTÓN BUSCAR flexible (para TS)
# ---------------------------------------------------------------------
CAND_BUSCAR_TS = ["#product-searcher-btn-accreditation", "#product-searcher-btn",
                  'button:has-text("Buscar")', 'button:has-text("Consultar")',
                  'button:has-text("Disponibilidad")',
                  'button[type="submit"]']


def _pulsar_buscar_flexible(page, debug=False):
    for sel in CAND_BUSCAR_TS:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=5000)
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(2500)
                return True
        except Exception:
            continue
    try:
        page.get_by_role(
            "button",
            name=re.compile(r"buscar|consultar|disponibilidad", re.I)
        ).first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2500)
        return True
    except Exception as e:
        print(f"      ✗ No encuentro botón BUSCAR: {e}")
        _dump_debug(page, "no_boton_buscar")
        return False


# ---------------------------------------------------------------------
# FORMULARIO (helpers)
# ---------------------------------------------------------------------
def rellenar_campo(page, etiqueta_regex, valor, fallbacks, debug=False):
    for sel in fallbacks:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(valor, timeout=2500)
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


def _login_pasajeros(page, debug=False):
    try:
        page.locator("#addPaxIpt").first.wait_for(state="visible", timeout=15000)
    except Exception:
        if debug:
            print("      (no veo #addPaxIpt, intento igualmente rellenar por fallbacks)")

    for i, p in enumerate(PASAJEROS, 1):
        print(f"      Login pasajero {i} ({p['dni']})…")
        rellenar_campo(page, re.compile(r"DNI", re.I),   p["dni"],   FALLBACK_DNI,   debug)
        rellenar_campo(page, re.compile(r"Clave", re.I), p["clave"], FALLBACK_CLAVE, debug)
        page.get_by_text("Añadir", exact=False).first.click()
        page.wait_for_timeout(600)
        err = page.get_by_text(re.compile(r"ya ha sido introducido|no es válido", re.I)).first
        if err.count() and err.is_visible():
            raise RuntimeError(f"La web rechaza al pasajero {i} ({p['dni']}): '{err.inner_text()}'")


def _desmarcar_mascotas(page):
    if not MASCOTAS:
        try:
            page.locator("input#with-pets-accreditation").uncheck(timeout=2000)
        except Exception:
            pass


def _pulsar_buscar(page):
    try:
        page.locator("#product-searcher-btn-accreditation").click(timeout=5000)
    except Exception:
        page.get_by_role("button", name=re.compile(r"buscar|consultar|disponibilidad", re.I)).first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2500)


# ------------------ MUNDICOLOR: aplicar combos ----------------------
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

    _pulsar_buscar(page)


def flujo_completo(page, destino, provincia, debug, con_login=True):
    print("   → Abriendo Mundicolor…")
    page.goto(URL_MUNDICOLOR, wait_until="networkidle")
    page.wait_for_timeout(1500)

    cookies(page, debug=debug)
    esperar_captcha(page, debug=debug)
    cookies(page, debug=debug)

    if con_login:
        _login_pasajeros(page, debug)

    _desmarcar_mascotas(page)

    print(f"      Transporte = '{TRANSPORTE}'")
    elegir_select(page, "#transport-accreditation", TRANSPORTE, obligatorio=True, debug=debug)

    try:
        elegir_select(page, "#origin-accreditation", ORIGEN, obligatorio=False, debug=debug)
    except Exception:
        pass

    aplicar_destino_provincia_y_buscar(page, destino, provincia, debug)


# ------------- TURISMOSOCIAL: flujo y combos -------------------------
def _regex_flexible_tildes(s):
    """Convierte 'Cadiz' → 'C[aá]d[ií]z', 'Malaga' → 'M[aá]l[aá]g[aá]', etc."""
    subs = {"a": "[aá]", "e": "[eé]", "i": "[ií]", "o": "[oó]", "u": "[uú]",
            "A": "[aáAÁ]", "E": "[eéEÉ]", "I": "[iíIÍ]", "O": "[oóOÓ]", "U": "[uúUÚ]"}
    return "".join(subs.get(c, re.escape(c)) for c in s)


def flujo_completo_turismosocial(page, zona, provincia, debug, con_login=True):
    print("   → Abriendo TurismoSocial…")
    page.goto(URL_TURISMOSOCIAL, wait_until="networkidle")
    page.wait_for_timeout(2000)

    cookies(page, debug=debug)
    esperar_captcha(page, debug=debug)
    cookies(page, debug=debug)

    if con_login:
        _login_pasajeros(page, debug)

    _desmarcar_mascotas(page)

    # Esperar activamente a que exista al menos un <select> visible
    print("      Esperando a que cargue el formulario…")
    t0 = time.time()
    while (time.time() - t0) < 15:
        try:
            n_vis = page.locator("select:visible").count()
        except Exception:
            n_vis = 0
        if n_vis > 0:
            break
        page.wait_for_timeout(500)

    if debug:
        print("      [debug] <select> presentes ahora:")
        for _fr, sel, _i in _iter_selects(page):
            try:
                opts = sel.evaluate("e => [...e.options].map(o => o.text.trim()).slice(0,10)")
                tag_id = sel.evaluate("e => (e.id||'')+'|'+(e.name||'')")
                print(f"        id|name={tag_id!r} opts[:10]={opts}")
            except Exception:
                pass

    # --- Transporte: por contenido (no por id) ---
    print(f"      Transporte = '{TRANSPORTE}'")
    try:
        ok = elegir_por_contenido(
            page, TRANSPORTE, "Transporte",
            obligatorio=True, debug=debug,
            regex_fallback=r"sin\s+transporte")
        if not ok:
            raise RuntimeError("Transporte no encontrado")
    except Exception as e:
        print(f"      ✗ Transporte: {e}")
        return False

    # --- Origen (opcional) ---
    try:
        elegir_por_contenido(
            page, ORIGEN, "Origen",
            obligatorio=False, debug=False,
            regex_fallback=r"selecciona")
    except Exception:
        pass

    # --- Zona de destino ---
    print(f"      Zona de destino = '{zona}'")
    try:
        regex_zona = None
        if "Capitales" in zona:
            regex_zona = r"capital(es)?\s+de\s+provincia"
        elif "Costa" in zona:
            regex_zona = r"costa"
        ok = elegir_por_contenido(
            page, zona, "ZonaDestino",
            obligatorio=True, debug=debug,
            regex_fallback=regex_zona)
        if not ok:
            raise RuntimeError("Zona no encontrada")
    except Exception as e:
        print(f"      ✗ Zona '{zona}': {e}")
        return False

    page.wait_for_timeout(1500)

    # --- Provincia ---
    print(f"      Provincia = '{provincia}'")
    try:
        base = provincia[3:] if provincia.startswith("I. ") else provincia
        regex_prov = _regex_flexible_tildes(base)
        ok = elegir_por_contenido(
            page, provincia, "Provincia",
            obligatorio=True, debug=debug,
            regex_fallback=regex_prov)
        if not ok:
            raise RuntimeError("Provincia no encontrada")
    except Exception as e:
        print(f"      ✗ Provincia '{provincia}' en zona '{zona}': {e}")
        return False

    # NO tocar Localidad ni No días (dejar por defecto)

    if not _pulsar_buscar_flexible(page, debug):
        return False
    return True


# ---------------------------------------------------------------------
# EXTRACCIÓN
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


def descripcion_filtro_reserva(destino, provincia=None):
    f = None
    if provincia:
        f = FILTRO_RESERVA_POR_PROVINCIA.get((destino.upper(), provincia.upper()))
    if f is None:
        f = FILTRO_RESERVA_POR_DESTINO.get(destino.upper())
    if f is None:
        return "sin reserva"
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
# RESERVA
# ---------------------------------------------------------------------
def intentar_reserva(page, context, linea_hotel, debug=False):
    print(f"      → Intentando reserva para: {linea_hotel[:80]}...")

    try:
        partes = [p.strip() for p in linea_hotel.split("|")]
        nombre_hotel = partes[1] if len(partes) > 1 else partes[0]
        print(f"      (nombre a buscar: '{nombre_hotel}')")

        fila = None
        for sel in ["tr", "li", "div"]:
            loc = page.locator(sel).filter(has_text=re.compile(re.escape(nombre_hotel), re.I))
            if loc.count() > 0:
                fila = loc.last
                break

        if not fila:
            print("      ✗ No encuentro la fila del hotel")
            return False

        btn_sel = fila.locator(
            "button, a, input[type='button'], input[type='submit'], [role='button']"
        ).filter(has_text=re.compile(r"seleccionar", re.I)).first

        if btn_sel.count() == 0:
            btn_sel = fila.get_by_role("button", name=re.compile(r"seleccionar", re.I)).first

        if btn_sel.count() == 0:
            print("      ✗ No encuentro botón SELECCIONAR en la fila")
            if debug:
                shot(page, "04_sin_boton_seleccionar", debug)
            return False

        print("      → Pulsando SELECCIONAR...")
        try:
            with context.expect_page(timeout=8000) as nueva_info:
                btn_sel.click()
            nueva = nueva_info.value
            print("      ✓ Nueva pestaña abierta")
            usa_nueva_pestana = True
        except PWTimeout:
            print("      (no se abrió nueva pestaña, asumo navegación en la misma)")
            page.wait_for_load_state("networkidle", timeout=15000)
            nueva = page
            usa_nueva_pestana = False

        nueva.wait_for_timeout(2500)
        if debug:
            shot(nueva, "04_pagina_reserva", debug)

        checkboxes = nueva.locator("input[type='checkbox']")
        n = checkboxes.count()
        print(f"      Checkboxes encontradas: {n}")
        marcadas = 0
        for i in range(n):
            cb = checkboxes.nth(i)
            try:
                if cb.is_visible() and not cb.is_checked():
                    cb.check(timeout=3000)
                    marcadas += 1
                    print(f"      ✓ Checkbox {i+1} marcada")
            except Exception as e:
                print(f"      (checkbox {i+1} no marcada: {e})")

        nueva.wait_for_timeout(1000)
        if debug:
            shot(nueva, "05_checkboxes_marcadas", debug)

        print("      → Buscando FINALIZAR RESERVA...")
        btn_final = nueva.locator(
            "button, a, input[type='button'], input[type='submit'], [role='button']"
        ).filter(has_text=re.compile(r"finalizar\s+reserva", re.I)).first

        if btn_final.count() == 0:
            print("      ✗ No encuentro botón FINALIZAR RESERVA")
            if debug:
                shot(nueva, "05_sin_boton_finalizar", debug)
            if usa_nueva_pestana:
                try:
                    nueva.close()
                except Exception:
                    pass
            return False

        btn_final.click(timeout=5000)
        print("      ✓ FINALIZAR RESERVA pulsado")
        nueva.wait_for_timeout(3000)
        if debug:
            shot(nueva, "06_finalizado", debug)

        if usa_nueva_pestana:
            try:
                nueva.close()
            except Exception:
                pass

        return True
    except Exception as e:
        print(f"      [RESERVA ERROR] {e}")
        if debug:
            try:
                shot(page, "error_reserva", debug)
            except Exception:
                pass
        return False


# ---------------------------------------------------------------------
# MUNDICOLOR: una pasada de un combo
# ---------------------------------------------------------------------
def comprobar_combo(page, context, destino, provincia, debug, primera=False):
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

        filtro = FILTRO_POR_DESTINO.get(destino.upper())
        dias_temporal_ok = []
        for d in dias:
            if filtro is None:
                dias_temporal_ok.append(d)
            else:
                ma = mes_anio(d.get("mes", ""))
                if ma and ma[0] == filtro["anio"] and ma[1] in filtro["meses"]:
                    dias_temporal_ok.append(d)

        dias_todos = []
        for d in dias_temporal_ok:
            detalle = detalle_dia(page, d["idx"])
            hoteles_encontrados = set()
            for linea in detalle:
                h = hotel_match(linea)
                if h:
                    hoteles_encontrados.add(h)
            d["detalle"] = detalle
            d["destino"] = destino
            d["provincia"] = provincia
            d["hoteles_objetivo"] = sorted(hoteles_encontrados)
            d["es_objetivo"] = bool(hoteles_encontrados)
            dias_todos.append(d)

            if HACER_RESERVA:
                if not debe_reservar(destino, provincia, d.get("mes", "")):
                    if debug:
                        print(f"      · Reserva NO: día {d['dia']} de {d['mes']} no cumple filtro "
                              f"({descripcion_filtro_reserva(destino, provincia)})")
                    continue

                lineas_a_reservar = []
                if PROBAR_RESERVA_SIN_FILTRO:
                    if detalle:
                        lineas_a_reservar = [detalle[0]]
                else:
                    for linea in detalle:
                        if hotel_match_estricto(linea):
                            lineas_a_reservar = [linea]
                            break

                if not lineas_a_reservar:
                    if debug:
                        print(f"      · Reserva NO: día {d['dia']} sin hotel que cumpla")
                    continue

                for linea in lineas_a_reservar:
                    print(f"      ↪ [MUNDICOLOR] Reserva día {d['dia']} de {d['mes']} "
                          f"({destino}/{provincia}, filtro {descripcion_filtro_reserva(destino, provincia)})")
                    ok = intentar_reserva(page, context, linea, debug)
                    print("      ✓ Reserva completada" if ok else "      ✗ Reserva fallida")

                    try:
                        if not page.locator("#destination-accreditation").first.is_visible(timeout=1500):
                            print("      (la página ha cambiado tras la reserva; siguiente combo hará flujo completo)")
                            return dias_todos
                    except Exception:
                        pass

        return dias_todos
    except Exception as e:
        print(f"   [combo ERROR] {destino}/{provincia}: {e}", file=sys.stderr)
        try:
            page.screenshot(path=str(OUT_DIR / f"error_{destino}_{provincia}.png"), full_page=True)
        except Exception:
            pass
        return []


# ---------------------------------------------------------------------
# TURISMOSOCIAL: una pasada de un combo
# ---------------------------------------------------------------------
def comprobar_combo_turismosocial(page, context, zona, provincia, debug, primera=False):
    try:
        if primera:
            ok = flujo_completo_turismosocial(page, zona, provincia, debug, con_login=True)
            if not ok:
                return []
        else:
            try:
                hay_form = page.locator("select:visible").count() > 0
            except Exception:
                hay_form = False

            if hay_form:
                print("      (formulario ya cargado: solo cambio zona/provincia y BUSCAR)")
                try:
                    regex_zona = (r"capital(es)?\s+de\s+provincia" if "Capitales" in zona
                                  else r"costa")
                    elegir_por_contenido(
                        page, zona, "ZonaDestino",
                        obligatorio=True, debug=debug,
                        regex_fallback=regex_zona)
                    page.wait_for_timeout(1500)
                    base = provincia[3:] if provincia.startswith("I. ") else provincia
                    regex_prov = _regex_flexible_tildes(base)
                    elegir_por_contenido(
                        page, provincia, "Provincia",
                        obligatorio=True, debug=debug,
                        regex_fallback=regex_prov)
                    _pulsar_buscar_flexible(page, debug)
                except Exception as e:
                    print(f"      (reintento TS falló: {e}; hago flujo completo sin login)")
                    if not flujo_completo_turismosocial(page, zona, provincia, debug, con_login=False):
                        return []
            else:
                print("      (no veo el formulario, hago flujo completo sin login)")
                if not flujo_completo_turismosocial(page, zona, provincia, debug, con_login=False):
                    return []

        dias = page.evaluate(JS_DIAS_VERDES)

        dias_temporal_ok = []
        for d in dias:
            if debe_reservar_turismosocial(d.get("mes", "")):
                dias_temporal_ok.append(d)

        dias_todos = []
        for d in dias_temporal_ok:
            detalle = detalle_dia(page, d["idx"])
            hoteles_encontrados = set()
            for linea in detalle:
                h = hotel_match(linea)
                if h:
                    hoteles_encontrados.add(h)
            d["detalle"] = detalle
            d["destino"] = zona
            d["provincia"] = provincia
            d["hoteles_objetivo"] = sorted(hoteles_encontrados)
            d["es_objetivo"] = bool(hoteles_encontrados)
            dias_todos.append(d)

            if HACER_RESERVA and debe_reservar_turismosocial(d.get("mes", "")):
                lineas_a_reservar = []
                if PROBAR_RESERVA_SIN_FILTRO:
                    if detalle:
                        lineas_a_reservar = [detalle[0]]
                else:
                    for linea in detalle:
                        if hotel_match_estricto(linea):
                            lineas_a_reservar = [linea]
                            break

                if not lineas_a_reservar:
                    if debug:
                        print(f"      · Reserva TS NO: día {d['dia']} sin hotel que cumpla")
                    continue

                for linea in lineas_a_reservar:
                    print(f"      ↪ [TURISMOSOCIAL] Reserva día {d['dia']} de {d['mes']} "
                          f"({zona}/{provincia}, meses {sorted(MESES_TURISMOSOCIAL)})")
                    ok = intentar_reserva(page, context, linea, debug)
                    print("      ✓ Reserva completada" if ok else "      ✗ Reserva fallida")

                    try:
                        if page.locator("select:visible").count() == 0:
                            print("      (la página ha cambiado tras la reserva; siguiente combo hará flujo completo)")
                            return dias_todos
                    except Exception:
                        pass

        return dias_todos
    except Exception as e:
        print(f"   [combo TS ERROR] {zona}/{provincia}: {e}", file=sys.stderr)
        try:
            page.screenshot(path=str(OUT_DIR / f"error_ts_{zona}_{provincia}.png"), full_page=True)
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


def _seccion_cuerpo(res_seccion, titulo):
    """
    Devuelve el bloque de una web (Mundicolor o TurismoSocial) SIN repetir
    el título (ya lo pone construir_cuerpo_combinado) y SIN contadores ni
    el aviso de MODO PRUEBA. Solo lista las zonas y los hoteles encontrados.
    """
    lineas = []
    objetivos = (res_seccion or {}).get("disponibles_objetivo", [])

    zonas = {}
    for d in objetivos:
        key = f"{d.get('destino','?')} / {d.get('provincia') or '(sin provincia)'}"
        zonas.setdefault(key, []).append(d)

    if not zonas:
        lineas.append("(Sin disponibilidad en hoteles objetivo.)")
        return "\n".join(lineas)

    for zona, dias in zonas.items():
        lineas.append(f"--- {zona} ---")
        for d in dias:
            hoteles = ", ".join(d.get("hoteles_objetivo", []))
            lineas.append(f"  • Día {d['dia']} de {d['mes']}   [{hoteles}]")
            for l in d["detalle"]:
                if hotel_match(l):
                    lineas.append(f"      {l}")
        lineas.append("")
    return "\n".join(lineas)


def construir_cuerpo_combinado(res):
    sep = "=" * 60
    partes = [
        f"Consulta: {res['fecha_consulta']}",
        "",
        sep,
        "MUNDICOLOR",
        sep,
        _seccion_cuerpo(res.get("mundicolor"), "MUNDICOLOR"),
        "",
        sep,
        "TURISMOSOCIAL",
        sep,
        _seccion_cuerpo(res.get("turismosocial"), "TURISMOSOCIAL"),
    ]
    return "\n".join(partes)


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
    print("→ Filtro RESERVA por destino (Mundicolor):")
    for dest in ("BALEARES", "CANARIAS"):
        print(f"     {dest}: {descripcion_filtro_reserva(dest)}")
        for (d, p), _f in FILTRO_RESERVA_POR_PROVINCIA.items():
            if d == dest:
                print(f"       · {p}: {descripcion_filtro_reserva(d, p)}")
    print(f"→ TurismoSocial: meses de interés = {sorted(MESES_TURISMOSOCIAL)}")
    if PROBAR_EMAIL_SIN_FILTRO:
        print("⚠️  PROBAR_EMAIL_SIN_FILTRO = True → email aunque no haya hoteles objetivo")
    if HACER_RESERVA:
        print("⚠️  HACER_RESERVA = True → se intentará reservar")
        if PROBAR_RESERVA_SIN_FILTRO:
            print("⚠️  PROBAR_RESERVA_SIN_FILTRO = True → reserva con cualquier hotel")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True, slow_mo=0)
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
            print(f"\n{'=' * 25} MUNDICOLOR {'=' * 25}")
            print(f"→ {len(combos)} combinaciones a comprobar (misma pestaña)\n")
            todos = []
            todos_objetivo = []
            for i, (dest, prov) in enumerate(combos, 1):
                print(f"[{i}/{len(combos)}] {dest} / {prov}  · filtro email: {descripcion_filtro(dest)}"
                      f"  · filtro reserva: {descripcion_filtro_reserva(dest, prov)}")
                dias = comprobar_combo(page, context, dest, prov, debug, primera=(i == 1))

                if not dias:
                    print("      · sin días")
                else:
                    for d in dias:
                        hoteles = d.get("hoteles_objetivo", [])
                        if hoteles:
                            print(f"      🟢 Día {d['dia']} de {d['mes']}  → OBJETIVO: {', '.join(hoteles)}")
                        else:
                            print(f"      ⚪ Día {d['dia']} de {d['mes']}  (sin hotel objetivo)")
                        for l in d["detalle"]:
                            prefijo = "          · " if not hotel_match(l) else "          ★ "
                            print(f"{prefijo}{l}")

                todos.extend(dias)
                todos_objetivo.extend([d for d in dias if d.get("es_objetivo")])

            def _filtro_serializable(f):
                if f is None:
                    return None
                return {"anio": f["anio"], "meses": sorted(f["meses"])}

            resultado_mundi = {
                "fecha_consulta": datetime.now().isoformat(timespec="seconds"),
                "hoteles_objetivo": HOTELES_OBJETIVO,
                "modo_prueba_sin_filtro": PROBAR_EMAIL_SIN_FILTRO,
                "hacer_reserva": HACER_RESERVA,
                "probar_reserva_sin_filtro": PROBAR_RESERVA_SIN_FILTRO,
                "filtro_reserva_por_destino": {
                    k: _filtro_serializable(v) for k, v in FILTRO_RESERVA_POR_DESTINO.items()
                },
                "filtro_reserva_por_provincia": {
                    f"{k[0]}/{k[1]}": _filtro_serializable(v)
                    for k, v in FILTRO_RESERVA_POR_PROVINCIA.items()
                },
                "combos_comprobados": [
                    {"destino": d, "provincia": p,
                     "filtro_email": _filtro_serializable(FILTRO_POR_DESTINO.get(d.upper())),
                     "filtro_reserva": _filtro_serializable(
                         FILTRO_RESERVA_POR_PROVINCIA.get((d.upper(), p.upper()))
                         or FILTRO_RESERVA_POR_DESTINO.get(d.upper())
                     )}
                    for d, p in combos
                ],
                "disponibles": todos,
                "disponibles_objetivo": todos_objetivo,
            }

            if solo:
                print("\n(flag --solo activo: se omite TurismoSocial)")
                resultado_ts = None
            else:
                combos_ts = [(z, p) for z in ZONAS_TURISMOSOCIAL for p in PROVINCIAS_TURISMOSOCIAL]
                print(f"\n{'=' * 25} TURISMOSOCIAL {'=' * 25}")
                print(f"→ {len(combos_ts)} combinaciones a comprobar (misma pestaña)\n")

                todos_ts = []
                todos_objetivo_ts = []
                for i, (zona, prov) in enumerate(combos_ts, 1):
                    print(f"[{i}/{len(combos_ts)}] {zona} / {prov}  · filtro reserva: meses {sorted(MESES_TURISMOSOCIAL)}")
                    dias = comprobar_combo_turismosocial(
                        page, context, zona, prov, debug, primera=(i == 1)
                    )

                    if not dias:
                        print("      · sin días")
                    else:
                        for d in dias:
                            hoteles = d.get("hoteles_objetivo", [])
                            if hoteles:
                                print(f"      🟢 Día {d['dia']} de {d['mes']}  → OBJETIVO: {', '.join(hoteles)}")
                            else:
                                print(f"      ⚪ Día {d['dia']} de {d['mes']}  (sin hotel objetivo)")
                            for l in d["detalle"]:
                                prefijo = "          · " if not hotel_match(l) else "          ★ "
                                print(f"{prefijo}{l}")

                    todos_ts.extend(dias)
                    todos_objetivo_ts.extend([d for d in dias if d.get("es_objetivo")])

                resultado_ts = {
                    "fecha_consulta": datetime.now().isoformat(timespec="seconds"),
                    "hoteles_objetivo": HOTELES_OBJETIVO,
                    "modo_prueba_sin_filtro": PROBAR_EMAIL_SIN_FILTRO,
                    "hacer_reserva": HACER_RESERVA,
                    "probar_reserva_sin_filtro": PROBAR_RESERVA_SIN_FILTRO,
                    "meses_interes": sorted(MESES_TURISMOSOCIAL),
                    "combos_comprobados": [
                        {"destino": z, "provincia": p} for z, p in combos_ts
                    ],
                    "disponibles": todos_ts,
                    "disponibles_objetivo": todos_objetivo_ts,
                }

            resultado = {
                "fecha_consulta": datetime.now().isoformat(timespec="seconds"),
                "mundicolor": resultado_mundi,
                "turismosocial": resultado_ts,
            }
            (OUT_DIR / "ultimo_resultado.json").write_text(
                json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
            return resultado
        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


def mostrar(res, debug=False):
    if res is None:
        return
    res_mundi = res.get("mundicolor") or {}
    res_ts = res.get("turismosocial") or {}

    n_obj_mundi = len(res_mundi.get("disponibles_objetivo", []))
    n_obj_ts = len(res_ts.get("disponibles_objetivo", [])) if res_ts else 0
    n_obj_total = n_obj_mundi + n_obj_ts

    print(f"\n[{res['fecha_consulta']}]")
    print(f"  Mundicolor    : {n_obj_mundi} día(s) con hotel objetivo")
    print(f"  TurismoSocial : {n_obj_ts} día(s) con hotel objetivo")

    ASUNTO = "[IMSERSO 2027] Disponibilidad hoteles islas y península"

    if n_obj_total == 0 and not PROBAR_EMAIL_SIN_FILTRO:
        print("→ Sin hoteles objetivo en ninguna web: no se envía email.")
        if not EMAIL_SOLO_SI_HAY:
            enviar_email(ASUNTO, construir_cuerpo_combinado(res), debug=debug)
        return

    print("\n→ Enviando email resumen (Mundicolor + TurismoSocial)…")
    enviar_email(ASUNTO, construir_cuerpo_combinado(res), debug=debug)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, metavar="MIN",
                    help="repetir cada N minutos (recomendado 5)")
    ap.add_argument("--debug", action="store_true",
                    help="navegador visible + capturas")
    ap.add_argument("--solo", metavar="DESTINO",
                    help="comprobar solo un destino de Mundicolor, p.ej. --solo BALEARES "
                         "(omite TurismoSocial)")
    args = ap.parse_args()

    while True:
        mostrar(comprobar(args.debug, solo=args.solo), debug=args.debug)
        if not args.watch:
            break
        print(f"\n… esperando {args.watch} min (Ctrl-C para salir)")
        time.sleep(max(args.watch, 1) * 60)
