"""Tri des fiches relevées selon les critères du tableau de bord.

Usage : python triage.py criteres.json lot1.json [lot2.json ...] > tri.json

Pour chaque fiche : extrait loyer, charges, pièces, chambres, localité, date
d'entrée, puis classe en
  match  : tout ce qui est connu correspond (date dans la fenêtre ou inconnue)
  check  : plausible mais une info manque ou déborde (loyer sur demande,
           quartier incertain, date hors fenêtre, loyer légèrement au-dessus…)
  out    : clairement hors critères (avec la raison)
Les doublons entre sources (même loyer, mêmes pièces, même rue) sont regroupés.
"""
import json, re, sys, unicodedata
from datetime import date

MONTHS = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
          "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12}

GENEVA_CITY_NPA = {str(n) for n in range(1201, 1210)}
ZONE_DEFS = {
    "carouge": {"npa": {"1227"}, "names": ["carouge"]},
    "les acacias": {"npa": {"1227"}, "names": ["acacias"]},
    "acacias": {"npa": {"1227"}, "names": ["acacias"]},
    "ville de geneve": {"npa": GENEVA_CITY_NPA, "names": [
        "plainpalais", "eaux-vives", "eaux vives", "champel", "jonction", "paquis", "servette", "petit-saconnex",
        "charmilles", "saint-jean", "st-jean", "florissant", "malagnou", "vieille-ville", "delices", "grottes",
        "cite", "geneve", "genève", "genf", "geneva", "rive", "tranchees", "bastions", "sécheron", "secheron",
        "nations", "contamines", "vermont", "beaulieu", "conches"]},
}
# Communes hors Ville de Genève / Carouge : si elles apparaissent, la fiche est hors zone.
OTHER_PLACES = ["lancy", "grand-lancy", "petit-lancy", "meyrin", "vernier", "onex", "thonex", "thônex", "chene-bourg",
    "chêne-bourg", "chene-bougeries", "chêne-bougeries", "cologny", "veyrier", "plan-les-ouates", "bernex",
    "versoix", "bellevue", "collonge", "vandoeuvres", "pregny", "grand-saconnex", "le grand-saconnex", "chambesy",
    "chambésy", "troinex", "confignon", "satigny", "anieres", "anières", "hermance", "corsier", "vesenaz", "vésenaz",
    "puplinge", "presinge", "jussy", "choulex", "collex", "genthod", "aire-la-ville", "avully", "cartigny",
    "chancy", "dardagny", "russin", "bardonnex", "perly", "soral", "laconnex", "avusy", "gy", "meinier",
    "lausanne", "prilly", "renens", "nyon", "gland", "rolle", "morges", "vevey", "montreux", "sion", "martigny",
    "fribourg", "neuchatel", "neuchâtel", "crans-montana", "zurich", "zürich", "bern", "berne", "basel", "bale",
    "(vd)", "(vs)", "(fr)", "(ne)", "vaud", "valais", "annemasse", "ferney", "saint-julien", "gaillard"]

NOT_HOUSING = re.compile(r"\b(parking|place de parc|place intérieure|garage|box|bureau|arcade|dépôt|depot|"
                         r"local commercial|surface commerciale|entrepôt|cave|à vendre|prix de vente|"
                         r"tiefgarage|parkplatz|chalet|bureaux|cabinet|enseigne|espace professionnel|espace polyvalent|"
                         r"atelier|commercial|locaux|commerce|industrie|studios?|colocation)\b|"
                         r"/\s*m[²2]\s*/\s*an", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def num(s: str) -> int | None:
    v = re.sub(r"[^\d]", "", s or "")
    return int(v) if v else None


AMOUNT = r"(\d{1,2}(?:['’ .,]\d{3})(?![\d]|['’.,]\d)|\d{3,5}(?![\d'’.,]\d))"


def parse_money(text: str):
    """Loyer (premier montant plausible) et charges."""
    t = text.replace("\u202f", " ").replace("\xa0", " ").replace("’", "'")
    charges = None
    for pat in (r"charges\s*:?\s*(?:CHF|Fr\.?)?\s*" + AMOUNT,
                r"\+\s*(?:CHF|Fr\.?)?\s*" + AMOUNT + r"\.?-?\s*(?:de\s+)?charges"):
        m = re.search(pat, t, re.I)
        if m:
            c = num(m.group(1))
            if c and 20 <= c <= 1500:
                charges = c
                break
    rent = None
    pats = [r"(?:CHF|Fr\.|loyer\s*:?)\s*" + AMOUNT, AMOUNT + r"\s*(?:\.-|\.–)?\s*(?:CHF|Fr\b)"]
    for pat in pats:
        for m in re.finditer(pat, t, re.I):
            v = num(m.group(1))
            if v and 600 <= v <= 30000 and v != charges:
                rent = v
                break
        if rent:
            break
    on_request = bool(re.search(r"sur demande|nous consulter|on request", t, re.I))
    return rent, charges, on_request


def parse_rooms(text: str):
    t = re.sub(r"(\d)\s*½", r"\1.5", re.sub(r"(\d),5", r"\1.5", text))
    rooms = []
    for a, b in re.findall(r"(?<![\d.])(\d{1,2}(?:\.5)?)\s*(?:pi[eè]ces?|pces?|p\.|zimmer|rooms?)(?![a-z])"
                           r"|(?<![\d.])(\d{1,2}(?:\.5)?)[pP](?![a-zA-Z])", t, re.I):
        v = float(a or b)
        if 1 <= v <= 12:
            rooms.append(v)
    beds = [int(x) for x in re.findall(r"(?<![\d.])(\d)\s*(?:chambres?(?:\s+à\s+coucher)?|bedrooms?)\b", t, re.I)]
    return (max(rooms) if rooms else None), (max(beds) if beds else None)


def parse_date(text: str):
    t = norm(text)
    if re.search(r"(disponib\w*|libre|entree|date de disponibilite)\s*:?\s*(de suite|immediat|tout de suite|rapidement)", t) \
            or re.search(r"\b(de suite|immediatement|immediate)\b", t):
        soon = "immediate"
    else:
        soon = None
    m = re.search(r"(?:des le|des|a partir du|disponible le|libre le|disponibilite\s*:?|entree\s*:?)\s*(?:le\s*)?"
                  r"(1er|\d{1,2})\s+([a-z]+)\s+(20\d\d)", t)
    if m and m.group(2) in MONTHS:
        d = 1 if m.group(1) == "1er" else int(m.group(1))
        try:
            return date(int(m.group(3)), MONTHS[m.group(2)], d), soon
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](20\d\d)\b", t)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))), soon
        except ValueError:
            pass
    return None, soon


def zone_check(blob: str, zones: list[str]):
    """True = dans une zone voulue, False = ailleurs, None = inconnu."""
    b = norm(blob)
    npas = set(re.findall(r"\b(1[0-9]{3})\b", b))
    wanted_npa, wanted_names = set(), []
    for z in zones:
        d = ZONE_DEFS.get(norm(z).strip())
        if d:
            wanted_npa |= d["npa"]
            wanted_names += d["names"]
        else:
            wanted_names.append(norm(z))
    geneva_npa = {n for n in npas if n.startswith("12")}
    swiss_other = re.findall(r"\b([1-9]\d{3})\s+(?!CHF|Fr\b|M2|M²|Pi)[A-Z][A-Za-zÀ-ÿ'\-]+", blob)
    swiss_other = [n for n in swiss_other if not n.startswith("12") and not n.startswith("20")]
    if geneva_npa & wanted_npa:
        return True, sorted(geneva_npa & wanted_npa)[0]
    if swiss_other and not geneva_npa:
        return False, swiss_other[0]
    for p in OTHER_PLACES:
        if re.search(r"(?<![a-z])" + re.escape(norm(p)) + r"(?![a-z])", b):
            return False, p
    if geneva_npa and not (geneva_npa & wanted_npa):
        return False, sorted(geneva_npa)[0]
    for n in wanted_names:
        if re.search(r"(?<![a-z])" + re.escape(norm(n)) + r"(?![a-z])", b):
            return True, n
    return None, None


def month_bounds(ym: str, end=False):
    if not ym:
        return None
    y, m = map(int, ym.split("-")[:2])
    if not end:
        return date(y, m, 1)
    return date(y + (m == 12), m % 12 + 1, 1)


def classify(card: dict, crit: dict) -> dict:
    text, detail = card.get("text", ""), card.get("detail") or ""
    blob = f"{text} {detail[:2500]} {card.get('href', '')}"
    rent, charges, on_request = parse_money(text)
    if rent is None:
        rent, charges2, on_request2 = parse_money(detail[:2500])
        charges = charges or charges2
        on_request = on_request or on_request2
    elif charges is None and detail:
        charges = parse_money(detail[:2500])[1]
    rooms, beds = parse_rooms(text)
    if rooms is None or beds is None:
        r2, b2 = parse_rooms(detail[:2500])
        rooms, beds = rooms or r2, beds or b2
    avail, soon = parse_date(f"{text} {detail[:3000]}")
    # La fiche courte d'abord : la page détaillée cite souvent d'autres biens.
    in_zone, zone_hit = zone_check(f"{text} {card.get('href', '')}", crit.get("zones", []))
    if in_zone is None:
        # Seulement le haut de la fiche détaillée : plus bas, on trouve
        # l'adresse de l'agence ou d'autres biens.
        in_zone, zone_hit = zone_check(detail[:600], crit.get("zones", []))
    # Une date égale ou proche d'aujourd'hui = date d'affichage ou « de suite ».
    if avail and (avail - date.today()).days <= 7:
        avail, soon = None, "immediate"

    out = {"rent": rent, "charges": charges, "rooms": rooms, "bedrooms": beds,
           "available": avail.isoformat() if avail else ("de suite" if soon else None), "zone": zone_hit}
    reasons_out, reasons_check = [], []

    if NOT_HOUSING.search(text) and not rooms and not beds:
        reasons_out.append("pas un logement")
    t_rent, _, t_req = parse_money(text)
    t_rooms, t_beds = parse_rooms(text)
    if re.search(r"\d+\s*/\s*\d+\s*biens|Tri\s*:|awswaf|\d+ appartements de \d", text):
        reasons_out.append("fiche illisible")
    elif t_rent is None and t_rooms is None and t_beds is None and not t_req \
            and not re.search(r"pi[eè]ces?|appartement|logement|attique|duplex", text, re.I):
        reasons_out.append("fiche illisible")
    if re.search(r"\d{1,3}['’ .]\d{3}['’ .]\d{3}", text) or re.search(r"\bà vendre\b|prix de vente", text, re.I):
        reasons_out.append("vente")

    rmin, bmin, rmax = crit.get("rooms_min", 5), crit.get("bedrooms_min", 3), crit.get("rent_max", 4200)
    if beds is not None:
        if beds < bmin and (rooms is None or rooms < rmin):
            reasons_out.append(f"{beds} chambre(s)")
        elif beds < bmin:
            reasons_check.append(f"{rooms:g} pièces mais {beds} chambre(s) annoncée(s)")
    elif rooms is not None:
        if rooms < rmin - 0.5:
            reasons_out.append(f"{rooms:g} pièces")
        elif rooms < rmin:
            reasons_check.append(f"{rooms:g} pièces, nombre de chambres non indiqué")
    else:
        reasons_check.append("pièces non indiquées")

    total = rent + (charges or 0) if rent else None
    if rent is None:
        reasons_check.append("loyer sur demande" if on_request else "loyer non indiqué")
    elif rent > rmax * 1.05:
        reasons_out.append(f"loyer CHF {rent}")
    elif total > rmax:
        reasons_check.append(f"CHF {total} charges comprises, au-dessus de {rmax}")

    if in_zone is False:
        reasons_out.append(f"hors zone ({zone_hit})")
    elif in_zone is None:
        reasons_out.append("lieu non reconnu (ni Genève, ni Carouge, ni Acacias)")

    # Fenêtre d'entrée : indicative, ne change jamais le niveau, juste une mention.
    lo, hi = month_bounds(crit.get("entry_from")), month_bounds(crit.get("entry_to"), end=True)
    if avail and lo and hi and not (lo <= avail < hi):
        out["entry_note"] = f"entrée le {avail.strftime('%d.%m.%Y')}, hors fenêtre"
    elif soon and not avail:
        out["entry_note"] = "disponible de suite"
    elif avail:
        out["entry_note"] = f"entrée le {avail.strftime('%d.%m.%Y')}"

    if reasons_out:
        out.update(level="out", reason=" ; ".join(reasons_out))
    elif reasons_check:
        out.update(level="check", reason=" ; ".join(reasons_check))
    else:
        out.update(level="match", reason="")
    return out


def street_key(card):
    t = norm(f"{card.get('text','')} {card.get('href','')}")
    m = re.search(r"(rue|route|avenue|av\.?|chemin|ch\.|boulevard|bd|place|quai|cours|rampe|promenade)[ -]+(?:de |du |des |de la |de l' ?)?([a-z\-]{3,})", t)
    return m.group(2) if m else None


def main():
    crit = json.load(open(sys.argv[1]))
    cards = []
    for path in sys.argv[2:]:
        cards += json.load(open(path))["cards"]
    results, groups = [], {}
    for c in cards:
        if c.get("pre") == "out":
            continue
        r = classify(c, crit)
        r.update(hash=c["hash"], source=c["source"], source_id=c["source_id"], url=c["href"],
                 text=c["text"][:400], detail=(c.get("detail") or "")[:1500])
        key = (r["rent"], r["rooms"], street_key(c)) if r["rent"] and r["rooms"] else None
        if key and key in groups:
            groups[key]["also"] = sorted(set(groups[key].get("also", []) + [c["source"]]))
            continue
        if key:
            groups[key] = r
        results.append(r)
    json.dump(results, sys.stdout, ensure_ascii=False, indent=1)
    lv = {k: sum(1 for r in results if r["level"] == k) for k in ("match", "check", "out")}
    print(f"tri : {lv}", file=sys.stderr)


if __name__ == "__main__":
    main()
