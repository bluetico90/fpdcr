"""UNAFUT Live · servidor local. Fuente de consulta pública: futstatscr.com.

No es una API oficial de UNAFUT. Refresco conservador: 5 minutos.
"""
from __future__ import annotations
import copy
import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET
from urllib.parse import quote

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SOURCE = 'https://futstatscr.com/'
# Costa Rica usa UTC-6 permanentemente; evita dependencia tzdata en Windows.
TZ = timezone(timedelta(hours=-6), name='Costa Rica')
TTL = 300
PORT = int(os.environ.get('PORT', '8765'))
# Render proporciona PORT y necesita 0.0.0.0; Windows permanece limitado a localhost.
HOST = os.environ.get('HOST', '0.0.0.0' if os.environ.get('RENDER') or os.environ.get('PORT') else '127.0.0.1')
OPEN_BROWSER = HOST in ('127.0.0.1', 'localhost') and os.environ.get('OPEN_BROWSER', '1') == '1'
FORCE_MIN_INTERVAL = 60  # protege la fuente externa de refrescos masivos desde la web pública
TEAMS = {
    'DS': ('Deportivo Saprissa', '#74308e'),
    'CSC': ('Cartaginés', '#1d64b0'),
    'SSJ': ('Sporting FC', '#3c424c'),
    'LDA': ('Alajuelense', '#cb273c'),
    'SC': ('San Carlos', '#df3c3b'),
    'ISC': ('Inter San Carlos', '#bf9e25'),
    'PFC': ('Puntarenas FC', '#e87829'),
    'CSH': ('Herediano', '#e4bc29'),
    'EB': ('Escorpiones', '#b68b37'),
    'PZ': ('Pérez Zeledón', '#2781c0'),
}
# Escudos alternativos verificados de Wikimedia Commons. Se prioriza el escudo actualizado de la fuente.
LOGO_FALLBACK = {
  'DS': 'https://commons.wikimedia.org/wiki/Special:FilePath/Escudo%20Saprissa%202012.svg',
  'CSC': 'https://commons.wikimedia.org/wiki/Special:FilePath/Escudo%20del%20Club%20Sport%20Cartagin%C3%A9s.svg',
  'SSJ': 'https://commons.wikimedia.org/wiki/Special:FilePath/Escudo%20Sporting%20SJ.svg',
  'LDA': 'https://commons.wikimedia.org/wiki/Special:FilePath/Logo%20de%20la%20Liga%20Deportiva%20Alajuelense.svg',
  'CSH': 'https://commons.wikimedia.org/wiki/Special:FilePath/Escudo%20del%20Club%20Sport%20Herediano.svg',
  'PZ': 'https://commons.wikimedia.org/wiki/Special:FilePath/Escudo%20de%20la%20Asociaci%C3%B3n%20Deportiva%20Municipal%20de%20P%C3%A9rez%20Zeled%C3%B3n.png',
}

CODE_PATTERN = '|'.join(sorted(TEAMS, key=len, reverse=True))
RESULT_REGEX = re.compile(rf'\b({CODE_PATTERN})\s+(\d+)\s*[-–:]\s*({CODE_PATTERN})\s+(\d+)\b')
MATCH_REGEX = re.compile(rf'\b({CODE_PATTERN})\s*({CODE_PATTERN})?\s*(\d+)\s*[–-]\s*(\d+)\s*({CODE_PATTERN})\b')

# Referencia de arranque: consulta del 24/10/2026, no fingir datos en tiempo real.
BOOT = [
    ('DS',12,24,8,0,4,26,12,'LWLWL'),
    ('CSC',12,24,8,0,4,23,14,'WWLLW'),
    ('SSJ',13,24,7,3,3,26,20,'WD LWW'.replace(' ','')),
    ('LDA',12,19,5,4,3,20,14,'WDDWL'),
    ('SC',12,19,6,1,5,13,14,'DLWWW'),
    ('ISC',12,18,5,3,4,14,17,'LLWDL'),
    ('PFC',12,15,4,3,5,12,18,'WWLDW'),
    ('CSH',12,11,3,2,7,12,16,'WDLLW'),
    ('EB',13,10,3,1,9,14,22,'LLWWL'),
    ('PZ',12,9,2,3,7,9,22,'LLDLL'),
]

def seed():
    teams = []
    for code,pj,pts,g,e,p,gf,gc,form in BOOT:
        name,color=TEAMS[code]
        teams.append(dict(code=code,name=name,color=color,pj=pj,pts=pts,g=g,e=e,p=p,gf=gf,gc=gc,gd=gf-gc,form=list(form)[:5],logo=LOGO_FALLBACK.get(code)))
    return dict(teams=teams, matches=[], source=SOURCE, source_time='2026-10-24', fetched_at=None, live=False,
                stale=True, status='Sin conexión: se muestra una referencia del 24/10/2026.', round=13, projected=False)

def get_html(url=SOURCE):
    req=Request(url, headers={'User-Agent':'Mozilla/5.0 (compatible; UnafutPersonalDashboard/1.0)', 'Accept-Language':'es-CR,es;q=0.9'})
    with urlopen(req, timeout=18) as resp:
        if resp.status != 200:
            raise ValueError('HTTP %s'%resp.status)
        return resp.read(3_000_000).decode('utf-8','replace')

def img_url(el):
    if not el: return None
    for key in ('src','data-src','data-original'):
        value=el.get(key)
        if value and not value.startswith('data:'):
            url=urljoin(SOURCE,value)
            if urlparse(url).scheme=='https': return url
    srcset=el.get('srcset','')
    if srcset:
        url=urljoin(SOURCE,srcset.split(',')[0].strip().split(' ')[0])
        if url.startswith('https://'): return url
    return None

def identify(text):
    compact=' '.join(text.split())
    matches=re.findall(r'(?<![A-Z])('+CODE_PATTERN+r')(?![A-Z])',compact.upper())
    return matches[0] if matches else None

def parse_form(cell, team_code):
    # Los últimos 5 encuentros están en una lista dentro de la celda Club.
    # Evitar leer otros textos del encabezado para inferir resultados.
    entries=[x.get_text(' ',strip=True) for x in cell.find_all('li')]
    if not entries:
        entries=RESULT_REGEX.findall(cell.get_text(' ',strip=True))
        entries=['%s %s - %s %s'%m for m in entries]
    result=[]
    for entry in entries:
        m=RESULT_REGEX.search(entry)
        if not m: continue
        home,hs,away,as_=m.groups()
        if home!=team_code and away!=team_code: continue
        ours=int(hs if home==team_code else as_)
        theirs=int(as_ if home==team_code else hs)
        result.append('W' if ours>theirs else 'L' if ours<theirs else 'D')
        if len(result)==5: break
    return result

def find_logo(soup, cell, team_code):
    # Preferir el escudo que publica la fuente dentro de la fila.
    for el in cell.find_all('img'):
        u=img_url(el)
        if u: return u
    # Alternativa: buscar en el directorio ya incrustado en la página.
    for el in soup.find_all('img'):
        alt=(el.get('alt') or '').upper()
        if re.search(r'(?<![A-Z])'+team_code+r'(?![A-Z])',alt):
            u=img_url(el)
            if u:return u
    return None

def parse_standings(soup):
    candidates=[]
    for table in soup.find_all('table'):
        head=' '.join(t.get_text(' ',strip=True) for t in table.find_all('th')).upper()
        if 'PJ' in head and ('GF' in head or 'DIF' in head) and ('PTS' in head or 'PTS.' in head):
            candidates.append(table)
    for table in candidates:
        teams=[]
        for tr in table.find_all('tr'):
            tds=tr.find_all('td',recursive=False)
            if len(tds)<9:continue
            # Tabla original: posición, club, pts, pj, G, E, P, GF, GC, GD
            code=identify(tds[1].get_text(' ',strip=True))
            if not code:
                href=' '.join(str(a.get('href','')) for a in tds[1].find_all('a'))
                code=identify(href.replace('/',' '))
            if not code:
                imgs=tds[1].find_all('img')
                for el in imgs:
                    code=identify(el.get('alt',''))
                    if code:break
            if not code or code in [t['code'] for t in teams]:continue
            try:
                vals=[int(re.search(r'-?\d+',td.get_text(' ',strip=True).replace('−','-')).group()) for td in tds[2:9]]
                if len(vals)<7:continue
                pts,pj,g,e,p,gf,gc=vals
                if not (0<=pj<=35 and 0<=pts<=100 and 0<=g<=35 and 0<=e<=35 and 0<=p<=35):continue
                name,color=TEAMS[code]
                teams.append(dict(code=code,name=name,color=color,pj=pj,pts=pts,g=g,e=e,p=p,gf=gf,gc=gc,gd=gf-gc,
                    form=parse_form(tds[1],code),logo=find_logo(soup,tds[1],code) or LOGO_FALLBACK.get(code)))
            except (TypeError,ValueError,AttributeError):continue
        if len(teams)==10:
            return teams
    raise ValueError('No se pudo validar la tabla con los diez equipos; se conserva la última versión válida.')

def parse_matches(soup):
    # Enlaces a partidos del sitio, con equipos, marcadores y estado.
    # La forma del HTML puede cambiar: no inventar encuentros al no hallarlos.
    matches=[]
    seen=set()
    for a in soup.find_all('a',href=True):
        href=a.get('href','')
        if not any(k in href.lower() for k in ('match','partido','fixture','game','/calendario/')):continue
        raw=a.get_text(' ',strip=True)
        codes=re.findall(r'(?<![A-Z])('+CODE_PATTERN+r')(?![A-Z])',raw.upper())
        # Algunas tarjetas repiten el código dos veces por equipo.
        uniq=[]
        for c in codes:
            if not uniq or c!=uniq[-1]:uniq.append(c)
        if len(uniq)!=2 or uniq[0]==uniq[1]:continue
        home,away=uniq
        if (home,away) in seen:continue
        seen.add((home,away))
        status='programado'
        if re.search(r'finalizado|final\b|\bft\b',raw,re.I):status='finalizado'
        elif re.search(r'medio tiempo|descanso|\bht\b|\b\d{1,3}\s*[\'’]|\b\d{1,3}\s*min\b|en vivo',raw,re.I):status='en_vivo'
        # Sólo tomar resultados de una secuencia score–score, sin horas.
        scored=re.search(r'(\d{1,2})\s*[–-]\s*(\d{1,2})',raw)
        hs=int(scored.group(1)) if scored else None
        as_=int(scored.group(2)) if scored else None
        if status=='programado' and scored:
            # Si no podemos confirmar el estado, no usarlo para proyecciones.
            status='desconocido'
        matches.append(dict(home=home,away=away,hs=hs,as_=as_,status=status,description=raw[:145],url=urljoin(SOURCE,href),kickoff=None,round=13))
    return matches[:35]

MONTHS={'ene':1,'feb':2,'mar':3,'abr':4,'may':5,'jun':6,'jul':7,'ago':8,'sept':9,'sep':9,'oct':10,'nov':11,'dic':12}
def detail_match(m):
    """Obtiene fecha/hora de la página de partido, convertida a UTC-6. No inventar horarios."""
    try:
        html=get_html(m['url'])
        soup=BeautifulSoup(html,'html.parser')
        text=' '.join(soup.get_text(' ',strip=True).split())
        pattern=r'(?:Inicio(?:\s*[·:-]\s*Costa Rica)?|Inicio)\s*(\d{1,2})\s+(ene|feb|mar|abr|may|jun|jul|ago|sep|sept|oct|nov|dic)[a-z]*\s+(20\d{2}),?\s+(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?'
        found=re.search(pattern,text,re.I)
        if found:
            d,month,year,h,mi,mer=found.groups()
            hour=int(h)%12+(12 if mer.lower()=='p' else 0)
            dt=datetime(int(year),MONTHS[month.lower()],int(d),hour,int(mi),tzinfo=TZ)
            m['kickoff']=dt.isoformat(timespec='minutes')
        # Estado definitivo solo con evidencia explícita del detalle.
        if re.search(r'\bFinalizado\b',text[:1300],re.I):m['status']='finalizado'
        elif re.search(r'\b(No iniciado|Antes del partido)\b',text[:1300],re.I) and m['status']=='desconocido':m['status']='programado'
    except Exception:
        pass
    return m

def enrich_matches(matches):
    # Limitar conexiones: una vez por ciclo de caché, y paralelas para evitar bloqueos.
    if not matches:return matches
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures=[pool.submit(detail_match,m) for m in matches[:12]]
        for f in as_completed(futures):
            try:f.result()
            except Exception:pass
    return matches

def parse_page(html):
    soup=BeautifulSoup(html,'html.parser')
    teams=parse_standings(soup)
    matches=enrich_matches(parse_matches(soup))
    # Los partidos en juego no se incorporan automáticamente a la tabla de la fuente.
    # Calcular una segunda clasificación PROVISIONAL, sólo si hay marcador confiable.
    project=copy.deepcopy(teams)
    bycode={t['code']:t for t in project}
    applied=[]
    for m in matches:
        if m['status']!='en_vivo' or m['hs'] is None or m['as_'] is None:continue
        h=bycode[m['home']];a=bycode[m['away']]
        for t,goals,against in ((h,m['hs'],m['as_']),(a,m['as_'],m['hs'])):
            t['pj']+=1;t['gf']+=goals;t['gc']+=against;t['gd']=t['gf']-t['gc']
            if goals>against:t['g']+=1;t['pts']+=3
            elif goals==against:t['e']+=1;t['pts']+=1
            else:t['p']+=1
        applied.append(m)
    now=datetime.now(TZ).isoformat(timespec='seconds')
    return dict(teams=teams,provisional=project, matches=matches, live=bool(applied), projected=bool(applied),
                stale=False, round=13, source=SOURCE,source_time=now,fetched_at=now,
                status=('Clasificación provisional: se incluyen marcadores en juego.' if applied
                    else 'Clasificación basada en la última tabla publicada por la fuente.'))

cache=seed()
lock=threading.Lock()
last_attempt=0.0

def state(force=False):
    global cache,last_attempt
    with lock:
        now=time.monotonic()
        if now-last_attempt >= (FORCE_MIN_INTERVAL if force else TTL):
            last_attempt=now
            try:
                new=parse_page(get_html())
                # Si el escudo no viene en las posiciones, consultar el directorio de equipos.
                if any(not t['logo'] for t in new['teams']):
                    try:
                        logo_soup=BeautifulSoup(get_html('https://futstatscr.com/teams'),'html.parser')
                        for t in new['teams']:
                            if not t['logo']:
                                t['logo']=find_logo(logo_soup,logo_soup,t['code']) or LOGO_FALLBACK.get(t['code'])
                        for t in new['provisional']:
                            t['logo']=next((base['logo'] for base in new['teams'] if base['code']==t['code']),None)
                    except Exception:
                        pass
                cache=new
            except Exception as exc:
                cache=dict(cache)
                cache['stale']=True
                cache['status']='Fuente no disponible o cambió de formato. Conservamos los últimos datos válidos.'
                cache['error']=str(exc)[:200]
        return copy.deepcopy(cache)

# Noticias: medios originales, enlaces verificables, sin duplicar artículos.
# Usamos RSS cuando exista; si falla, portada pública del medio.
NEWS_SOURCES = [
    ('CRHoy','https://crhoy.com/feed/','https://crhoy.com/deportes/','crhoy.com'),
    ('Diario Extra','https://www.diarioextra.com/feed/','https://www.diarioextra.com/categoria/deportes/','diarioextra.com'),
    ('DeportesCR','https://deportescr.net/feed/','https://deportescr.net/','deportescr.net'),
    ('Grecia Deportes','https://greciadeportes.com/feed/','https://greciadeportes.com/','greciadeportes.com'),
    ('Teletica','https://www.teletica.com/rss/deportes.xml','https://www.teletica.com/deportes','teletica.com'),
    ('La Nación','https://www.nacion.com/arc/outboundfeeds/rss/category/puro-deporte/','https://www.nacion.com/puro-deporte/','nacion.com'),
    ('La Teja','https://www.lateja.cr/arc/outboundfeeds/rss/category/deportes/','https://www.lateja.cr/deportes/','lateja.cr'),
]
NEWS_SEARCH_QUERY = ('(Saprissa OR Alajuelense OR Herediano OR Cartaginés OR Sporting FC OR Puntarenas FC '
    'OR San Carlos OR Pérez Zeledón OR Escorpiones OR Inter San Carlos OR Liga Promerica) '
    '(site:crhoy.com OR site:nacion.com OR site:teletica.com OR site:deportescr.net '
    'OR site:diarioextra.com OR site:lateja.cr OR site:greciadeportes.com) when:7d')
NEWS_CACHE={'items':[], 'fetched':0, 'updated':None, 'sources':{}}
NEWS_LOCK=threading.Lock()

# Titulares de equipos y contexto futbolístico: no confundir una ciudad
# (Puntarenas, San Carlos, Pérez Zeledón) con el club local.
TEAM_NEWS=re.compile(r'\b(?:saprissa|alajuelense|herediano|cartagin[eé]s|sporting|puntarenas|'
    r'san carlos|p[eé]rez zeled[oó]n|escorpiones|inter san carlos|primera divisi[oó]n|'
    r'l[ií]ga promerica|liga deportiva|torneo apertura|torneo clausura|unafut)\b',re.I)
# Clubes cuya mención suele indicar fútbol: pedir además términos
# futbolísticos para descartar homónimos o noticias ajenas al deporte.
CLUB_EXPLICIT=re.compile(r'\b(?:saprissa|alajuelense|herediano|cartagin[eé]s|'
    r'sporting\s+(?:fc|f\.?c\.?)|inter\s+san\s+carlos|'
    r'escorpiones\s+(?:fc|f\.?c\.?)|'
    r'puntarenas\s+(?:fc|f\.?c\.?)|'
    r'(?:ad|a\.?d\.?)\s+san\s+carlos|'
    r'(?:municipal|ad|a\.?d\.?)\s+p[eé]rez\s+zeled[oó]n|'
    r'l[ií]ga\s+promerica|unafut|torneo\s+(?:apertura|clausura)|'
    r'primera\s+divisi[oó]n)\b',re.I)
AMBIGUOUS_TEAM=re.compile(r'\b(?:puntarenas|san\s+carlos|p[eé]rez\s+zeled[oó]n|'
    r'escorpiones|sporting)\b',re.I)
FOOTBALL_CONTEXT=re.compile(r'\b(?:f[uú]tbol|futbol[ií]st[ao]s?|futbolero|'
    r'partidos?|marcador(?:es)?|goles?|golazo|golpazo|goleada|'
    r'victoria|derrota|empate|remontada|cl[aá]sico|'
    r't[eé]cnico|entrenador|director\s+t[eé]cnico|dt|'
    r'jornada|torneo|campeonato|liga|primera\s+divisi[oó]n|'
    r'cancha|gramilla|estadios?|jugadores?|delantero|'
    r'portero|guardameta|defensa|mediocampo|'
    r'penal(?:ti)?|[áa]rbitro|expulsado|'
    r'tabla\s+de\s+posiciones|copa\s+centroamericana|'
    r'liguilla|semifinal|final|clasificaci[oó]n|'
    r'vs\.?|contra|derbi|ascenso|descenso)\b',re.I)
# Bloqueo para sucesos y noticias generales en titulares que contienen
# por casualidad el nombre de una ciudad con club de primera división.
GENERAL_NEWS=re.compile(r'\b(?:motociclista|balazos?|asesinad[oa]s?|'
    r'homicidios?|homicida|sicarios?|femicidios?|polic[ií]a|'
    r'crimen|cr[ií]menes|delincuen(?:te|cia)|detenid[oa]s?|'
    r'ataque\s+armado|atacad[oa]\s+a\s+balazos|'
    r'accidente\s+de\s+tr[aá]nsito|colisi[oó]n|'
    r'decomis[oa]|allanamiento|narcotr[aá]fico|'
    r'diputad[oa]s?|asamblea\s+legislativa|'
    r'terremoto|temblor|inundaci[oó]n)\b',re.I)

def is_football_news(title, url=''):
    """Titulares de Primera División; evita confundir ciudades con clubes.

    Exige mención futbolística o de un club inequívoco y bloquea sucesos
    ajenos al deporte. No infiere relevancia a partir del dominio del medio.
    """
    title=' '.join(str(title or '').split())
    if len(title)<16 or GENERAL_NEWS.search(title):
        return False
    if not TEAM_NEWS.search(title):
        return False
    if CLUB_EXPLICIT.search(title):
        return True
    return bool(FOOTBALL_CONTEXT.search(title))

ALLOWED_DOMAINS=tuple(x[3] for x in NEWS_SOURCES)

def trusted_url(url, expected=None):
    try:
        v=urlparse((url or '').strip());d=(v.hostname or '').lower()
        domains=(expected,) if expected else ALLOWED_DOMAINS
        return v.scheme=='https' and v.username is None and v.password is None and any(d==x or d.endswith('.'+x) for x in domains)
    except Exception:return False

def safe_picture(url, article_url):
    if not url:return None
    url=urljoin(article_url,str(url).strip());u=urlparse(url)
    if u.scheme!='https' or not u.hostname or u.username or u.password:return None
    # only remote images, not local/private addresses
    if u.hostname.lower() in ('localhost','127.0.0.1','0.0.0.0') or u.hostname.endswith('.local'):return None
    return url

def local_date(value):
    try:return parsedate_to_datetime(value).astimezone(TZ).strftime('%d/%m/%Y')
    except Exception:
        try:return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(TZ).strftime('%d/%m/%Y')
        except Exception:return ''

def get_news_html(url):
    req=Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; UNAFUTLive/1.1; noticias)',
             'Accept':'application/rss+xml,application/xml,text/html;q=0.8','Accept-Language':'es-CR,es;q=0.9'})
    with urlopen(req,timeout=5) as resp:
        if resp.status!=200:raise ValueError('HTTP '+str(resp.status))
        return resp.read(1_500_000).decode('utf-8','replace')

def parse_rss(name,url,domain):
    root=ET.fromstring(get_news_html(url));out=[]
    entries=list(root.findall('./channel/item'))+list(root.findall('{http://www.w3.org/2005/Atom}entry'))
    for e in entries:
        title=(e.findtext('title') or e.findtext('{http://www.w3.org/2005/Atom}title') or '').strip()
        link=(e.findtext('link') or '').strip()
        if not link:
            tag=e.find('{http://www.w3.org/2005/Atom}link')
            if tag is not None:link=tag.attrib.get('href','')
        if not title or not trusted_url(link,domain) or not is_football_news(title,link):continue
        stamp=e.findtext('pubDate') or e.findtext('{http://www.w3.org/2005/Atom}updated') or ''
        pic=None
        for tag in e.iter():
            kind=tag.tag.rsplit('}',1)[-1].lower()
            if kind in ('thumbnail','content') and 'url' in tag.attrib and ('media' in tag.tag.lower() or kind=='thumbnail'):
                pic=safe_picture(tag.attrib['url'],link)
            elif kind=='enclosure' and tag.attrib.get('type','').startswith('image/'):
                pic=safe_picture(tag.attrib.get('url'),link)
            if pic:break
        out.append(dict(title=title,source=name,url=link,published=local_date(stamp),raw_date=stamp,image=pic))
        if len(out)>=10:break
    return out

def parse_listing(name,url,domain):
    """Read public sports index if feed is unavailable; never invent articles."""
    doc=BeautifulSoup(get_news_html(url),'html.parser');out=[];seen=set()
    for a in doc.select('main a[href], article a[href], a[href]'):
        link=urljoin(url,a.get('href',''))
        if not trusted_url(link,domain) or link in seen or link.rstrip('/')==url.rstrip('/'):continue
        u=urlparse(link);path=u.path.strip('/')
        if len(path)<15 or path.count('/')<1 or any(x in path.lower() for x in ('/categoria/','/tag/','/author/','/autor/','/feed/')):continue
        head=a.find(['h2','h3','h4'])
        title=(head.get_text(' ',strip=True) if head else a.get('aria-label') or a.get_text(' ',strip=True))
        title=re.sub(r'\s+',' ',title).strip()
        if not (20<=len(title)<=230) or not is_football_news(title,link):continue
        seen.add(link)
        img=a.find('img')
        if img is None:
            parent=a.parent
            if parent and parent.name in ('article','div','li'):img=parent.find('img')
        pic=None
        if img:
            src=img.get('data-src') or img.get('data-lazy-src') or img.get('src')
            if not src and img.get('srcset'):src=img['srcset'].split(',')[0].strip().split(' ')[0]
            pic=safe_picture(src,link)
        out.append(dict(title=title,source=name,url=link,published='',raw_date='',image=pic))
        if len(out)>=10:break
    return out

def source_news(src):
    name,rss,listing,domain=src
    try:
        items=parse_rss(name,rss,domain)
        if items:return name,items,'RSS'
    except Exception:pass
    try:
        items=parse_listing(name,listing,domain)
        return name,items,'Portada' if items else 'Sin artículos relacionados'
    except Exception:return name,[],'No accesible'

def article_image(item):
    if item.get('image') or not trusted_url(item['url']):return item
    try:
        soup=BeautifulSoup(get_news_html(item['url']),'html.parser')
        for attr,val in [('property','og:image:secure_url'),('property','og:image'),('name','twitter:image')]:
            tag=soup.find('meta',attrs={attr:val})
            if tag and tag.get('content'):
                item['image']=safe_picture(tag['content'],item['url'])
                if item['image']:break
        if not item['published']:
            tag=soup.find('meta',attrs={'property':'article:published_time'})
            if tag and tag.get('content'):item['published']=local_date(tag['content'])
    except Exception:pass
    return item

def google_headlines():
    """Last resort only: Google News links have no original image by design."""
    url='https://news.google.com/rss/search?q='+quote(NEWS_SEARCH_QUERY)+'&hl=es-419&gl=CR&ceid=CR:es-419'
    root=ET.fromstring(get_news_html(url));out=[]
    for e in root.findall('./channel/item'):
        title=(e.findtext('title') or '').strip();link=(e.findtext('link') or '').strip();src=e.find('source')
        publisher=(src.text or '').strip() if src is not None else 'Noticias'
        if not title or not link.startswith('https://news.google.com/') or not is_football_news(title,link):continue
        if not any(x in publisher.lower() for x in ('crhoy','nación','nacion','teletica','extra','deportescr','grecia','la teja')):continue
        out.append(dict(title=title,source=publisher,url=link,published=local_date(e.findtext('pubDate') or ''),image=None,raw_date=''))
        if len(out)>=8:break
    return out

def headlines():
    with NEWS_LOCK:
        if time.monotonic()-NEWS_CACHE['fetched']<1200:
            return [item for item in NEWS_CACHE['items'] if is_football_news(item.get('title',''),item.get('url',''))][:6]
        NEWS_CACHE['fetched']=time.monotonic();previous=NEWS_CACHE['items'];found=[];diag={}
        with ThreadPoolExecutor(max_workers=7) as pool:
            jobs=[pool.submit(source_news,s) for s in NEWS_SOURCES]
            for job in as_completed(jobs):
                try:
                    name,items,status=job.result();found.extend(items)
                    diag[name]={'estado':status,'articulos':len(items)}
                except Exception:pass
        # favor original media articles; enrich images from og:image
        unique={}
        for item in found:
            if is_football_news(item.get('title',''),item.get('url','')):
                unique.setdefault(item['url'].split('?')[0],item)
        items=list(unique.values())[:21]
        with ThreadPoolExecutor(max_workers=7) as pool:
            enriched=list(pool.map(article_image,items))
        # Source-first to maximize thumbnails; fill only if too few articles
        if len(enriched)<6:
            try:
                for item in google_headlines():
                    if item['url'] not in unique and is_football_news(item.get('title',''),item.get('url','')):enriched.append(item)
            except Exception:pass
        result=[item for item in enriched if is_football_news(item.get('title',''),item.get('url',''))][:6]
        if result:NEWS_CACHE['items']=result
        else:NEWS_CACHE['items']=[item for item in previous if is_football_news(item.get('title',''),item.get('url',''))][:6]
        NEWS_CACHE['sources']=diag
        NEWS_CACHE['updated']=datetime.now(TZ).isoformat()
        return NEWS_CACHE['items']

class Handler(BaseHTTPRequestHandler):
    def respond(self,data,ctype='application/json; charset=utf-8',status=200):
        self.send_response(status)
        self.send_header('Content-Type',ctype)
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path == '/health':
            self.respond(b'{"status":"ok"}')
        elif self.path.startswith('/api/tg-results'):
            try:
                dataset=json.loads((ROOT/'tg_resultados.json').read_text(encoding='utf-8'))
                if not isinstance(dataset,dict) or not isinstance(dataset.get('sorteos'),list):
                    raise ValueError('Datos de TG invalidos')
                self.respond(json.dumps({'fecha':dataset.get('fecha'),'sorteos':dataset['sorteos']},ensure_ascii=False).encode('utf-8'))
            except (OSError,ValueError,TypeError):
                self.respond(b'{"fecha":null,"sorteos":[]}', 'application/json; charset=utf-8')
        elif self.path in ('/tg-panel-base.webp','/tg-logo-original.webp'):
            asset=ROOT/self.path.lstrip('/')
            if asset.is_file():
                self.respond(asset.read_bytes(),'image/webp')
            else:
                self.respond(b'Archivo TG no encontrado','text/plain',404)
        elif self.path == '/api/news':
            self.respond(json.dumps({'items':headlines(), 'updated':NEWS_CACHE['updated']},ensure_ascii=False).encode('utf8'))
        elif self.path.startswith('/api/news-status'):
            headlines()
            self.respond(json.dumps({'updated':NEWS_CACHE['updated'], 'sources':NEWS_CACHE['sources'], 'total':len(NEWS_CACHE['items']), 'with_images':sum(bool(x.get('image')) for x in NEWS_CACHE['items'])},ensure_ascii=False).encode('utf8'))
        elif self.path.startswith('/api/state'):
            data=state(force='force=1' in self.path)
            self.respond(json.dumps(data,ensure_ascii=False).encode('utf8'))
        elif self.path in ('/ads/tiempos-golfito.png', '/ads/cabinas-golfito.png', '/ads/tiempos-golfito-mobile.png', '/ads/cabinas-golfito-mobile.png'):
            image_path=ROOT/self.path.lstrip('/')
            if image_path.is_file():
                self.respond(image_path.read_bytes(),'image/png')
            else:
                self.respond(b'Imagen no encontrada','text/plain',404)
        elif re.fullmatch(r'/logos/(DS|CSC|SSJ|LDA|SC|PFC|ISC|CSH|EB|PZ)\.png',self.path):
            image_path=ROOT/self.path.lstrip('/')
            self.respond(image_path.read_bytes(),'image/png')
        elif self.path in ('/favicon.ico','/favicon.svg','/favicon-32x32.png','/favicon-16x16.png','/apple-touch-icon.png','/android-chrome-192x192.png','/android-chrome-512x512.png','/site.webmanifest'):
            static_map={
                '/favicon.ico':('favicon.ico','image/x-icon'),
                '/favicon.svg':('favicon.svg','image/svg+xml'),
                '/favicon-32x32.png':('favicon-32x32.png','image/png'),
                '/favicon-16x16.png':('favicon-16x16.png','image/png'),
                '/apple-touch-icon.png':('apple-touch-icon.png','image/png'),
                '/android-chrome-192x192.png':('android-chrome-192x192.png','image/png'),
                '/android-chrome-512x512.png':('android-chrome-512x512.png','image/png'),
                '/site.webmanifest':('site.webmanifest','application/manifest+json'),
            }
            filename, mime = static_map[self.path]
            file_path = ROOT/filename
            if file_path.is_file():
                self.respond(file_path.read_bytes(), mime)
            else:
                self.respond(b'Archivo no encontrado','text/plain',404)
        elif self.path=='/' or self.path.startswith('/index.html'):
            self.respond((ROOT/'index.html').read_bytes(),'text/html; charset=utf-8')
        else:self.respond(b'No encontrado','text/plain',404)
    def log_message(self,fmt,*args):pass

if __name__=='__main__':
    try:
        server=ThreadingHTTPServer((HOST,PORT),Handler)
    except OSError as err:
        print('No se pudo iniciar el servidor en el puerto',PORT,':',err)
        print('Cierre cualquier instancia previa del panel y pruebe de nuevo.')
        raise SystemExit(1)
    print(f'\n UNAFUT Live funcionando en {HOST}:{PORT}', flush=True)
    print(' Actualiza desde FutStatsCR cada 5 minutos con el navegador abierto.')
    print(' Mantenga esta ventana abierta. Ctrl + C para detener.\n', flush=True)
    # Abrir el navegador SOLO cuando el servidor ya esta listo.
    if OPEN_BROWSER:
        threading.Timer(1.0, lambda: webbrowser.open(f'http://127.0.0.1:{PORT}')).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nServidor detenido.')
    finally:
        server.server_close()
