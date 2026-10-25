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

# Fuentes editoriales externas. Solo titulares, metadatos y enlaces; sin copiar artículos.
# Algunos sitios pueden bloquear RSS, imágenes o lecturas automáticas: se omiten sin inventar.
NEWS_SOURCES = [
    ('CRHoy', 'https://crhoy.com/feed/', 'crhoy.com'),
    ('Diario Extra', 'https://www.diarioextra.com/feed/', 'diarioextra.com'),
    ('DeportesCR', 'https://deportescr.net/feed/', 'deportescr.net'),
    ('Grecia Deportes', 'https://greciadeportes.com/feed/', 'greciadeportes.com'),
    ('Teletica', 'https://www.teletica.com/rss/deportes.xml', 'teletica.com'),
    ('La Nación', 'https://www.nacion.com/arc/outboundfeeds/rss/category/puro-deporte/', 'nacion.com'),
    ('La Teja', 'https://www.lateja.cr/arc/outboundfeeds/rss/category/deportes/', 'lateja.cr'),
]
# Enlaces de búsqueda secundaria: se usan para titulares, no se inventan fotos ni URLs originales.
NEWS_SEARCH_QUERY = ('(Saprissa OR Alajuelense OR Herediano OR Cartaginés OR Sporting FC '
    'OR Puntarenas FC OR San Carlos OR Pérez Zeledón OR Escorpiones OR Inter San Carlos '
    'OR Liga Promerica) (site:crhoy.com OR site:nacion.com OR site:teletica.com '
    'OR site:deportescr.net OR site:diarioextra.com OR site:lateja.cr '
    'OR site:greciadeportes.com OR site:espn.co.cr) when:7d')
NEWS_CACHE={'items':[], 'fetched':0, 'updated':None}
NEWS_LOCK=threading.Lock()
TEAM_NEWS=re.compile(r'\b(?:saprissa|alajuelense|herediano|cartagin[eé]s|sporting|puntarenas|'
    r'san carlos|p[eé]rez zeled[oó]n|escorpiones|inter san carlos|primera divisi[oó]n|'
    r'l[ií]ga promerica|liga deportiva|torneo apertura|torneo clausura|unafut)\b',re.I)
ALLOWED_DOMAINS=tuple(src[2] for src in NEWS_SOURCES)+('espn.co.cr','espn.com')

def trusted_url(url):
    try:
        parsed=urlparse((url or '').strip())
        domain=(parsed.hostname or '').lower()
        return parsed.scheme=='https' and any(domain==d or domain.endswith('.'+d) for d in ALLOWED_DOMAINS)
    except Exception: return False

def safe_picture(url, article_url):
    if not url:return None
    url=urljoin(article_url,url.strip())
    parts=urlparse(url)
    if parts.scheme!='https' or not parts.hostname or parts.username or parts.password:return None
    if parts.hostname.lower() in ('localhost','127.0.0.1','0.0.0.0'):return None
    return url

def local_date(value):
    try:
        return parsedate_to_datetime(value).astimezone(TZ).strftime('%d/%m/%Y')
    except Exception:
        try:return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(TZ).strftime('%d/%m/%Y')
        except Exception:return ''

def parse_rss(name, url, domain):
    """Retrieve allowed RSS/Atom XML; extract official media thumbnails when published."""
    data=get_news_html(url)
    root=ET.fromstring(data)
    entries=[]
    for el in list(root.findall('./channel/item'))+list(root.findall('{http://www.w3.org/2005/Atom}entry')):
        title=(el.findtext('title') or el.findtext('{http://www.w3.org/2005/Atom}title') or '').strip()
        link=(el.findtext('link') or '').strip()
        if not link:
            linktag=el.find('{http://www.w3.org/2005/Atom}link')
            if linktag is not None:link=linktag.attrib.get('href','')
        if not title or not trusted_url(link) or not TEAM_NEWS.search(title):continue
        published=el.findtext('pubDate') or el.findtext('{http://www.w3.org/2005/Atom}updated') or ''
        image=None
        for tag in el.iter():
            typ=tag.tag.rsplit('}',1)[-1].lower()
            if typ in ('thumbnail','content') and 'url' in tag.attrib and ('media' in tag.tag.lower() or typ=='thumbnail'):
                image=safe_picture(tag.attrib['url'],link)
            elif typ=='enclosure' and tag.attrib.get('type','').startswith('image/'):
                image=safe_picture(tag.attrib.get('url'),link)
            if image:break
        entries.append({'title':title,'source':name,'url':link,'published':local_date(published),
                        'raw_date':published,'image':image})
        if len(entries)>=12:break
    return entries

def get_news_html(url):
    req=Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; UnafutLiveNews/1.0)',
             'Accept':'application/rss+xml,application/xml,text/html;q=0.8','Accept-Language':'es-CR,es;q=0.9'})
    with urlopen(req,timeout=7) as resp:
        if resp.status!=200:raise ValueError('HTTP '+str(resp.status))
        return resp.read(2_000_000).decode('utf-8','replace')

def article_image(news):
    if news['image'] or not trusted_url(news['url']):return news
    try:
        soup=BeautifulSoup(get_news_html(news['url']),'html.parser')
        for attr,val in [('property','og:image:secure_url'),('property','og:image'),('name','twitter:image')]:
            tag=soup.find('meta',attrs={attr:val})
            if tag and tag.get('content'):
                news['image']=safe_picture(tag['content'],news['url'])
                if news['image']:break
    except Exception:pass
    return news

def google_headlines():
    url='https://news.google.com/rss/search?q='+quote(NEWS_SEARCH_QUERY)+'&hl=es-419&gl=CR&ceid=CR:es-419'
    root=ET.fromstring(get_news_html(url))
    out=[]
    for el in root.findall('./channel/item'):
        title=(el.findtext('title') or '').strip()
        link=(el.findtext('link') or '').strip()
        src=el.find('source')
        publisher=(src.text or '').strip() if src is not None else 'Noticias'
        if not title or not link.startswith('https://') or not TEAM_NEWS.search(title):continue
        if not any(x in publisher.lower() for x in ('crhoy','nación','nacion','teletica','extra','deportescr','grecia','la teja','espn')):continue
        out.append(dict(title=title,source=publisher,url=link,published=local_date(el.findtext('pubDate') or ''),image=None))
        if len(out)>=10:break
    return out

def headlines():
    """Cache 20 min; use original thumbnails only where sources publish and permit retrieval."""
    with NEWS_LOCK:
        if time.monotonic()-NEWS_CACHE['fetched']<1200:return NEWS_CACHE['items']
        NEWS_CACHE['fetched']=time.monotonic()
        previous=NEWS_CACHE['items']
        found=[]
        with ThreadPoolExecutor(max_workers=7) as pool:
            jobs=[pool.submit(parse_rss,*entry) for entry in NEWS_SOURCES]
            for job in as_completed(jobs):
                try:found.extend(job.result())
                except Exception:pass
        # Metadata fetch only for original trusted URLs, never third-party Google redirects.
        candidates=sorted(found,key=lambda n:n.get('raw_date',''),reverse=True)[:18]
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs=[pool.submit(article_image,item) for item in candidates]
            enriched=[]
            for job in as_completed(jobs):
                try:enriched.append(job.result())
                except Exception:pass
        # Keep each publication once, omit duplicate story URLs.
        unique={}
        for item in enriched:
            unique[item['url'].split('?')[0]]=item
        result=list(unique.values())
        if len(result)<6:
            try:
                for item in google_headlines():
                    unique.setdefault(item['url'].split('?')[0],item)
                result=list(unique.values())
            except Exception:pass
        result=result[:12]
        if result:NEWS_CACHE['items']=result
        else:NEWS_CACHE['items']=previous
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
        elif self.path.startswith('/api/news'):
            self.respond(json.dumps({'items':headlines()},ensure_ascii=False).encode('utf8'))
        elif self.path.startswith('/api/state'):
            data=state(force='force=1' in self.path)
            self.respond(json.dumps(data,ensure_ascii=False).encode('utf8'))
        elif re.fullmatch(r'/logos/(DS|CSC|SSJ|LDA|SC|PFC|ISC|CSH|EB|PZ)\.png',self.path):
            image_path=ROOT/self.path.lstrip('/')
            self.respond(image_path.read_bytes(),'image/png')
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
