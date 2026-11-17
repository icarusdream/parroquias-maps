"""Piloto Apify: busca la página de Facebook de cada parroquia y lee su información pública de contacto.
Corre en GitHub Actions con el secreto APIFY_TOKEN. Nunca imprime ni guarda el token.
Uso: python apify/piloto.py <lista.json> <tope_usd> <salida_dir>
"""
import json, os, re, sys, time, urllib.request, urllib.parse

lista_path, tope, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
os.makedirs(out, exist_ok=True)
TOKEN = os.environ.get("APIFY_TOKEN", "")
API = "https://api.apify.com/v2"
estado = {"fecha": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "paso": "inicio"}

def guardar_estado():
    json.dump(estado, open(os.path.join(out, "estado.json"), "w"), ensure_ascii=False, indent=1)

def api(metodo, ruta, cuerpo=None, params=None):
    url = API + ruta + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=data, method=metodo,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"null")

def saldo():
    d = api("GET", "/users/me/limits")["data"]
    usado = d.get("current", {}).get("monthlyUsageUsd")
    limite = d.get("limits", {}).get("maxMonthlyUsageUsd")
    return usado, limite

def correr(actor, entrada, tope_usd, minutos=40):
    run = api("POST", f"/acts/{actor}/runs", entrada, {"maxTotalChargeUsd": f"{tope_usd:.2f}"})["data"]
    rid, inicio = run["id"], time.time()
    while run["status"] in ("READY", "RUNNING") and time.time() - inicio < minutos * 60:
        time.sleep(15)
        run = api("GET", f"/actor-runs/{rid}")["data"]
    items, offset = [], 0
    while True:
        lote = api("GET", f"/datasets/{run['defaultDatasetId']}/items", params={"offset": offset, "limit": 1000, "clean": "true"})
        items += lote; offset += len(lote)
        if len(lote) < 1000: break
    return run["status"], run.get("usageTotalUsd"), items

if not TOKEN:
    estado.update(paso="sin_token", mensaje="El secreto APIFY_TOKEN no está guardado en el repositorio")
    guardar_estado(); sys.exit(0)
try:
    usado, limite = saldo()
except Exception as e:
    estado.update(paso="token_invalido", mensaje=f"Apify rechazó el token: {str(e)[:120]}")
    guardar_estado(); sys.exit(0)
disponible = (limite or 0) - (usado or 0)
estado.update(paso="saldo", usado_usd=usado, limite_usd=limite, disponible_usd=round(disponible, 2))
guardar_estado()
if disponible < 1.0:
    estado.update(paso="sin_saldo", mensaje="Queda menos de US$1 este mes"); guardar_estado(); sys.exit(0)
presupuesto = min(tope, disponible - 0.3)
lista = json.load(open(lista_path))

# Modo relectura: si cada entrada ya trae "fb", no se busca en Google
SOLO_LEER = all(x.get("fb") for x in lista)
# 1) Buscar la página de Facebook de cada parroquia en Google
consultas = [x["consulta"] for x in lista]
if SOLO_LEER:
    st, costo1, res = "OMITIDA", 0, []
else:
  st, costo1, res = correr("apify~google-search-scraper", {
    "queries": "\n".join(consultas), "resultsPerPage": 10, "maxPagesPerQuery": 1,
    "countryCode": "co", "languageCode": "es", "mobileResults": False, "saveHtml": False,
}, presupuesto * 0.35)
estado.update(paso="busqueda", busqueda_estado=st, busqueda_usd=costo1, busqueda_resultados=len(res)); guardar_estado()

def raiz_fb(u):
    m = re.match(r"https?://(?:[a-z]+\.)?facebook\.com/(.+)", u or "")
    if not m: return None
    ruta = m.group(1).split("?")[0].strip("/")
    partes = ruta.split("/")
    if partes[0] in ("groups", "events", "watch", "reel", "share", "photo", "photo.php", "story.php", "permalink.php", "hashtag", "marketplace", "login"): return None
    if partes[0] == "p" and len(partes) > 1: return f"https://www.facebook.com/p/{partes[1]}/"
    if partes[0] == "profile.php": return None
    if partes[0] == "people" and len(partes) > 2: return f"https://www.facebook.com/people/{partes[1]}/{partes[2]}/"
    return f"https://www.facebook.com/{partes[0]}/"

por_consulta = {}
for r in res:
    q = (r.get("searchQuery") or {}).get("term", "")
    por_consulta[q] = r.get("organicResults") or []

def tokens(s):
    s = s.lower()
    for a, b in zip("áéíóúñ", "aeioun"): s = s.replace(a, b)
    return {t for t in re.findall(r"[a-z0-9]+", s) if len(t) > 2 and t not in {"parroquia", "iglesia", "san", "santa", "nuestra", "senora", "del", "los", "las", "facebook"}}

hallados = {}
for x in lista:
    org = por_consulta.get(x["consulta"], [])
    tn, tm = tokens(x["parroquia"]), tokens(x["municipio"])
    for o in org:
        u = raiz_fb(o.get("url"))
        if not u: continue
        texto = (o.get("title", "") + " " + o.get("description", "") + " " + u)
        tt = tokens(texto)
        if (tn & tt) and (tm & tt or not tm):
            hallados[x["id"] + "|" + x["jur"]] = {"fb": u, "titulo": o.get("title", ""), "fragmento": o.get("description", "")}
            break
if SOLO_LEER:
    for x in lista:
        hallados[x["id"] + "|" + x["jur"]] = {"fb": x["fb"], "titulo": x.get("titulo", ""), "fragmento": ""}
estado.update(paginas_halladas=len(hallados)); guardar_estado()

# 2) Leer la información pública de esas páginas
paginas = sorted({h["fb"] for h in hallados.values()})
fbres = []
if paginas:
    restante = presupuesto - (costo1 or 0)
    st2, costo2, fbres = correr("apify~facebook-pages-scraper", {"startUrls": [{"url": u} for u in paginas]}, max(restante, 0.5))
    estado.update(paginas_estado=st2, paginas_usd=costo2, paginas_leidas=len(fbres))
try:
    u2, _ = saldo(); estado.update(usado_despues_usd=u2)
except Exception:
    pass
estado["paso"] = "terminado"
json.dump({"lista": lista, "hallados": hallados, "paginas": fbres}, open(os.path.join(out, "piloto.json"), "w"), ensure_ascii=False)
guardar_estado()
