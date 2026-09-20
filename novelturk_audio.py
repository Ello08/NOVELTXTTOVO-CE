# -*- coding: utf-8 -*-
"""
novelturk.com -> TXT -> MP3 (sesli kitap)   (GitHub Actions, Google Colab veya yerel makine)

Komutlar
  scrape   Bolumleri novelturk.com'dan cekip TXT olarak kaydeder   (txt/<seri>/0001_Bolum_1.txt)
  plan     TXT klasorune bakip paralel is (matrix) plani uretir     (GitHub Actions icin)
  tts      TXT dosyalarini edge-tts ile MP3'e cevirir              (audio_out/<seri>/001_Bolum_1.mp3)
  all      scrape + tts art arda (varsayilan; Colab / yerel kullanim)
  probe    Indirme yapmadan erisim tanisi yazdirir
  voices   Kullanilabilir edge-tts seslerini listeler

Ornekler
  python novelturk_audio.py                                   # tum akis, ayarlar asagidaki SERIES'ten
  python novelturk_audio.py scrape --series solo-farming-in-the-tower --max-chapters 10
  python novelturk_audio.py tts --voice tr-TR-EmelNeural --start 101 --end 200
  python novelturk_audio.py voices --locale tr-TR

Nasil calisir
  Kazima: curl_cffi ile tarayici TLS/HTTP2 parmak izi taklit edilir, oturum ana sayfayla isitilir.
  Seri bilgisi HTML'den, alinamazsa WordPress REST API'sinden (wp-json) okunur. Bolum metni REST'ten,
  sonraki bolum "Sonraki" baglantisindan ya da REST bolum listesinden bulunur. Bolumler sirayla
  gezildigi icin kazima paralellesmez; yarim kalirsa onbellekten (novelturk_cache) devam eder.
  Ses: her TXT dosyasi parcalara bolunur, edge-tts ile seslendirilir (zaman asimi + yeniden deneme),
  parcalar tek bir MP3'te birlestirilir. Dosya numaralari tum seriye gore verildigi icin farkli
  isteki (shard) MP3'ler birbirinin ustune yazilmaz.

Ortam degiskenleri (hepsi istege bagli)
  NT_SERIES         "link1,link2"   SERIES listesini ezer
  NT_MAX_CHAPTERS   "5" / "0"|"all" (0 = tum seri)
  NT_OUT            calisma klasoru (onbellek burada; varsayilan: Colab'da /content, aksi halde .)
  NOVELTURK_PROXY   http://kullanici:sifre@host:port  (veri merkezi IP'si engelliyse)
"""
import sys, subprocess, importlib, os


def _pip(*pkgs):
    cmd = [sys.executable, "-m", "pip", "install", "-q", *pkgs]
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        subprocess.check_call(cmd + ["--break-system-packages"])


for _m, _p in [("curl_cffi", "curl_cffi"), ("bs4", "beautifulsoup4"), ("lxml", "lxml"),
               ("edge_tts", "edge-tts"), ("tqdm", "tqdm")]:
    try:
        importlib.import_module(_m)
    except ImportError:
        _pip(_p)

import argparse, asyncio, json, logging, math, random, re, time, unicodedata, html as html_lib
from pathlib import Path
from urllib.parse import urlparse, urlencode, quote
from curl_cffi import requests as cffi
from bs4 import BeautifulSoup, NavigableString, Comment
from tqdm.auto import tqdm
import edge_tts

log = logging.getLogger("novelturk_audio")

# ===================== AYARLAR =====================
SERIES = [
    "https://novelturk.com/novel/solo-farming-in-the-tower/",
    # "Solo Farming Tower",
]
MAX_CHAPTERS = 5        # None = serinin tamami. Yeni seride once 3 ile deneyin.
DELAY = (1.0, 2.2)      # bolumler arasi rastgele bekleme (sn)
DEFAULT_VOICE = "tr-TR-AhmetNeural"
# ===================================================

PROXY = os.getenv("NOVELTURK_PROXY") or None
BASE = "https://novelturk.com"
WORK = Path(os.getenv("NT_OUT") or ("/content" if Path("/content").exists() else "."))
WORK.mkdir(parents=True, exist_ok=True)

STATE = {"html": True}   # HTML sayfalari engelliyse False olur; bir daha denenmez


def warn(msg):
    print(f"\n⚠ {msg}", flush=True)
    if os.getenv("GITHUB_ACTIONS"):
        print(f"::warning::{msg}", flush=True)


# ---------------- HTTP katmani ----------------
class FetchError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


# En yeni tarayici parmak izlerinden eskiye; kurulu curl_cffi'de olmayanlar elenir.
_PREF = ["chrome136", "chrome133a", "chrome131", "chrome124", "chrome120", "chrome119",
         "chrome116", "chrome110"]


def _targets():
    try:
        from curl_cffi.requests import BrowserType
        have = {b.value for b in BrowserType}
    except Exception:
        have = set()
    return ([t for t in _PREF if t in have] or ["chrome120", "chrome110"]) + ["chrome"]


IMPERSONATE = _targets()
LANG = "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"
CHALLENGE = ("Just a moment", "cf-chl", "challenge-platform", "Attention Required", "cf_chl_opt")


def _site(url, referer):
    """Sec-Fetch-Site degeri: adres cubuguna yazma / ayni site / baska site."""
    if not referer:
        return "none"
    a, b = urlparse(url).netloc, urlparse(referer).netloc
    if a == b:
        return "same-origin"
    return "same-site" if a.split(".")[-2:] == b.split(".")[-2:] else "cross-site"


class Http:
    def __init__(self):
        self.i = 0
        self.s = None
        self.name = "?"
        self.warm = False
        self._new()

    def _new(self):
        proxies = {"http": PROXY, "https": PROXY} if PROXY else None
        for _ in range(len(IMPERSONATE)):
            name = IMPERSONATE[self.i % len(IMPERSONATE)]
            try:
                self.s, self.name, self.warm = cffi.Session(impersonate=name, proxies=proxies), name, False
                return
            except Exception:
                self.i += 1
        self.s, self.name, self.warm = cffi.Session(impersonate="chrome", proxies=proxies), "chrome", False

    def _headers(self, url, kind, referer):
        # User-Agent / sec-ch-ua'yi bilerek elle vermiyoruz: impersonate, TLS parmak iziyle
        # uyumlu setini kendisi ekler. Uyumsuz elle yazilmis UA, engel ihtimalini arttirir.
        h = {"Accept-Language": LANG}
        if kind == "json":
            h.update({"Accept": "application/json, text/plain, */*", "Sec-Fetch-Dest": "empty",
                      "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": _site(url, referer or BASE)})
        elif kind == "image":
            h.update({"Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                      "Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors",
                      "Sec-Fetch-Site": _site(url, referer or BASE)})
        else:
            h.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                "image/avif,image/webp,image/apng,*/*;q=0.8",
                      "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document",
                      "Sec-Fetch-Mode": "navigate", "Sec-Fetch-User": "?1",
                      "Sec-Fetch-Site": _site(url, referer)})
        if referer:
            h["Referer"] = referer
        return h

    def _once(self, url, kind, referer):
        return self.s.get(url, headers=self._headers(url, kind, referer), timeout=30,
                          allow_redirects=True)

    def _warm_up(self):
        """Gercek kullanici gibi once ana sayfaya girip cerezleri al."""
        self.warm = True
        try:
            self._once(BASE + "/", "html", None)
            time.sleep(random.uniform(0.6, 1.4))
        except Exception:
            pass

    @staticmethod
    def _challenged(r, kind):
        if r.headers.get("cf-mitigated") == "challenge":
            return True
        if kind == "image":
            return False
        head = r.text[:4000]
        if kind == "json" and not head.lstrip().startswith("<"):
            return False    # gecerli JSON; icerikte "Just a moment" gecebilir
        return any(m in head for m in CHALLENGE)

    def request(self, url, kind="html", referer=None, retries=3):
        last, status = "?", None
        for n in range(1, retries + 1):
            wait = None
            try:
                if not self.warm and url.rstrip("/") != BASE:
                    self._warm_up()
                r = self._once(url, kind, referer)
                status = r.status_code
                challenged = self._challenged(r, kind)
                if status == 200 and not challenged:
                    return r
                if status in (400, 401, 404, 410) and not challenged:
                    raise FetchError(f"{url} alinamadi (HTTP {status})", status)
                last = f"HTTP {status}" + (" / Cloudflare challenge" if challenged else "") \
                       + f" [{self.name}]"
                ra = r.headers.get("retry-after")
                if status == 429 and ra and ra.isdigit():
                    wait = min(int(ra), 60)
            except FetchError:
                raise
            except Exception as e:
                last = repr(e)
            self.i += 1
            self._new()          # farkli parmak izi + temiz oturum ile tekrar dene
            if n < retries:
                time.sleep(wait or min(2 ** n, 20) + random.random())
        raise FetchError(f"{url} alinamadi ({last})", status)

    def get(self, url, referer=None, binary=False, retries=3):
        r = self.request(url, "image" if binary else "html", referer, retries)
        return r.content if binary else r.text

    def get_json(self, url, referer=None, retries=3):
        r = self.request(url, "json", referer or BASE + "/", retries)
        try:
            return json.loads(r.text)
        except ValueError:
            raise FetchError(f"{url} gecerli JSON degil", r.status_code)


http = Http()


def wp_get(route, retries=3, **params):
    """WordPress REST GET. Once /wp-json/, olmazsa ?rest_route= bicimi denenir."""
    qs = urlencode({k: v for k, v in params.items() if v is not None})
    urls = [f"{BASE}/wp-json{route}?{qs}", f"{BASE}/?rest_route={quote(route)}&{qs}"]
    err = None
    for u in urls:
        try:
            return http.get_json(u, referer=BASE + "/", retries=retries)
        except FetchError as e:
            err = e
            if e.status in (400, 404):    # rota yok / sayfa sonu: ikinci bicim de ayni sonucu verir
                raise
    raise err


# ---------------- HTML -> temiz XHTML bloklari ----------------
BLOCK = {"p", "div", "section", "article", "main", "center", "blockquote", "ul", "ol", "li",
         "table", "thead", "tbody", "tr", "td", "th", "pre", "hr",
         "h1", "h2", "h3", "h4", "h5", "h6"}
KEEP = {"b", "strong", "i", "em", "u"}
NOISE = re.compile(r"(novel\s*t[üu]rk|novelturk\.com|bu bölümü paylaş|bu bölümde hata bildir|"
                   r"bildirimlere izin ver|bölüm yorumları)", re.I)
ZW = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


def plain(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def inline_html(node):
    out = []
    for ch in node.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            out.append(html_lib.escape(str(ch), quote=False))
        elif ch.name == "br":
            out.append("<br/>")
        elif ch.name in ("img", "script", "style", "svg", "ins"):
            continue
        elif ch.name in KEEP:
            t = inline_html(ch)
            if t.strip():
                out.append(f"<{ch.name}>{t}</{ch.name}>")
        else:
            out.append(inline_html(ch))
    return "".join(out)


def blocks_from(node, out):
    buf = []

    def flush():
        txt = "".join(buf).strip()
        buf.clear()
        for line in re.split(r"(?:<br/>\s*)+", txt):
            if plain(line):
                out.append(f"<p>{line.strip()}</p>")

    for ch in node.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            buf.append(html_lib.escape(str(ch), quote=False))
            continue
        n = ch.name
        if n in ("script", "style", "img", "svg", "noscript", "ins"):
            continue
        if n == "br":
            buf.append("<br/>")
            continue
        if n not in BLOCK:
            t = inline_html(ch) if n not in KEEP else f"<{n}>{inline_html(ch)}</{n}>"
            buf.append(t)
            continue
        flush()
        if n == "hr":
            out.append("<hr/>")
        elif re.fullmatch(r"h[1-6]", n):
            t = inline_html(ch).strip()
            if plain(t):
                out.append(f"<h3>{t}</h3>")
        elif n == "p":
            t = inline_html(ch).strip()
            if plain(t):
                out.append(f"<p>{t}</p>")
        elif n == "blockquote":
            inner = []
            blocks_from(ch, inner)
            if inner:
                out.append("<blockquote>" + "".join(inner) + "</blockquote>")
        elif n in ("ul", "ol"):
            for li in ch.find_all("li", recursive=False):
                t = inline_html(li).strip()
                if plain(t):
                    out.append(f"<p>• {t}</p>")
        elif n == "table":
            for tr in ch.find_all("tr"):
                cells = [inline_html(td).strip() for td in tr.find_all(["td", "th"])]
                cells = [c for c in cells if plain(c)]
                if cells:
                    out.append("<p>" + " | ".join(cells) + "</p>")
        elif n == "pre":
            out.append("<pre>" + html_lib.escape(ch.get_text()) + "</pre>")
        else:
            blocks_from(ch, out)
    flush()


def filigran_mi(t):
    """Unicode 'susleme' harfleriyle yazilmis site filigranlarini yakalar."""
    n = sum(1 for c in t if 0x1D400 <= ord(c) <= 0x1D7FF or 0x1F100 <= ord(c) <= 0x1F1FF
            or 0x2100 <= ord(c) <= 0x214F or 0x20A0 <= ord(c) <= 0x20CF
            or 0x2980 <= ord(c) <= 0x29FF or 0x3000 <= ord(c) <= 0x303F)
    return n >= 3


def to_blocks(node):
    """Bir HTML dugumunden temiz <p> bloklari cikarir; metin cok kisaysa None."""
    for t in node.find_all(["script", "ins", "style", "iframe", "noscript"]):
        t.extract()
    for t in node.select("div.ad-slot"):
        t.extract()
    blocks = []
    blocks_from(node, blocks)
    blocks = [ZW.sub("", b) for b in blocks]
    blocks = [b for b in blocks if not filigran_mi(plain(b))]
    blocks = [b for b in blocks if not (len(plain(b)) <= 150 and NOISE.search(plain(b)))]
    return blocks if len(plain(" ".join(blocks))) >= 100 else None


# ---------------- Site islemleri ----------------
def absolute(u):
    return u if u.startswith("http") else BASE + (u if u.startswith("/") else "/" + u)


def novel_url_of(x):
    x = x.strip()
    return x if x.startswith("http") else f"{BASE}/novel/{x.strip('/')}/"


def last_seg(u):
    return u.rstrip("/").split("/")[-1]


def chapter_num(s):
    m = re.search(r"bolum-(\d+)", s)
    return int(m.group(1)) if m else None


def novel_info_html(url):
    soup = BeautifulSoup(http.get(url, referer=BASE + "/"), "lxml")
    meta = lambda p: (soup.find("meta", property=p) or {}).get("content", "")
    h1 = soup.find("h1")
    a = soup.select_one('a[href*="nauthor="]')
    links = [(x.get_text(" ", strip=True), absolute(x["href"]))
             for x in soup.find_all("a", href=True) if "/bolum/" in x["href"]]
    first = next((h for t, h in links if re.search(r"[İI]LK", t)), None)
    last = next((h for t, h in links if re.search(r"\bSON\b", t)), None)
    if not first and links:   # yedek: numarasi en kucuk bolum
        num = lambda h: chapter_num(h) if chapter_num(h) is not None else 10 ** 9
        first = min((h for _, h in links), key=num)
    return {"title": h1.get_text(" ", strip=True) if h1 else "",
            "author": a.get_text(strip=True) if a else "",
            "desc": html_lib.unescape(meta("og:description")),
            "cover": html_lib.unescape(meta("og:image")),
            "first": first, "last": last, "url": url}


# ---- WordPress REST yedegi ----
def _txt(s):
    return html_lib.unescape(plain(s or ""))


def rest_types():
    """{rest_base: tur_slug} - sitenin REST'e acik icerik turleri."""
    try:
        d = wp_get("/wp/v2/types", retries=2)
        return {(v.get("rest_base") or k): k for k, v in d.items()}
    except FetchError as e:
        if e.status in (400, 404):
            return {}
        raise


def find_novel_post(slug):
    """Seri yazisini REST'te bulur: /types ile kesfedilen turler, sonra posts/pages, sonra arama."""
    types = rest_types()
    is_novel = lambda b: (re.search(r"novel|seri|roman", b + types[b], re.I)
                          and not re.search(r"chapter|bolum|b[öo]l[üu]m", b + types[b], re.I))
    bases = [b for b in types if is_novel(b)]
    bases += [b for b in ("novel", "novels", "series", "seri", "posts", "pages")
              if b not in bases and (not types or b in types)]
    for b in bases:
        try:
            arr = wp_get(f"/wp/v2/{b}", retries=2, slug=slug, _embed=1)
        except FetchError as e:
            if e.status in (400, 404):
                continue
            raise
        if arr:
            return arr[0]
    # Son care: genel arama (tum acik turlerde)
    try:
        res = wp_get("/wp/v2/search", retries=2, search=slug.replace("-", " "), per_page=10,
                     _fields="id,url,subtype")
    except FetchError:
        res = []
    for r in res:
        if slug in r.get("url", ""):
            base = next((k for k, v in types.items() if v == r.get("subtype")), r.get("subtype"))
            try:
                return wp_get(f"/wp/v2/{base}/{r['id']}", retries=2, _embed=1)
            except FetchError:
                continue
    raise FetchError(f"REST API'de '{slug}' serisi bulunamadi", 404)


def novel_info_rest(slug, url):
    p = find_novel_post(slug)
    y = p.get("yoast_head_json") or {}
    emb = p.get("_embedded") or {}
    title = _txt((p.get("title") or {}).get("rendered"))
    desc = (y.get("og_description") or y.get("description")
            or _txt((p.get("excerpt") or {}).get("rendered"))
            or _txt((p.get("content") or {}).get("rendered"))[:600])
    cover = (next((i.get("url") for i in (y.get("og_image") or []) if i.get("url")), "")
             or ((emb.get("wp:featuredmedia") or [{}])[0] or {}).get("source_url", "")
             or p.get("jetpack_featured_media_url", ""))
    author = ""
    for group in emb.get("wp:term") or []:
        for t in group or []:
            if "author" in (t.get("taxonomy") or "") and t.get("name"):
                author = t["name"]
    return {"title": title, "author": author, "desc": desc, "cover": cover,
            "first": None, "last": None, "url": url}


def rest_chapter_index(info):
    """[(slug, link), ...] bolum numarasina gore sirali. Bolumler novel adiyla aranir."""
    slug = last_seg(info["url"])
    found = {}
    for term in dict.fromkeys([info["title"], slug.replace("-", " ")]):
        if not term:
            continue
        for page in range(1, 101):
            try:
                arr = wp_get("/wp/v2/chapter", retries=2, search=term, per_page=100, page=page,
                             orderby="date", order="asc", _fields="id,slug,link,title")
            except FetchError as e:
                if e.status in (400, 404):    # sayfa sonu
                    break
                raise
            for a in arr:
                found.setdefault(a["slug"], a)
            if len(arr) < 100:
                break
        if found:
            break
    mine = {k: v for k, v in found.items() if slug in k or slug in v.get("link", "")}
    if found and not mine:
        warn("Bolum listesi seri adina gore suzulemedi; arama sonuclari oldugu gibi kullaniliyor.")
    found = mine or found
    order = sorted(enumerate(found.values()),
                   key=lambda t: (chapter_num(t[1]["slug"]) if chapter_num(t[1]["slug"]) is not None
                                  else 10 ** 9, t[0]))
    return [(v["slug"], v["link"]) for _, v in order]


def index_of(info):
    if info.get("_idx") is None:
        try:
            info["_idx"] = rest_chapter_index(info)
        except FetchError as e:
            warn(f"Bolum listesi REST'ten alinamadi: {e}")
            info["_idx"] = []
    return info["_idx"]


def index_next(info, cur):
    idx = index_of(info)
    slugs = [s for s, _ in idx]
    cs = last_seg(cur)
    if cs in slugs:
        i = slugs.index(cs)
        return idx[i + 1][1] if i + 1 < len(idx) else None
    return None


def novel_info(url):
    slug = last_seg(url)
    h = {}
    if STATE["html"]:
        try:
            h = novel_info_html(url)
        except FetchError as e:
            STATE["html"] = False
            warn(f"Ana sayfa HTML'i alinamadi ({e}). WordPress REST API yedegine geciliyor.")
    r = {}
    if not h.get("first") or not h.get("title"):
        try:
            r = novel_info_rest(slug, url)
        except FetchError as e:
            warn(f"REST API'den seri bilgisi alinamadi: {e}")
    info = {**r, **{k: v for k, v in h.items() if v}}
    info.update(url=url, title=info.get("title") or slug.replace("-", " ").title(),
                author=info.get("author") or "Bilinmiyor", desc=info.get("desc", ""),
                cover=info.get("cover", ""), first=info.get("first"), last=info.get("last"))
    if not info["first"]:
        idx = index_of(info)
        if idx:
            info["first"], info["last"] = idx[0][1], info["last"] or idx[-1][1]
    return info


def chapter_page(url, referer):
    soup = BeautifulSoup(http.get(url, referer=referer), "lxml")
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    nxt = None
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower() == "sonraki" and "/bolum/" in a["href"]:
            nxt = absolute(a["href"])
            break
    return title, nxt, soup


def rest_chapter(slug):
    arr = wp_get("/wp/v2/chapter", slug=slug, _fields="id,slug,title,content")
    if not arr:
        return "", None
    raw = ZW.sub("", arr[0]["content"]["rendered"])
    soup = BeautifulSoup("<div>" + raw + "</div>", "lxml")
    return _txt((arr[0].get("title") or {}).get("rendered")), to_blocks(soup.find("div"))


def html_blocks(soup):
    """Son care: REST calismazsa bolum metnini HTML sayfasindan oku."""
    for sel in ("div.entry-content", "div.chapter-content", "#chapter-content",
                "div.reading-content", "article"):
        node = soup.select_one(sel)
        b = to_blocks(node) if node else None
        if b:
            return b
    return None


def fetch_chapter(info, cur, ref):
    """{'title','blocks','next'} dondurur. HTML engelliyse REST'e duser."""
    cs = last_seg(cur)
    title = nxt = soup = None
    if STATE["html"]:
        try:
            title, nxt, soup = chapter_page(cur, ref)
        except FetchError as e:
            STATE["html"] = False
            warn(f"Bolum HTML'i alinamadi ({e}); bundan sonra yalnizca REST kullanilacak.")
    rtitle, blocks = "", None
    try:
        rtitle, blocks = rest_chapter(cs)
    except FetchError as e:
        if soup is None:
            raise
        warn(f"REST bolum icerigi alinamadi ({e}); metin HTML'den okunacak.")
    if not blocks and soup is not None:
        blocks = html_blocks(soup)
    is_last = bool(info["last"]) and cur.rstrip("/") == info["last"].rstrip("/")
    if not nxt and not is_last:
        nxt = index_next(info, cur)
    title = title or rtitle or cs
    title = title.replace(info["title"], "", 1).strip(" –—-") or title
    return {"title": title, "blocks": blocks, "next": nxt}


# ---------------- Metin temizleme / dosya adi ----------------
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def clean_line(text):
    """Unicode normalize eder, gorunmez karakterleri atar, bosluklari sadelestirir."""
    text = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)
    return re.sub(r"\s+", " ", text).strip()


def slugify(text, max_len=50):
    text = text.translate(_TR_MAP)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return text[:max_len].strip("_")


def blocks_to_lines(blocks):
    """Kazinan <p>/<h3>/<blockquote>... bloklarini duz metin satirlarina cevirir."""
    lines = []
    for b in blocks:
        if b == "<hr/>":
            lines.append("* * *")
            continue
        s = re.sub(r"</p>|</h3>|<br\s*/?>|</pre>", "\n", b)
        s = html_lib.unescape(re.sub(r"<[^>]+>", "", s))
        lines.extend(x for x in (clean_line(t) for t in s.split("\n")) if x)
    return lines


def write_txt(series_dir, idx, d):
    """Bolumu 0001_Bolum_1.txt olarak yazar: ilk satir baslik, sonra bos satirla ayrilmis paragraflar."""
    title = clean_line(d["title"]) or f"Bolum {idx}"
    name = f"{idx:04d}_{slugify(title) or f'Bolum_{idx}'}.txt"
    for old in series_dir.glob(f"{idx:04d}_*.txt"):    # baslik degistiyse eski dosya kalmasin
        if old.name != name:
            old.unlink()
    body = "\n\n".join(blocks_to_lines(d["blocks"]))
    (series_dir / name).write_text(f"{title}\n\n{body}\n", encoding="utf-8")


_TXT_RE = re.compile(r"^(\d+)_.+\.txt$")


def chapter_files(series_dir):
    """[(bolum_no, yol), ...] bolum numarasina gore sirali."""
    out = []
    for f in series_dir.glob("*.txt"):
        m = _TXT_RE.match(f.name)
        if m:
            out.append((int(m.group(1)), f))
    return sorted(out)


def series_dirs(root):
    return sorted(d for d in root.iterdir() if d.is_dir() and chapter_files(d))


def mp3_name(txt_path, width):
    """0001_Bolum_1.txt -> 001_Bolum_1.mp3 (en az 3 hane; epub_to_audio.py ile ayni duzen)."""
    m = re.match(r"^(\d+)_(.+)\.txt$", txt_path.name)
    return f"{int(m.group(1)):0{width}d}_{m.group(2)}.mp3"


def read_txt(path):
    """(baslik, govde) dondurur; sadece sembol iceren satirlar ('* * *') atilir."""
    lines = [clean_line(x) for x in path.read_text(encoding="utf-8").splitlines()]
    lines = [x for x in lines if x]
    title = lines[0] if lines else ""
    body = [x for x in lines[1:] if re.search(r"\w", x)]
    return title, "\n".join(body)


# ---------------- Kazima ----------------
def parse_max(v):
    v = str(v).strip().lower()
    return None if v in ("0", "all", "none") else int(v)


def series_list(args):
    raw = (args.series or "").strip()
    if not raw:
        return list(SERIES)
    return [s.strip() for s in re.split(r"[,\n]", raw) if s.strip()]


def scrape_series(entry, txt_root, max_chapters):
    """Seriyi bolum bolum gezip TXT yazar; kaydedilen bolum sayisini dondurur."""
    url = novel_url_of(entry)
    slug = last_seg(url)
    print(f"\n=== {slug} ===", flush=True)
    info = novel_info(url)
    if not info["first"]:
        print("✗ Ilk bolum bulunamadi (HTML ve REST denendi), seri atlandi.")
        return 0
    print(f"{info['title']} | Yazar: {info['author']} | Ilk: {last_seg(info['first'])} | "
          f"Son: {last_seg(info['last']) if info['last'] else '?'}", flush=True)

    cache = WORK / "novelturk_cache" / slug
    cache.mkdir(parents=True, exist_ok=True)
    series_dir = txt_root / slug
    series_dir.mkdir(parents=True, exist_ok=True)
    (series_dir / "_meta.json").write_text(
        json.dumps({k: info.get(k, "") for k in ("title", "author", "desc", "url")},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    saved, failed, seen = 0, [], set()
    cur, ref = info["first"], url
    bar = tqdm(total=max_chapters, desc=slug[:25], disable=None)   # TTY yoksa (Actions) kapali

    while cur and cur.rstrip("/") not in seen:
        if max_chapters is not None and saved + len(failed) >= max_chapters:
            break
        seen.add(cur.rstrip("/"))
        cp = cache / (last_seg(cur) + ".json")
        if cp.exists():
            d = json.loads(cp.read_text(encoding="utf-8"))
            if not d.get("next") and not (info["last"] and cur.rstrip("/") == info["last"].rstrip("/")):
                d["next"] = index_next(info, cur)
        else:
            try:
                d = fetch_chapter(info, cur, ref)
            except FetchError as e:
                warn(f"{last_seg(cur)} alinamadi: {e}")
                break
            if d["blocks"]:
                cp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            time.sleep(random.uniform(*DELAY))
        if d["blocks"]:
            saved += 1
            write_txt(series_dir, saved, d)     # anlik yaz: is yarida kesilse de TXT'ler kalir
        else:
            print(f"\n✗ Icerik alinamadi: {cur}")
            failed.append(cur)
        bar.update(1)
        n = saved + len(failed)
        if n % 10 == 0:
            print(f"  {n} bolum islendi...", flush=True)
        if info["last"] and cur.rstrip("/") == info["last"].rstrip("/"):
            break
        ref, cur = cur, d["next"]
    bar.close()

    if not saved:
        print("Hic bolum alinamadi.")
        return 0
    print(f"✓ {saved} bolum -> {series_dir}", flush=True)
    for f in failed:
        warn(f"alinamayan: {f}")
    return saved


def cmd_scrape(args):
    txt_root = Path(args.txt_dir)
    txt_root.mkdir(parents=True, exist_ok=True)
    max_ch = parse_max(args.max_chapters)
    total, empty = 0, []
    for s in series_list(args):
        try:
            n = scrape_series(s, txt_root, max_ch)
        except Exception as e:  # noqa: BLE001
            print(f"✗ {s} atlandi: {e!r}", flush=True)
            n = 0
        total += n
        if not n:
            empty.append(s)
    print(f"\nToplam {total} bolum TXT olarak kaydedildi: {txt_root.resolve()}", flush=True)
    for s in empty:
        warn(f"Hic bolum alinamayan seri: {s}")
    return 0 if total else 1     # kismi basari da basaridir; hicbir sey yoksa Actions kirmizi olsun


# ---------------- Ses uretimi (edge-tts) ----------------
def _hard_split(sentence, max_chars):
    """Cok uzun tek bir cumleyi kelime sinirlarindan boler."""
    parts, cur = [], ""
    for word in sentence.split(" "):
        if len(word) > max_chars:  # patolojik durum: bosluksuz dev kelime
            if cur:
                parts.append(cur)
                cur = ""
            parts.extend(word[i:i + max_chars] for i in range(0, len(word), max_chars))
        elif cur and len(cur) + 1 + len(word) > max_chars:
            parts.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        parts.append(cur)
    return parts


def split_text(text, max_chars):
    """Metni paragraf/cumle sinirlarini koruyarak en fazla max_chars'lik parcalara boler."""
    chunks, buf = [], ""

    def flush():
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for paragraph in text.split("\n"):
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            pieces = [sentence] if len(sentence) <= max_chars else _hard_split(sentence, max_chars)
            for piece in pieces:
                if buf and len(buf) + 1 + len(piece) > max_chars:
                    flush()
                sep = "" if (not buf or buf.endswith("\n")) else " "
                buf = f"{buf}{sep}{piece}"
        if buf and not buf.endswith("\n"):
            buf += "\n"  # paragraf sonu
    flush()
    return [c for c in chunks if re.search(r"\w", c)]


def spoken_text(title, body):
    """Basligi (sonuna nokta koyarak, dogal bir duraklama icin) govdenin onune ekler."""
    if not title:
        return body
    if title[-1] not in ".!?…:;":
        title += "."
    return f"{title}\n{body}"


async def _stream_audio(text, args):
    comm = edge_tts.Communicate(text, args.voice, rate=args.rate, volume=args.volume, pitch=args.pitch)
    buf = bytearray()
    async for msg in comm.stream():
        if msg["type"] == "audio":
            buf.extend(msg["data"])
    if not buf:
        raise RuntimeError("Servisten ses verisi alinamadi")
    return bytes(buf)


async def synth_chunk(text, args, label):
    """Tek bir parcayi zaman asimi + ustel geri cekilmeli yeniden deneme ile sentezler."""
    last_exc = None
    for attempt in range(1, args.retries + 1):
        try:
            return await asyncio.wait_for(_stream_audio(text, args), timeout=args.chunk_timeout or None)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < args.retries:
                delay = min(2 ** attempt, 30)
                log.warning("%s: deneme %d/%d basarisiz (%s: %s); %d sn sonra tekrar denenecek",
                            label, attempt, args.retries, type(exc).__name__, exc, delay)
                await asyncio.sleep(delay)
    raise RuntimeError(f"{label}: {args.retries} denemede sentezlenemedi") from last_exc


async def convert_file(idx, txt_path, out_path, total, args, sem):
    """Bir TXT dosyasini MP3'e cevirir. Basarili ya da atlandiysa True."""
    if out_path.exists() and not args.overwrite:
        log.info("[%d/%d] Zaten var, atlandi: %s", idx, total, out_path.name)
        return True

    title, body = read_txt(txt_path)
    if not body:
        log.warning("[%d/%d] Okunacak metin yok, atlandi: %s", idx, total, txt_path.name)
        return True

    async with sem:
        text = spoken_text(title, body)
        chunks = split_text(text, args.max_chars)
        log.info("[%d/%d] %s  (%d karakter, %d parca)", idx, total, out_path.name, len(text), len(chunks))
        tmp = out_path.with_name(out_path.name + ".part")
        try:
            with tmp.open("wb") as fh:
                for i, chunk in enumerate(chunks, 1):
                    fh.write(await synth_chunk(chunk, args, f"{out_path.name} parca {i}/{len(chunks)}"))
            tmp.replace(out_path)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("[%d/%d] BASARISIZ: %s -> %s", idx, total, out_path.name, exc)
            tmp.unlink(missing_ok=True)
            return False


async def tts_series(series_dir, out_dir, args):
    files = chapter_files(series_dir)
    if not files:
        log.error("TXT dosyasi bulunamadi: %s", series_dir)
        return 0, 1
    total = files[-1][0]
    end = args.end or total
    files = [(i, p) for i, p in files if args.start <= i <= end]
    if args.limit:
        files = files[: args.limit]
    if not files:
        log.warning("%s: %d-%d araliginda bolum yok (toplam %d)", series_dir.name, args.start, end, total)
        return 0, 0

    log.info("Seri: %s | bolum %d-%d (toplam %d) | ses: %s",
             series_dir.name, files[0][0], files[-1][0], total, args.voice)
    out_dir.mkdir(parents=True, exist_ok=True)
    width = max(3, len(str(total)))
    sem = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*(
        convert_file(i, p, out_dir / mp3_name(p, width), total, args, sem) for i, p in files
    ))
    ok = sum(results)
    return ok, len(results) - ok


def check_tts_args(args):
    """rate/pitch/volume/voice bicimini bastan dogrular; hata mesajini dondurur (yoksa None)."""
    if args.max_chars < 200:
        return "--max-chars en az 200 olmali"
    if args.start < 1:
        return "--start en az 1 olmali"
    if args.end < 0 or (args.end and args.end < args.start):
        return "--end, --start degerinden kucuk olamaz (0 = sonuna kadar)"
    args.concurrency = max(1, args.concurrency)
    args.retries = max(1, args.retries)
    try:
        edge_tts.Communicate("test", args.voice, rate=args.rate, volume=args.volume, pitch=args.pitch)
    except (ValueError, TypeError) as exc:
        return f"Gecersiz ses ayari: {exc}"
    return None


def run_async(coro):
    """asyncio.run; Jupyter/Colab gibi calisan bir dongu varsa ayri thread'de calistirir."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import threading
    box = {}

    def worker():
        try:
            box["r"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["r"]


def cmd_tts(args):
    err = check_tts_args(args)
    if err:
        log.error(err)
        return 2
    root = Path(args.txt_dir)
    if not root.is_dir():
        log.error("TXT klasoru yok: %s", root)
        return 1
    dirs = [root / args.slug] if args.slug else series_dirs(root)
    if not dirs or not all(d.is_dir() for d in dirs):
        log.error("Seslendirilecek seri klasoru bulunamadi (%s)", args.slug or root)
        return 1

    total_ok = total_fail = 0
    for d in dirs:
        ok, fail = run_async(tts_series(d, Path(args.out_dir) / d.name, args))
        total_ok += ok
        total_fail += fail
    log.info("Bitti: %d bolum basarili, %d basarisiz. Cikti: %s",
             total_ok, total_fail, Path(args.out_dir).resolve())
    return 0 if total_fail == 0 else 1


# ---------------- Paralel is plani (GitHub Actions matrix) ----------------
def cmd_plan(args):
    root = Path(args.txt_dir)
    per = max(1, args.per_job)
    include, notes = [], []
    for d in (series_dirs(root) if root.is_dir() else []):
        total = max(i for i, _ in chapter_files(d))
        n = math.ceil(total / per)
        size = math.ceil(total / n)       # parcalari dengeli bol (890 bolum, 100 -> 9 x 99)
        for k in range(n):
            s, e = k * size + 1, min((k + 1) * size, total)
            if s <= total:
                include.append({"shard": len(include) + 1, "slug": d.name, "start": s, "end": e})
        notes.append(f"- `{d.name}`: **{total}** bölüm → {n} paralel parça (parça başına ~{size} bölüm)")

    if not include:
        print("::error::Planlanacak TXT bölümü bulunamadı.", file=sys.stderr)
        return 1
    if len(include) > 256:
        print(f"::error::{len(include)} parça, GitHub matrix sınırı olan 256'yı aşıyor; "
              "chapters_per_job değerini büyütün.", file=sys.stderr)
        return 1

    matrix = json.dumps({"include": include}, separators=(",", ":"))
    print(matrix)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"matrix={matrix}\nshards={len(include)}\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("### Plan\n" + "\n".join(notes) + f"\n\nToplam paralel job: **{len(include)}**\n")
    print(f"Plan: {len(include)} parça", file=sys.stderr)
    return 0


# ---------------- Yardimci komutlar ----------------
def cmd_probe(args):
    """Indirme yapmadan hangi yolun acik oldugunu gosterir (sonucu paylasarak tani konulabilir)."""
    url = novel_url_of(series_list(args)[0])
    print(f"Parmak izi adaylari: {IMPERSONATE} | proxy: {'var' if PROXY else 'yok'}")
    tests = [("HTML ana sayfa", BASE + "/", "html"), ("HTML seri", url, "html"),
             ("REST kok", f"{BASE}/wp-json/", "json"),
             ("REST types", f"{BASE}/wp-json/wp/v2/types", "json"),
             ("REST chapter", f"{BASE}/wp-json/wp/v2/chapter?per_page=1&_fields=id,slug", "json"),
             ("REST rest_route", f"{BASE}/?rest_route=/wp/v2/types", "json")]
    for name, u, kind in tests:
        try:
            r = http._once(u, kind, None if kind == "html" else BASE + "/")
            print(f"{name:16} HTTP {r.status_code} | server={r.headers.get('server')} | "
                  f"cf-mitigated={r.headers.get('cf-mitigated')} | {r.text[:70]!r}")
        except Exception as e:
            print(f"{name:16} HATA {e!r}")
    return 0


def cmd_voices(args):
    async def go():
        for v in sorted(await edge_tts.list_voices(), key=lambda v: v["ShortName"]):
            if v["Locale"].lower().startswith(args.locale.lower()):
                print(f'{v["ShortName"]:<28} {v["Gender"]}')
    run_async(go())
    return 0


def cmd_all(args):
    rc = cmd_scrape(args)
    return rc if rc else cmd_tts(args)


# ---------------- CLI ----------------
def _normalize_argv(argv):
    """'--rate -15%' argparse'ta secenek sanilir; '--rate=-15%' bicimine cevirir."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] in ("--rate", "--volume", "--pitch") and i + 1 < len(argv):
            out.append(f"{argv[i]}={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="novelturk.com bolumlerini TXT olarak ceker ve edge-tts ile MP3'e cevirir.")
    p.add_argument("command", nargs="?", default="all",
                   choices=["all", "scrape", "plan", "tts", "probe", "voices"],
                   help="calistirilacak adim (varsayilan: all)")
    p.add_argument("--series", default=os.getenv("NT_SERIES", ""),
                   help="Seri linki veya slug (virgulle ayirin). Bos ise dosyadaki SERIES kullanilir")
    p.add_argument("--max-chapters", default=os.getenv("NT_MAX_CHAPTERS") or str(MAX_CHAPTERS),
                   help="Seri basina bolum siniri (0 veya all = tum seri)")
    p.add_argument("--txt-dir", default=str(WORK / "txt"), help="TXT klasoru (varsayilan: <calisma>/txt)")
    p.add_argument("--out-dir", "-o", default=str(WORK / "audio_out"),
                   help="MP3 klasoru (varsayilan: <calisma>/audio_out)")
    p.add_argument("--slug", default="", help="tts: sadece bu seriyi (klasor adi) seslendir")
    p.add_argument("--start", type=int, default=1, help="tts: ilk bolum numarasi (dahil)")
    p.add_argument("--end", type=int, default=0, help="tts: son bolum numarasi (dahil, 0 = sonuna kadar)")
    p.add_argument("--limit", type=int, default=0, help="tts: secilen aralıktan sadece ilk N bolum (test icin)")
    p.add_argument("--per-job", type=int, default=100, help="plan: paralel job basina en fazla bolum")
    p.add_argument("--voice", "-v", default=DEFAULT_VOICE, help=f"Edge TTS sesi (varsayilan: {DEFAULT_VOICE})")
    p.add_argument("--rate", default="+0%", help="Konusma hizi, ornek: +10%% veya -15%%")
    p.add_argument("--volume", default="+0%", help="Ses seviyesi, ornek: +10%%")
    p.add_argument("--pitch", default="+0Hz", help="Ses perdesi, ornek: +5Hz")
    p.add_argument("--max-chars", type=int, default=3000, help="Tek seferde sentezlenecek en fazla karakter")
    p.add_argument("--retries", type=int, default=5, help="Parca basina yeniden deneme sayisi")
    p.add_argument("--chunk-timeout", type=float, default=120.0, help="Parca basina zaman asimi (sn, 0 = kapali)")
    p.add_argument("--concurrency", type=int, default=3, help="Ayni anda islenecek bolum sayisi")
    p.add_argument("--overwrite", action="store_true", help="Var olan MP3 dosyalarinin uzerine yaz")
    p.add_argument("--locale", default="tr-TR", help="voices: listelenecek dil (varsayilan: tr-TR)")
    p.add_argument("--verbose", action="store_true", help="Ayrintili log")
    # Colab/Jupyter kendi argumanlarini (-f kernel.json) gecirir; onlari yok say
    raw = sys.argv[1:] if argv is None else list(argv)
    args, _ = p.parse_known_args([] if "google.colab" in sys.modules else _normalize_argv(raw))
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    cmds = {"all": cmd_all, "scrape": cmd_scrape, "plan": cmd_plan, "tts": cmd_tts,
            "probe": cmd_probe, "voices": cmd_voices}
    return cmds[args.command](args)


if __name__ == "__main__":
    rc = main()
    if rc:
        sys.exit(rc)
