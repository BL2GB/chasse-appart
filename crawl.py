"""Robot de relevé des annonces de location (régies + portails, Genève/Carouge).

Pour chaque source :
  1. vérifie le robots.txt du site (on saute ce qui est interdit à tous les robots) ;
  2. ouvre la page de liste dans Chromium headless (le JavaScript s'exécute) ;
  3. parcourt toutes les pages : lien « suivant », bouton « voir plus », défilement ;
  4. extrait chaque « fiche » (un lien + le bloc de texte qui l'entoure) ;
  5. pour les fiches nouvelles qui passent le pré-filtre large, ouvre la fiche
     détaillée et garde son texte (c'est là qu'on trouve la date d'entrée).

Aucun sélecteur propre à un site : l'interprétation (pièces, loyer, quartier,
date) est faite ensuite par Claude, selon les critères du tableau de bord.

Sorties dans data/ :
  seen.json            empreinte -> date de 1re vue
  batches/<date>.json  fiches nouvelles d'un passage (gardées 30 jours)
  index.json           liste des lots (lisible sans API GitHub)
  run.json             état de chaque source au dernier passage
"""
import asyncio, hashlib, json, os, re, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from urllib.request import Request, urlopen

import yaml
from playwright.async_api import async_playwright

ROOT = Path(__file__).parent
DATA = ROOT / "data"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
KEEP_DAYS = 30

RENT_LINK = re.compile(r"(à|a)[ -]?louer|louer|location|rent|mieten", re.I)
SALE_LINK = re.compile(r"vendre|acheter|vente|kaufen|buy|commercial|parking|bureau", re.I)
LISTING_HINT = re.compile(r"(CHF|Fr\.|\bfrs?\b|loyer|pi[eè]ces?|pces?|\bp\.|chambres?|m²|m2|rooms?|Zimmer)", re.I)
PRICE_HINT = re.compile(r"\d[\d'’ .]{2,}")

ROOMS = re.compile(r"(\d+(?:[.,]5)?)\s*(?:pi[eè]ces?|pces?|p\.|rooms?|Zimmer)", re.I)
BEDROOMS = re.compile(r"(\d)\s*chambres?", re.I)
CHF = re.compile(r"(?:CHF|Fr\.?|frs?)\s*([\d'’ .]{3,9})|([\d'’ .]{3,9})\s*(?:CHF|\.-|Fr\.?)", re.I)
NOT_HOUSING = re.compile(r"\b(à vendre|prix de vente|parking|place de parc|garage|box|bureau|arcade|"
                         r"dépôt|local commercial|surface commerciale|entrepôt|cave)\b", re.I)

COOKIE_JS = r"""
() => {
  const rx = /^(tout accepter|accepter( tout| et fermer| les cookies)?|j'accepte|accept( all)?|ok|d'accord|alle akzeptieren|autoriser)$/i;
  for (const b of document.querySelectorAll('button, a, [role=button]')) {
    const t = (b.innerText || '').trim();
    if (rx.test(t)) { try { b.click(); return t; } catch (e) {} }
  }
  return null;
}
"""

CARDS_JS = r"""
() => {
  const out = [], seen = new Set();
  const here = location.href.split('#')[0];
  const dataUrl = e => e && (e.getAttribute('data-href') || e.getAttribute('data-url') || e.getAttribute('data-link'));
  const abs = u => { if (!u) return null; try { return new URL(u, location.href).href; } catch (e) { return null; } };
  const nodes = document.querySelectorAll('a[href], [data-href], [data-url], [data-link]');
  for (const a of nodes) {
    let href = a.tagName === 'A' && a.getAttribute('href') !== null ? a.href : abs(dataUrl(a));
    if (!href || /^(javascript|mailto|tel):/.test(href)) continue;
    // On remonte jusqu'à un bloc assez grand pour être une fiche, sans jamais
    // englober un bloc qui contient des liens vers d'autres pages (= une liste).
    const base = href.split('#')[0];
    let el = a, txt = '';
    for (let i = 0; i < 7 && el; i++) {
      const others = [...el.querySelectorAll('a[href]')].some(x => {
        const h = x.href.split('#')[0];
        return h !== base && h !== here && !/^(javascript|mailto|tel):/.test(x.href);
      });
      if (others && el !== a) break;
      txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
      if (txt.length >= 40) break;
      el = el.parentElement;
    }
    if (txt.length < 40 || txt.length > 900) continue;
    // Lien qui ramène à la page de liste (fiche ouverte en JavaScript) : on
    // cherche une adresse dans les attributs data-* du bloc.
    let weak = false;
    if (base === here || href.split('?')[0] === here.split('?')[0]) {
      const alt = el && (dataUrl(el) || dataUrl(el.querySelector('[data-href],[data-url],[data-link]')) ||
                         dataUrl(el.closest('[data-href],[data-url],[data-link]')));
      if (alt && abs(alt) !== here) href = abs(alt); else weak = true;
    }
    const key = href.split('#')[0] + '|' + txt.slice(0, 80);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({href, text: txt, weak});
  }
  return out;
}
"""

# Indices de pagination (boutons visibles) pour diagnostiquer une source bloquée en page 1.
NAV_HINTS_JS = r"""
() => [...document.querySelectorAll('button, [role=button], nav a, [class*=pag] *, [class*=more] *')]
  .filter(e => e.offsetParent !== null).map(e => (e.innerText || e.getAttribute('aria-label') || '').trim())
  .filter(t => t && t.length < 40).slice(0, 40)
"""

LINKS_JS = r"""
() => [...document.querySelectorAll('a[href]')].map(a => ({href: a.href, text: (a.innerText||'').trim().slice(0,80), rel: a.rel || '', aria: a.getAttribute('aria-label') || ''}))
"""

# Bouton « voir plus » : on clique tant qu'il y en a un (et que la page grossit).
MORE_JS = r"""
() => {
  const rx = /^(voir|afficher|charger) (plus|davantage)|^plus de (résultats|biens|objets)|^load more|^mehr (anzeigen|laden)|^show more/i;
  for (const b of document.querySelectorAll('button, a, [role=button]')) {
    const t = (b.innerText || '').trim();
    if (rx.test(t) && b.offsetParent !== null) { b.scrollIntoView(); b.click(); return t; }
  }
  return null;
}
"""

# Bouton « suivant » ou numéro de page sans lien (pagination en JavaScript).
NEXT_BTN_JS = r"""
(n) => {
  const els = [...document.querySelectorAll('button, [role=button], li, span, a:not([href]), a[href="#"], a[href^="javascript"]')];
  const vis = e => e.offsetParent !== null && !e.disabled && !/disabled/.test(e.className || '');
  const lbl = e => ((e.innerText || '') + ' ' + (e.getAttribute('aria-label') || '')).trim();
  let t = els.find(e => vis(e) && /^(suivant|suivante|page suivante|next|weiter|›|»|>|→)$/i.test(lbl(e)))
       || els.find(e => vis(e) && lbl(e) === String(n) && e.children.length <= 1);
  if (!t) return false;
  t.scrollIntoView(); t.click(); return true;
}
"""

FIRST_LINKS_JS = r"""
() => [...document.querySelectorAll('a[href]')].slice(0, 400).map(a => a.href).join('|').length + ':' +
      [...document.querySelectorAll('a[href]')].map(a => a.href).filter(h => /\d{3,}/.test(h)).slice(0, 5).join('|')
"""

DETAIL_JS = r"""
() => {
  const main = document.querySelector('main, article, [role=main], #content, .content') || document.body;
  return (main.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 3500);
}
"""


# ---------------------------------------------------------------- utilitaires
NAV_PARAMS = re.compile(r"^(returnto|return|ref|referer|from|origin|back|page|p|sort|sortingfield|order|zoom|utm_.*|fbclid|gclid)$", re.I)


def norm_url(u: str) -> str:
    """URL sans les paramètres de navigation (retour à la page 2, tri, suivi…)."""
    s = urlsplit(u)
    q = "&".join(kv for kv in s.query.split("&") if kv and not NAV_PARAMS.match(kv.split("=")[0]))
    return urlunsplit((s.scheme, s.netloc.lower(), s.path.rstrip("/"), q, ""))


def card_hash(src_id: str, card: dict) -> str:
    digits = "".join(re.findall(r"\d", re.sub(r"https?://\S+", "", card["text"])))[:40]
    # Fiche sans lien propre : l'empreinte repose sur le texte seul.
    url = "" if card.get("weak") else norm_url(card["href"])
    return hashlib.sha1(f"{src_id}|{url}|{digits}|{card['text'][:60] if card.get('weak') else ''}".encode()).hexdigest()[:16]


def prefilter(text: str, cfg: dict) -> str:
    """'out' = ne peut pas être un logement compatible ; sinon 'check'."""
    rooms = [float(m.replace(",", ".")) for m in ROOMS.findall(text)]
    beds = [int(b) for b in BEDROOMS.findall(text)]
    prices = []
    for a, b in CHF.findall(text):
        v = re.sub(r"[^\d]", "", a or b)
        if v and 500 <= int(v) <= 50000:
            prices.append(int(v))
    if NOT_HOUSING.search(text) and not rooms and not beds:
        return "out"
    if rooms and max(rooms) < cfg.get("pieces_min", 3.5) and not (beds and max(beds) >= 3):
        return "out"
    if prices and min(prices) > cfg.get("loyer_max", 6000):
        return "out"
    return "check"


_robots: dict = {}


def robots_allows(url: str) -> bool | None:
    """True/False selon robots.txt (règles pour tous les robots) ; None si illisible."""
    s = urlsplit(url)
    base = f"{s.scheme}://{s.netloc}"
    if base not in _robots:
        rp = RobotFileParser()
        try:
            with urlopen(Request(base + "/robots.txt", headers={"User-Agent": UA}), timeout=15) as r:
                rp.parse(r.read().decode("utf-8", "ignore").splitlines())
            _robots[base] = rp
        except Exception:
            _robots[base] = None
    rp = _robots[base]
    return None if rp is None else rp.can_fetch("*", url)


async def settle(page, scrolls=5):
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    try:
        await page.evaluate(COOKIE_JS)
    except Exception:
        pass
    for _ in range(scrolls):
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(600)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass


async def discover(page, home: str):
    links = await page.evaluate(LINKS_JS)
    host = urlsplit(home).netloc.replace("www.", "")
    cands = []
    for l in links:
        h, t = l["href"], l["text"]
        if host not in urlsplit(h).netloc:
            continue
        blob = f"{t} {h}"
        if RENT_LINK.search(blob) and not SALE_LINK.search(blob):
            score = 3 if re.search(r"appartement|logement|r[ée]sidentiel|habitation", blob, re.I) else 0
            score += 2 if re.search(r"louer|location", t, re.I) else 0
            score -= len(urlsplit(h).path) / 200
            cands.append((score, h))
    cands.sort(reverse=True)
    return cands[0][1] if cands else None


async def next_page_url(page, visited: set):
    """Lien vers la page suivante de résultats, s'il y en a un."""
    links = await page.evaluate(LINKS_JS)
    cur = page.url
    best = None
    for l in links:
        h = l["href"].split("#")[0]
        if not h or h in visited or urlsplit(h).netloc != urlsplit(cur).netloc:
            continue
        label = f"{l['text']} {l['aria']}".strip().lower()
        if "next" in l["rel"] or re.fullmatch(r"(suivant(e)?|page suivante|next|weiter|›|»|>|→)\s*[›»>]?", label) \
                or re.search(r"page suivante|next page|nächste seite", label):
            return h
        # pagination numérotée : « 2 », « 3 »… on prend le plus petit numéro non visité
        if re.fullmatch(r"\d{1,3}", l["text"]) and re.search(r"(page|p|seite)[=/-]?\d", h, re.I):
            n = int(l["text"])
            if best is None or n < best[0]:
                best = (n, h)
    return best[1] if best else None


# ---------------------------------------------------------------- une source
async def crawl_source(ctx, src: dict, cfg: dict, seen: dict, full: bool, first_time: bool):
    page = await ctx.new_page()
    res = {"id": src["id"], "name": src["name"], "niveau": src.get("niveau"), "url": src["url"],
           "status": "ok", "cards": [], "pages": 0}
    try:
        allowed = robots_allows(src["url"])
        res["robots"] = {True: "ok", False: "interdit", None: "illisible"}[allowed]
        if allowed is False:
            res["status"] = "robots_disallow"
            return res
        resp = await page.goto(src["url"], wait_until="domcontentloaded", timeout=45000)
        res["http"] = resp.status if resp else None
        await settle(page)
        need_discover = src.get("discover") or (resp and resp.status >= 400)
        if need_discover:
            if resp and resp.status >= 400:
                root = f"{urlsplit(src['url']).scheme}://{urlsplit(src['url']).netloc}/"
                await page.goto(root, wait_until="domcontentloaded", timeout=45000)
                await settle(page, scrolls=1)
            target = await discover(page, page.url)
            if not target:
                res["status"] = "no_rent_page_found"
                return res
            if robots_allows(target) is False:
                res["status"] = "robots_disallow"
                res["resolved_url"] = target
                return res
            res["resolved_url"] = target
            await page.goto(target, wait_until="domcontentloaded", timeout=45000)
            await settle(page)

        cards, keys, visited = [], set(), {page.url.split("#")[0]}
        max_pages = int(src.get("max_pages", 25))
        start_url = page.url
        pages_without_new = 0
        try:
            while True:
                res["pages"] += 1
                # « voir plus » répété sur la même page
                for _ in range(15):
                    before = await page.evaluate("() => document.body.innerText.length")
                    clicked = await page.evaluate(MORE_JS)
                    if not clicked:
                        break
                    await page.wait_for_timeout(1500)
                    await settle(page, scrolls=2)
                    if await page.evaluate("() => document.body.innerText.length") <= before:
                        break
                added = new_here = 0
                for c in await page.evaluate(CARDS_JS):
                    k = (c["text"][:120] if c.get("weak") else norm_url(c["href"])) + "|" + c["text"][:60]
                    t = c["text"]
                    if k not in keys and LISTING_HINT.search(t) and (PRICE_HINT.search(t) or ROOMS.search(t)):
                        keys.add(k)
                        cards.append(c)
                        added += 1
                        if card_hash(src["id"], c) not in seen:
                            new_here += 1
                if res["pages"] >= max_pages:
                    break
                if res["pages"] > 1 and added == 0:          # la « page suivante » n'apporte rien
                    break
                # Passage incrémental : deux pages d'affilée sans aucune annonce
                # nouvelle = le reste est déjà connu (le passage complet du matin
                # relit tout, pour les sites qui ne trient pas par date).
                pages_without_new = 0 if new_here else pages_without_new + 1
                if not full and not first_time and pages_without_new >= 2:
                    res["stopped_early"] = True
                    break
                if src.get("page_param"):                     # pagination par paramètre d'URL
                    p = res["pages"] + 1
                    sep = "&" if "?" in start_url else "?"
                    nxt = re.sub(rf"([?&]){src['page_param']}=\d+", rf"\g<1>{src['page_param']}={p}", start_url) \
                        if re.search(rf"[?&]{src['page_param']}=", start_url) else f"{start_url}{sep}{src['page_param']}={p}"
                else:
                    nxt = await next_page_url(page, visited)
                if nxt:
                    if robots_allows(nxt) is False:
                        break
                    visited.add(nxt)
                    await page.goto(nxt, wait_until="domcontentloaded", timeout=45000)
                    await settle(page, scrolls=3)
                else:
                    # Pagination par boutons JavaScript (pas de lien) : on clique « suivant »
                    before = await page.evaluate(FIRST_LINKS_JS)
                    if not await page.evaluate(NEXT_BTN_JS, res["pages"] + 1):
                        break
                    await page.wait_for_timeout(1500)
                    await settle(page, scrolls=3)
                    if await page.evaluate(FIRST_LINKS_JS) == before:
                        break
        except Exception as e:
            # Une page qui plante en cours de route : on garde ce qui a été lu.
            if not cards:
                raise
            res["status"] = "partial"
            res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        if res["pages"] <= 1:
            try:
                res["nav_hints"] = await page.evaluate(NAV_HINTS_JS)
            except Exception:
                pass

        res["final_url"] = page.url
        res["title"] = await page.title()
        res["cards"] = cards
        if not cards:
            res["status"] = "no_cards"
            body = await page.evaluate("() => (document.body && document.body.innerText) || ''")
            res["page_excerpt"] = re.sub(r"\s+", " ", body)[:1500]
    except Exception as e:
        res["status"] = "error"
        res["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        await page.close()
    return res


async def read_detail(ctx, url: str) -> str | None:
    if robots_allows(url) is False:
        return None
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        return await page.evaluate(DETAIL_JS)
    except Exception:
        return None
    finally:
        await page.close()


# ---------------------------------------------------------------- programme
async def main():
    conf = yaml.safe_load((ROOT / "sources.yaml").read_text())
    pre = conf.get("prefiltre", {})
    sources = [dict(s, kind="regie") for s in conf.get("regies", [])] + \
              [dict(s, kind="portail") for s in conf.get("portails", [])]
    args = [a for a in sys.argv[1:] if a.strip()]
    full = "--full" in args or os.environ.get("FULL") == "1"
    only = set(a for a in args if not a.startswith("--"))
    if only:
        sources = [s for s in sources if s["id"] in only]

    DATA.mkdir(exist_ok=True)
    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    first_run = not seen
    known = DATA / "sources_vues.json"
    known_sources = set(json.loads(known.read_text())) if known.exists() else set()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent=UA, locale="fr-CH", viewport={"width": 1366, "height": 900},
                                        ignore_https_errors=True)   # certificats mal configurés chez certaines régies
        sem = asyncio.Semaphore(4)

        async def run(s):
            async with sem:
                first_time = s["id"] not in known_sources
                return await crawl_source(ctx, s, pre, seen, full, first_time)
        results = await asyncio.gather(*(run(s) for s in sources))

        new_cards, report = [], {"at": now, "first_run": first_run, "full": full, "sources": []}
        detail_jobs = []
        for s, r in zip(sources, results):
            n_new = n_check = 0
            for c in r["cards"]:
                h = card_hash(s["id"], c)
                if h in seen:
                    continue
                seen[h] = now
                n_new += 1
                card = {"hash": h, "source_id": s["id"], "source": s["name"], "kind": s["kind"],
                        "href": c["href"], "text": c["text"], "pre": prefilter(c["text"], pre),
                        "weak": bool(c.get("weak"))}
                if card["pre"] == "check":
                    n_check += 1
                    if not card["weak"] and n_check <= int(pre.get("details_max_par_source", 40)):
                        detail_jobs.append(card)
                new_cards.append(card)
            rep = {k: r.get(k) for k in ("id", "name", "niveau", "status", "robots", "http", "url",
                                         "resolved_url", "final_url", "title", "pages", "stopped_early", "error", "page_excerpt", "nav_hints")}
            rep.update(cards=len(r["cards"]), new=n_new, new_check=n_check)
            report["sources"].append(rep)

        dsem = asyncio.Semaphore(4)

        async def det(card):
            async with dsem:
                card["detail"] = await read_detail(ctx, card["href"])
        await asyncio.gather(*(det(c) for c in detail_jobs))
        await browser.close()

    batches = DATA / "batches"
    batches.mkdir(exist_ok=True)
    stamp = now.replace(":", "").replace("+0000", "Z")
    if new_cards:
        (batches / f"{stamp}.json").write_text(json.dumps(
            {"at": now, "first_run": first_run, "cards": new_cards}, ensure_ascii=False, indent=1))
    cutoff = datetime.fromtimestamp(time.time() - KEEP_DAYS * 86400, timezone.utc).strftime("%Y-%m-%d")
    for f in batches.glob("*.json"):
        if f.name[:10] < cutoff:
            f.unlink()
    (DATA / "index.json").write_text(json.dumps(
        {"at": now, "batches": sorted(f.name for f in batches.glob("*.json"))}, indent=1))
    (DATA / "run.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    seen_path.write_text(json.dumps(seen, indent=0))
    known.write_text(json.dumps(sorted(known_sources | {r["id"] for r in report["sources"] if r["cards"]})))

    for r in report["sources"]:
        print(f"{r['id']:<22} {r['status']:<18} pages={r['pages']:<3} fiches={r['cards']:<4} "
              f"nouvelles={r['new']:<4} à_trier={r['new_check']:<4} {r.get('final_url') or ''}")
    print(f"TOTAL nouvelles fiches: {len(new_cards)} (dont à trier: {sum(1 for c in new_cards if c['pre']=='check')})")


if __name__ == "__main__":
    asyncio.run(main())
