"""Tri des fiches relevées selon les critères du tableau de bord.

Usage : python triage.py criteres.json lot1.json [lot2.json ...] > tri.json

Pour chaque fiche : extrait loyer, charges, pièces, chambres, localité, date
d'entrée, meublé / temporaire, puis classe en
  match  : tout ce qui est connu correspond
  check  : plausible mais une info manque ou déborde (loyer non indiqué,
           pièces non indiquées, total charges comprises jusqu'à +5 % au-dessus
           du maximum, charges non indiquées avec un loyer à moins de 500 CHF du maximum…)
  out    : clairement hors critères (avec la raison)
La date d'entrée ne change jamais le niveau : elle est seulement signalée.
Un lieu non reconnu (ni code postal ni quartier de la zone) est écarté.
Les doublons d'un même passage (même loyer, mêmes pièces, même rue) sont regroupés.
"""
import json, re, sys, unicodedata
from datetime import date

MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
          "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower().replace("’", "'"))
    return "".join(c for c in s if not unicodedata.combining(c))


# ---------------------------------------------------------------- zones
CITY_NAMES = ["plainpalais", "eaux-vives", "eaux vives", "champel", "jonction", "paquis", "servette",
              "petit-saconnex", "charmilles", "saint-jean", "st-jean", "florissant", "malagnou",
              "vieille-ville", "vieille ville", "delices", "grottes", "tranchees", "bastions", "secheron",
              "nations", "contamines", "vermont", "beaulieu", "saint-gervais", "st-gervais", "cornavin",
              "geneve", "genf", "geneva"]
GENERIC = {"geneve", "genf", "geneva"}          # trop vagues : ne comptent qu'en dernier recours
CITY = {"npa": {str(n) for n in range(1201, 1210)}, "names": CITY_NAMES}
ZONE_DEFS = {
    # zones par défaut (avec leurs variantes d'écriture)
    "ville de geneve": CITY, "geneve": CITY, "geneve ville": CITY, "geneva": CITY, "genf": CITY,
    "carouge": {"npa": {"1227"}, "names": ["carouge"]},
    "carouge ge": {"npa": {"1227"}, "names": ["carouge"]},
    "les acacias": {"npa": {"1227"}, "names": ["acacias"]},
    "acacias": {"npa": {"1227"}, "names": ["acacias"]},
    # autres communes, si on les ajoute aux critères
    "lancy": {"npa": {"1212", "1213"}, "names": ["lancy", "grand-lancy", "petit-lancy"]},
    "grand-lancy": {"npa": {"1212"}, "names": ["grand-lancy"]},
    "petit-lancy": {"npa": {"1213"}, "names": ["petit-lancy"]},
    "onex": {"npa": {"1213"}, "names": ["onex"]},
    "chene-bougeries": {"npa": {"1224", "1231"}, "names": ["chene-bougeries", "conches"]},
    "chene-bourg": {"npa": {"1225"}, "names": ["chene-bourg"]},
    "thonex": {"npa": {"1226"}, "names": ["thonex"]},
    "plan-les-ouates": {"npa": {"1228"}, "names": ["plan-les-ouates"]},
    "veyrier": {"npa": {"1255", "1234"}, "names": ["veyrier", "vessy"]},
    "troinex": {"npa": {"1256"}, "names": ["troinex"]},
    "vernier": {"npa": {"1214", "1219", "1220"}, "names": ["vernier", "chatelaine", "lignon", "avanchets"]},
    "meyrin": {"npa": {"1217"}, "names": ["meyrin"]},
    "grand-saconnex": {"npa": {"1218"}, "names": ["grand-saconnex"]},
    "cologny": {"npa": {"1223"}, "names": ["cologny"]},
    "bernex": {"npa": {"1233"}, "names": ["bernex"]},
    "confignon": {"npa": {"1232"}, "names": ["confignon"]},
    "versoix": {"npa": {"1290"}, "names": ["versoix"]},
    "bellevue": {"npa": {"1293"}, "names": ["bellevue"]},
    "vandoeuvres": {"npa": {"1253"}, "names": ["vandoeuvres"]},
}
# Lieux hors zone : s'ils apparaissent (hors nom de rue), la fiche est hors zone.
OTHER_PLACES = ["lancy", "grand-lancy", "petit-lancy", "meyrin", "vernier", "onex", "thonex", "chene-bourg",
    "chene-bougeries", "conches", "cologny", "veyrier", "vessy", "plan-les-ouates", "bernex", "versoix", "bellevue",
    "collonge", "vandoeuvres", "pregny", "grand-saconnex", "chambesy", "troinex", "confignon", "satigny",
    "anieres", "hermance", "corsier", "vesenaz", "puplinge", "presinge", "jussy", "choulex", "collex", "genthod",
    "aire-la-ville", "avully", "cartigny", "chancy", "dardagny", "la plaine", "russin", "bardonnex", "perly",
    "soral", "laconnex", "avusy", "meinier", "lignon", "chatelaine", "cointrin", "avanchets", "lully", "bachet",
    "moillesulaz", "lausanne", "prilly", "renens", "nyon", "gland", "rolle", "morges", "vevey", "montreux", "sion",
    "martigny", "fribourg", "neuchatel", "crans-montana", "zurich", "bern", "berne", "basel", "bale",
    "(vd)", "(vs)", "(fr)", "(ne)", "(ju)", "vaud", "valais", "annemasse", "ambilly", "ferney", "gex", "divonne",
    "thoiry", "prevessin", "archamps", "saint-julien", "gaillard", "france"]
# Un lieu cité comme nom de rue ne dit rien de la commune (« Rue de Lausanne » est à Genève).
STREET_CTX = re.compile(r"(?:rue|route|rte|chemin|ch|avenue|av|boulevard|bd|quai|place|pl|pont|promenade|prom|"
                        r"sentier|passage|square|esplanade|rampe|cours|impasse|allee|tour|gare|parc|porte)\.?\s+"
                        r"(?:de\s+la\s+|de\s+l'|des\s+|du\s+|de\s+|d')?$")


def mentions(b: str, name: str) -> bool:
    """name apparaît dans b ailleurs que comme nom de rue (b : texte normalisé, tirets remplacés par des espaces)."""
    for m in re.finditer(r"(?<![a-z])" + re.escape(name.replace("-", " ")) + r"(?![a-z])", b):
        if not STREET_CTX.search(b[max(0, m.start() - 30):m.start()]):
            return True
    return False


def resolve_zones(zones):
    npa, specific, generic = set(), [], []
    for z in zones or []:
        d = ZONE_DEFS.get(norm(z).strip())
        names = d["names"] if d else [norm(z).strip()]
        if d:
            npa |= d["npa"]
        for n in names:
            (generic if n in GENERIC else specific).append(n)
    return npa, specific, generic


def zone_check(blob: str, zones):
    """(True|False|None, indice). True = dans la zone, False = ailleurs, None = inconnu."""
    b = norm(blob).replace("-", " ")             # « Petit-Saconnex » = « petit saconnex »
    wanted_npa, specific, generic = resolve_zones(zones)
    wanted_all = set(specific) | set(generic)
    # Code postal = 4 chiffres suivis d'un nom de lieu (« 1205 Genève », « 1227 Carouge GE »).
    # Une référence, un téléphone ou une année ne sont pas suivis d'un nom propre.
    npas = []
    for m in re.finditer(r"(?<![\w'.,²/-])(?:CH-)?(\d{4})\s*,?\s+(?!CHF\b|Fr\b|M2\b|M²|T[ée]l|Fax|R[ée]f)[A-ZÀ-Ý][A-Za-zÀ-ÿ'\-]+",
                         blob or ""):
        if re.search(r"(r[ée]f|t[ée]l|fax|n°|no|id|lot|#)\.?\s*:?\s*$", (blob or "")[max(0, m.start() - 8):m.start()], re.I):
            continue
        npas.append(m.group(1))
    npas = [n for n in npas if not n.startswith(("19", "20", "0"))]
    geneva = [n for n in npas if n.startswith("12")]
    others = [n for n in npas if not n.startswith("12")]
    hit = [n for n in geneva if n in wanted_npa]
    if hit:
        return True, hit[0]
    if others and not geneva:
        return False, others[0]
    for p in OTHER_PLACES:
        if p not in wanted_all and mentions(b, p):
            return False, p
    if geneva:
        return False, geneva[0]
    for n in specific + generic:
        if mentions(b, n):
            return True, n
    # Dernier recours : une rue qui porte le nom d'un quartier de la zone
    # (« Avenue de Champel », « Route des Acacias ») est dans la zone.
    for n in specific:
        if re.search(r"(?<![a-z])" + re.escape(n.replace("-", " ")) + r"(?![a-z])", b):
            return True, n
    return None, None


def url_place(href: str) -> str:
    """Lieu lisible dans l'adresse de l'annonce, pour les sites qui l'y mettent."""
    h = (href or "").lower()
    out = []
    if "immobilier.ch/" not in h:     # chez immobilier.ch, les nombres de l'adresse sont des n° d'agence ou d'annonce
        out = [f"{n} Lieu" for n in re.findall(r"(?<!\d)(12\d\d)(?!\d)", h)]      # « 1205-genf », « /1206/ »
    m = re.search(r"immobilier\.ch/.+/([a-z0-9\-]+)-\d{5,}/?$", h)                 # titre de l'annonce dans l'adresse
    if m:                                                    # « appartement-coeur-quartier-grottes-1613670 »
        out += [f"{n} Lieu" for n in re.findall(r"(?<!\d)(12\d\d)(?=-[a-z])", m.group(1))]   # « …-1207-geneve »
        out.append("lieu " + re.sub(r"\d+", " ", m.group(1)).replace("-", " "))
    m = re.search(r"immobilier\.ch/\w+/louer/[^/]+/geneve/([a-z\-]+)/", h)        # /geneve/<localité>/
    if m and m.group(1) not in ("page-1",):
        out.append("lieu " + m.group(1).replace("-ge", "").replace("-", " "))
    m = re.search(r"gerofinance\.ch/\d+-[\d-]+-([a-z0-9\-]+)$", h.rstrip("/"))
    if m:                                                                       # slug « …-rte-de-frontenex-geneve-… »
        out.append("lieu " + re.sub(r"\d+", " ", m.group(1)).replace("-", " "))
    return " ".join(out)


# ---------------------------------------------------------------- extraction
NOT_HOUSING = re.compile(r"\b(parking|place de parc|place interieure|garage|box|bureau|bureaux|arcade|depot|"
                         r"local|locaux|surface commerciale|entrepot|cave|a vendre|prix de vente|tiefgarage|"
                         r"parkplatz|chalet|cabinet|enseigne|espace professionnel|espace polyvalent|atelier|"
                         r"commercial|commerce|industrie|studios?|colocation)\b|/\s*m[²2]\s*/\s*an")
AMOUNT = r"(\d{1,2}(?:['’ .,]\d{3})(?![\d]|['’.,]\d)|\d{3,5}(?![\d'’.,]\d))"


def num(s):
    v = re.sub(r"[^\d]", "", s or "")
    return int(v) if v else None


CUR = r"(?:CHF|Fr\.?|Frs\.?)"
DASH = r"(?:\s*\.?\s*[-–—]+)?"                     # « .- », « .– », « .— »
CHARGE_PATS = (
    r"(?:acompte\s+de\s+charges|provisions?\s+(?:sur|pour)\s+(?:les\s+)?charges|charges(?:\s+mensuelles)?|nebenkosten)"
    r"\s*:?\s*" + CUR + r"?\s*" + AMOUNT,
    r"\+\s*" + CUR + r"?\s*" + AMOUNT + DASH + r"\s*(?:de\s+)?(?:charges|nebenkosten)",
)
GROSS_PATS = (
    r"(?:loyer\s+(?:mensuel\s+)?(?:brut|total)|bruttomiete|loyer\s+charges\s+comprises)\s*(?:\([^)]{0,40}\))?\s*:?\s*"
    + CUR + r"?\s*" + AMOUNT,
    CUR + r"\s*" + AMOUNT + DASH + r"\s*(?:/\s*mois|par\s+mois)?\s*,?\s*(?:charges\s+(?:comprises|incluses)|cc\b|c\.c\.|brut)",
    AMOUNT + DASH + r"\s*" + CUR + r"\s*(?:/\s*mois|par\s+mois)?\s*,?\s*(?:charges\s+(?:comprises|incluses)|cc\b|c\.c\.|brut)",
)
NET_PATS = (r"(?:loyer\s+(?:mensuel\s+)?net|nettomiete|hors\s+charges)\s*:?\s*" + CUR + r"?\s*" + AMOUNT,)


def _clean(text):
    return (text or "").replace("\u202f", " ").replace("\xa0", " ").replace("’", "'").replace("`", "'")


def _first(pats, t, lo, hi):
    for pat in pats:
        for m in re.finditer(pat, t, re.I):
            v = num(m.group(1))
            if v and lo <= v <= hi:
                return v
    return None


def parse_money(text: str):
    """(loyer affiché, charges, sur_demande, charges_comprises)."""
    t = _clean(text)
    charges = _first(CHARGE_PATS, t, 20, 1500)
    rent = None
    for pat in (r"(?:CHF|Fr\.|loyer\s*:?)\s*" + AMOUNT, AMOUNT + r"\s*(?:\.-|\.–)?\s*(?:CHF|Fr\b)"):
        for m in re.finditer(pat, t, re.I):
            v = num(m.group(1))
            if v and 600 <= v <= 30000 and v != charges:
                rent = v
                break
        if rent:
            break
    on_request = bool(re.search(r"sur demande|nous consulter|on request", t, re.I))
    incl = bool(re.search(r"charges comprises|charges incluses|c\.c\.|cc\b|brut|gross|inkl\.? nebenkosten", t, re.I))
    return rent, charges, on_request, incl


INCL_TEXT = re.compile(r"charges?\s*(?:mensuelles\s*)?:?\s*(?:comprises|incluses|inclues|inclus)\b|\bcc\b|c\.c\.|\bbrut\b", re.I)
INCL_DETAIL = re.compile(r"charges?\s*(?:mensuelles\s*)?:?\s*(?:comprises|incluses|inclues|inclus)\b", re.I)
# Sources dont le prix affiché sur la liste est le loyer brut (charges comprises) :
# Flatfox, vérifié sur les 37 fiches détaillées relevées le 16.11.2026 (37 sur 37).
GROSS_LIST_SOURCES = {"flatfox"}


def money(text: str, detail: str, source_id: str = ""):
    """(loyer affiché, charges, total charges comprises ou None, sur_demande, loyer_affiché_est_brut).

    Le loyer affiché sur la liste est tantôt net (charges en plus), tantôt brut (Flatfox, Comptoir
    immobilier…) : la fiche détaillée tranche quand elle donne « loyer brut », « loyer net » ou les charges."""
    t, d = _clean(text), _clean(detail)[:2500]
    rent, ch_t, req, _ = parse_money(t)
    from_list = rent is not None
    gross_t = _first(GROSS_PATS, t, 600, 30000)
    gross_d, net_d = _first(GROSS_PATS, d, 600, 30000), _first(NET_PATS, d, 600, 30000)
    ch_d = _first(CHARGE_PATS, d, 20, 1500)
    if rent is None:                                   # pas de loyer sur la liste : celui de la fiche
        rent, ch_d2, req_d, _ = parse_money(d)
        ch_d, req = ch_d or ch_d2, req or req_d
        if rent is None:
            return None, None, None, req, False
    same = lambda a, b: a is not None and b is not None and abs(a - b) <= 1
    brut = (rent, None, rent, req, True)
    if from_list and source_id in GROSS_LIST_SOURCES:
        return brut
    if same(gross_t, rent) or same(gross_d, rent):      # le loyer affiché est déjà charges comprises
        return brut
    if net_d and ch_d and same(net_d + ch_d, rent):     # « Loyer 3'900 · Loyer net 3'600 · Charges 300 »
        return brut
    if gross_d and same(net_d, rent):
        return rent, (gross_d - rent) if gross_d > rent else ch_d, gross_d, req, False
    if ch_t:                                           # « + CHF 300 de charges » à côté du loyer
        return rent, ch_t, rent + ch_t, req, False
    if ch_d and (net_d is None or same(net_d, rent)):
        return rent, ch_d, rent + ch_d, req, False
    if INCL_TEXT.search(t) or (ch_d is None and INCL_DETAIL.search(d)):
        return brut                                    # « charges comprises / incluses », sans montant de charges
    return rent, None, None, req, False


def parse_rooms(text: str, source_id: str = ""):
    t = re.sub(r"([a-zéèàù])([A-ZÉÈ])", r"\1 \2", text or "")                  # « piècesCarouge » collés
    t = re.sub(r"(\d)\s*½", r"\1.5", re.sub(r"(\d),5\b", r"\1.5", t))
    t = re.sub(r"(?<![\d.,])(\d{1,2})[.,]0(?!\d)", r"\1", t)          # « 4.0 pièces » = 4 pièces
    # Format « Pièces : 5 · Chambres : 3 » : l'étiquette précède le nombre ; prioritaire.
    rooms_lbl = [float(a) for a in re.findall(r"(?:nombre de\s+)?(?:pi[eè]ces?|zimmer|rooms?)\s*:\s*(\d{1,2}(?:\.5)?)(?![\d])", t, re.I)]
    beds_lbl = [int(x) for x in re.findall(r"(?:chambres?(?:\s+[aà]\s+coucher)?|bedrooms?)\s*:\s*(\d)(?![\d])", t, re.I)]
    rooms = []
    for a, b in re.findall(r"(?<![\d.])(\d{1,2}(?:\.5)?)\s*(?:pi[eè]ces?|pces?|p\.|zimmer|rooms?)(?![a-z])"
                           r"|(?<![\d.])(\d{1,2}(?:\.5)?)[pP](?![a-zA-Z])", t, re.I):
        rooms.append(float(a or b))
    beds = [int(x) for x in re.findall(r"(?<![\d.])(\d)\s*(?:chambres?(?:\s+[aà]\s+coucher)?|bedrooms?)\b", t, re.I)]
    if rooms_lbl:
        rooms = rooms_lbl
    if beds_lbl:
        beds = beds_lbl
    if source_id == "naef" and not beds:
        # Fiches Naef : « … 3 5 95m2 » = 3 chambres, 5 pièces, 95 m²
        m = re.search(r"(?<![\d.])(\d)\s+(\d{1,2}(?:\.5)?)\s+[\d'.]+\s?m2", t)
        if m:
            beds.append(int(m.group(1)))
            rooms.append(float(m.group(2)))
    rooms = [r for r in rooms if 1 <= r <= 12]
    beds = [b for b in beds if 0 <= b <= 8]
    return (max(rooms) if rooms else None), (max(beds) if beds else None)


DATE_KW = re.compile(r"(disponib|libre|entree|des le|des |a partir|a convenir|bezug|verfugbar|available|regime|"
                     r"date de disponibilite)")


def parse_date(text: str):
    """(date, 'immediate'|None). Une date n'est retenue que près d'un mot d'entrée/disponibilité."""
    t = norm(text)
    soon = "immediate" if re.search(r"\b(de suite|tout de suite|immediatement|immediate|sofort|immediately)\b", t) else None
    m = re.search(r"(?:des le|des|a partir du|disponible le|disponible des le|libre le|libre des le|disponibilite\s*:?|"
                  r"entree\s*:?)\s*(?:le\s*)?(1er|\d{1,2})\s+([a-z]+)\s+(20\d\d)", t)
    if m and m.group(2) in MONTHS:
        try:
            return date(int(m.group(3)), MONTHS[m.group(2)], 1 if m.group(1) == "1er" else int(m.group(1))), soon
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](20\d\d)\b", t):
        if DATE_KW.search(t[max(0, m.start() - 60):m.start()]):
            try:
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1))), soon
            except ValueError:
                continue
    return None, soon


def flags(text: str):
    t = norm(text)
    furnished = bool(re.search(r"\bmeubl[e]e?s?\b|\bfurnished\b|\bmoebliert|\bmobliert", t)) and \
        not re.search(r"\bnon[- ]meubl|\bunfurnished\b", t)
    temporary = bool(re.search(r"\btemporaire\b|courte duree|short[- ]term|\bbefristet|sous-location|\bsublet", t))
    return furnished, temporary


def month_bounds(ym, end=False):
    if not ym:
        return None
    y, m = map(int, ym.split("-")[:2])
    return date(y, m, 1) if not end else date(y + (m == 12), m % 12 + 1, 1)


# ---------------------------------------------------------------- classement
def classify(card: dict, crit: dict) -> dict:
    sid = card.get("source_id", "")
    text, detail = card.get("text", ""), card.get("detail") or ""
    top = detail[:1200]                       # le haut de la fiche détaillée : plus bas, d'autres biens
    rent, charges, total, on_request, gross = money(text, detail, sid)
    rooms, beds = parse_rooms(text, sid)
    if rooms is None or beds is None:
        r2, b2 = parse_rooms(top, sid)
        rooms, beds = rooms if rooms is not None else r2, beds if beds is not None else b2
    avail, soon = parse_date(f"{text} {detail[:2000]}")
    if avail and (avail - date.today()).days <= 7:      # date d'affichage ou « de suite »
        avail, soon = None, "immediate"
    in_zone, zone_hit = zone_check(f"{text} {url_place(card.get('href', ''))}", crit.get("zones", []))
    if in_zone is None and top:
        in_zone, zone_hit = zone_check(top[:600], crit.get("zones", []))
    elif in_zone and zone_hit in GENERIC and top:      # « Genève » seulement : la fiche précise peut-être le quartier
        z2, h2 = zone_check(top[:600], crit.get("zones", []))
        if z2 and h2 not in GENERIC:
            zone_hit = h2
    furnished, temporary = flags(f"{text} {top}")

    out = {"rent": rent, "charges": charges, "rooms": rooms, "bedrooms": beds, "zone": zone_hit,
           "available": avail.isoformat() if avail else ("de suite" if soon else None),
           "furnished": furnished, "temporary": temporary, "charges_included": gross, "total": total}
    why_out, why_check = [], []

    t_rent, _, t_req, _ = parse_money(text)
    t_rooms, t_beds = parse_rooms(text, sid)
    if NOT_HOUSING.search(norm(text)) and not rooms and not beds:
        why_out.append("pas un logement")
    if re.search(r"\d+\s*/\s*\d+\s*biens|Tri\s*:|awswaf|\d+ appartements de \d", text):
        why_out.append("fiche illisible")
    elif t_rent is None and t_rooms is None and t_beds is None and not t_req \
            and not re.search(r"pi[eè]ces?|appartement|logement|attique|duplex", text, re.I):
        why_out.append("fiche illisible")
    if re.search(r"\d{1,3}['’ .]\d{3}['’ .]\d{3}", text) or re.search(r"\bà vendre\b|prix de vente", text, re.I):
        why_out.append("vente")

    rmin, bmin, rmax = crit.get("rooms_min") or 5, crit.get("bedrooms_min") or 3, crit.get("rent_max") or 4200
    if beds is not None:
        if beds < bmin and (rooms is None or rooms < rmin):
            why_out.append(f"{beds} chambre(s)")
        elif beds < bmin:
            why_check.append(f"{rooms:g} pièces mais {beds} chambre(s) annoncée(s)")
    elif rooms is not None:
        if rooms < rmin:
            why_out.append(f"{rooms:g} pièces")      # p. ex. 4.5 pièces sans « 3 chambres » écrit
    else:
        m2 = [int(x) for x in re.findall(r"(?<![\d.,])(\d{2,3})\s?m(?:2|²)", f"{text} {top}")]
        if m2 and max(m2) < 65:
            why_out.append(f"{max(m2)} m²")              # trop petit pour 3 chambres
        else:
            why_check.append("pièces non indiquées")

    # Loyer maximum = charges comprises (choix de Sébastien, 16.11.2026).
    if rent is None:
        why_check.append("loyer sur demande" if on_request else "loyer non indiqué")
    elif total is not None:
        if total > rmax * 1.05:
            why_out.append(f"CHF {total} charges comprises")
        elif total > rmax:
            why_check.append(f"CHF {total} charges comprises, un peu au-dessus de {rmax}")
    elif rent > rmax * 1.05:
        why_out.append(f"loyer CHF {rent} sans les charges")
    elif rent > rmax - 500:
        why_check.append(f"charges non indiquées (loyer CHF {rent})")

    if in_zone is False:
        why_out.append(f"hors zone ({zone_hit})")
    elif in_zone is None:
        why_out.append("lieu non reconnu")

    if furnished and crit.get("include_furnished") is False:
        why_out.append("meublé")
    if temporary and crit.get("include_temporary") is False:
        why_out.append("location temporaire")

    # Fenêtre d'entrée : indicative, ne change jamais le niveau.
    lo, hi = month_bounds(crit.get("entry_from")), month_bounds(crit.get("entry_to"), end=True)
    if avail and lo and hi and not (lo <= avail < hi):
        out["entry_note"] = f"entrée le {avail.strftime('%d.%m.%Y')}, hors fenêtre"
    elif avail:
        out["entry_note"] = f"entrée le {avail.strftime('%d.%m.%Y')}"
    elif soon:
        out["entry_note"] = "disponible de suite"

    if why_out:
        out.update(level="out", reason=" ; ".join(why_out))
    elif why_check:
        out.update(level="check", reason=" ; ".join(why_check))
    else:
        out.update(level="match", reason="")
    return out


def street_key(card):
    t = norm(f"{card.get('text', '')} {card.get('href', '')}")
    m = re.search(r"(rue|route|avenue|av\.?|chemin|boulevard|bd|place|quai|cours|rampe|promenade)[ -]+"
                  r"(?:de |du |des |de la |de l' ?)?([a-z\-]{3,})", t)
    return m.group(2) if m else None


def main():
    crit = json.load(open(sys.argv[1]))
    cards = []
    for path in sys.argv[2:]:
        cards += json.load(open(path))["cards"]
    results, groups = [], {}
    for c in cards:
        # Les fiches écartées par le pré-filtre du robot sont triées quand même :
        # si les critères s'élargissent, elles peuvent redevenir pertinentes.
        r = classify(c, crit)
        r.update(hash=c["hash"], source=c["source"], source_id=c["source_id"], url=c["href"],
                 weak=bool(c.get("weak")), text=c["text"][:400], detail=(c.get("detail") or "")[:1500])
        key = (r["rent"], r["rooms"], street_key(c)) if r["rent"] and r["rooms"] and street_key(c) else None
        if key and key in groups:
            groups[key]["also"] = sorted(set(groups[key].get("also", []) + [c["source"]]) - {groups[key]["source"]})
            continue
        if key:
            groups[key] = r
        results.append(r)
    json.dump(results, sys.stdout, ensure_ascii=False, indent=1)
    lv = {k: sum(1 for r in results if r["level"] == k) for k in ("match", "check", "out")}
    print(f"tri : {lv}", file=sys.stderr)


if __name__ == "__main__":
    main()
