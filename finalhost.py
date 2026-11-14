from __future__ import annotations
import asyncio, contextlib, html, json, logging, os, re, shutil, signal, sqlite3, sys, time, uuid, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "8717606762:AAEcpgnvQTCNHAV3pQRdTmUYKvOKSH717Js")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6858000955"))
REQUIRED_CHANNEL = "@TeleNexusIo"; REQUIRED_CHANNEL_URL = "https://t.me/TeleNexusIo"
WAVEPAY = ("Phyoe Zaw Htet", "09408026025"); KBZPAY = ("Wai Phyoe Aung", "09407562154")
DATA = Path(os.getenv("DATA_DIR", "/app/data")).resolve(); PROJECTS=DATA/"projects"; LOGS=DATA/"logs"; VENVS=DATA/"venvs"; BACKUPS=DATA/"backups"; DB=DATA/"runner.sqlite3"
for p in (DATA, PROJECTS, LOGS, VENVS, BACKUPS): p.mkdir(parents=True, exist_ok=True)
FREE_PROJECTS, PREMIUM_PROJECTS = 1, 5; FREE_QUOTA, PREMIUM_QUOTA=500*1024**2,5*1024**3; FREE_UPLOAD, PREMIUM_UPLOAD=100*1024**2,500*1024**2
MAX_RESTARTS, DEP_TIMEOUT, PROCESS_TIMEOUT = 5, 600, 24*60*60; SCHEMA_VERSION=6
SECRET_KEYS=re.compile(r"(?i)(token|password|secret|api[_-]?key|authorization|private[_-]?key)\s*[=:]\s*[^\s,;]+")
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s %(levelname)s %(name)s %(message)s"); log=logging.getLogger("vps")
for _logger_name in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)
processes:dict[str,asyncio.subprocess.Process]={}; tasks:dict[str,asyncio.Task]={}; states:dict[int,dict[str,Any]]={}; broadcast_jobs:dict[int,asyncio.Task]={}

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def esc(x): return html.escape(str(x),quote=False)
def mask(x): return SECRET_KEYS.sub(lambda m:m.group(1)+"=<masked>",str(x))
def conn(): c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; return c
def audit(uid,action,detail=""):
    with conn() as c:c.execute("INSERT INTO audit(user_id,action,detail,created_at) VALUES(?,?,?,?)",(uid,action,mask(detail)[:1000],now()))
def init():
    with conn() as c:
        c.executescript('''PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,name TEXT,username TEXT,approved INTEGER DEFAULT 0,blocked INTEGER DEFAULT 0,plan TEXT DEFAULT 'free',premium_until TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,user_id INTEGER,name TEXT,path TEXT,runtime TEXT,entrypoint TEXT,status TEXT DEFAULT 'stopped',auto_restart INTEGER DEFAULT 0,created_at TEXT,updated_at TEXT,UNIQUE(user_id,name));
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,project_id TEXT,user_id INTEGER,status TEXT,pid INTEGER,exit_code INTEGER,log_path TEXT,started_at TEXT,finished_at TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY,user_id INTEGER,months INTEGER,amount INTEGER,status TEXT DEFAULT 'pending',receipt_file_id TEXT,reason TEXT,created_at TEXT,reviewed_at TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS env(id INTEGER PRIMARY KEY,project_id TEXT,key TEXT,value TEXT,UNIQUE(project_id,key));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,user_id INTEGER,action TEXT,detail TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY,user_id INTEGER,title TEXT,body TEXT,read INTEGER DEFAULT 0,created_at TEXT);
        CREATE TABLE IF NOT EXISTS backups(id TEXT PRIMARY KEY,path TEXT,created_at TEXT,size INTEGER);''')
        old=c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone(); c.execute("INSERT INTO meta(key,value) VALUES('schema_version,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(SCHEMA_VERSION),)) if False else c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",('schema_version',str(SCHEMA_VERSION)))
        c.execute("INSERT OR IGNORE INTO settings VALUES('join_enabled','1')"); c.execute("INSERT OR IGNORE INTO settings VALUES('maintenance','0')")
def setting(k,d=''):
    with conn() as c:r=c.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone(); return r['value'] if r else d
def set_setting(k,v):
    with conn() as c:c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,str(v)))
def user(uid,up=None):
    with conn() as c:
        r=c.execute('SELECT * FROM users WHERE user_id=?',(uid,)).fetchone(); u=up.effective_user if up else None
        if not r:c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?)',(uid,u.full_name if u else '',u.username or '' if u else '',0,0,'free',None,now()))
        elif u:c.execute('UPDATE users SET name=?,username=? WHERE user_id=?',(u.full_name,u.username or '',uid))
        return c.execute('SELECT * FROM users WHERE user_id=?',(uid,)).fetchone()
def admin(uid): return uid==ADMIN_ID
def premium(r):
    if admin(r['user_id']): return True
    try:return r['plan']=='premium' and r['premium_until'] and datetime.fromisoformat(r['premium_until'])>datetime.now(timezone.utc)
    except:return False
def limits(r):return (PREMIUM_PROJECTS,PREMIUM_QUOTA,PREMIUM_UPLOAD,3) if premium(r) else (FREE_PROJECTS,FREE_QUOTA,FREE_UPLOAD,1)
def safe(x):return re.sub(r'[^A-Za-z0-9_.-]','_',x.strip())[:48].strip('._-') or 'project'
def size(p):
    try:return sum(x.stat().st_size for x in p.rglob('*') if x.is_file() and '.venv' not in x.parts)
    except:return 0
def used(uid):
    with conn() as c:rows=c.execute('SELECT path FROM projects WHERE user_id=?',(uid,)).fetchall()
    return sum(size(Path(r['path'])) for r in rows)
def count(uid):
    with conn() as c:return c.execute('SELECT COUNT(*) n FROM projects WHERE user_id=?',(uid,)).fetchone()['n']
def btn(t,d):return B(t,callback_data=d)
def back(d='menu'):return [[btn('⬅️ Back',d),btn('🏠 Main Menu','menu')]]
def main_kb(a=False):
    x=[[btn('📂 Projects','projects'),btn('📤 Upload Project','upload')],[btn('📊 System Status','system'),btn('💾 Storage','storage')],[btn('⚙️ Settings','settings'),btn('💎 Buy Premium','premium')],[btn('🔔 Notifications','notifs'),btn('❓ Help','help')]]
    if a:x.append([btn('🛡️ Admin Panel','admin')])
    return K(x)
def admin_kb():return K([[btn('👥 Users','users:0'),btn('💳 Pending Payments','payments:0')],[btn('📢 Broadcast','broadcast'),btn('📈 Audit Logs','audit:0')],[btn('💾 Backups','backups'),btn('🩺 Health','health')],[btn('⚙️ Maintenance','maint'),btn('🧹 Cleanup','cleanup')],*back()])
def join_kb():return K([[btn('📢 Join Channel','join')],[btn('✅ I Joined','verify')]])
async def retry(fn,tries=4):
    for i in range(tries):
        try:return await fn()
        except RetryAfter as e:await asyncio.sleep(min(e.retry_after,60))
        except (TelegramError,asyncio.TimeoutError):
            if i==tries-1:raise
            await asyncio.sleep(2**i)
async def joined(context,uid):
    if admin(uid) or setting('join_enabled','1')!='1':return True
    try:return (await context.bot.get_chat_member(REQUIRED_CHANNEL,uid)).status in ('member','administrator','creator')
    except Exception:return False
async def menu(update,u):
    mp,quota,_,_=limits(u); exp=f"\n⏳ Expires: {u['premium_until'][:10]}" if premium(u) and u['premium_until'] else ''
    t=f"🤖 <b>VPS Hosting Panel</b>\n\n👤 <code>{u['user_id']}</code>\n💎 Plan: <b>{'Premium' if premium(u) else 'Free'}</b>{exp}\n📦 Projects: {count(u['user_id'])}/{mp}\n💾 Storage: {used(u['user_id'])/1024/1024:.1f}/{quota/1024/1024:.0f} MB"
    if update.callback_query:await update.callback_query.edit_message_text(t,parse_mode=ParseMode.HTML,reply_markup=main_kb(admin(u['user_id'])))
    else:await update.message.reply_text(t,parse_mode=ParseMode.HTML,reply_markup=main_kb(admin(u['user_id'])))
async def start(update,context):
    u=user(update.effective_user.id,update)
    if u['blocked']:return await update.message.reply_text('🚫 Account blocked.')
    if setting('maintenance','0')=='1' and not admin(u['user_id']):return await update.message.reply_text('🛠️ Maintenance mode is active.')
    if not await joined(context,u['user_id']):return await update.message.reply_text('🔒 Join the required channel first.',reply_markup=join_kb())
    if not admin(u['user_id']) and not u['approved']:return await update.message.reply_text('🛡️ Access pending.',reply_markup=K([[btn('📨 Request Access','request')]]))
    await menu(update,u)
def scan(root):
    fs=[p for p in root.rglob('*') if p.is_file() and '.venv' not in p.parts and '__pycache__' not in p.parts]; runtime=entry=''; cfg=root/'runner.json'
    if cfg.exists():
        try:x=json.loads(cfg.read_text());runtime,entry=x.get('runtime',''),x.get('entrypoint','')
        except Exception:pass
    for n in ('main.py','bot.py','app.py','run.py','start.py','index.py'):
        q=next((p for p in fs if p.name==n),None)
        if q and not entry:runtime,entry='python',str(q.relative_to(root));break
    for n in ('index.js','main.js','app.js','bot.js','server.js'):
        q=next((p for p in fs if p.name==n),None)
        if q and not entry:runtime,entry='node',str(q.relative_to(root));break
    if not entry:
        q=next((p for p in fs if p.suffix=='.py'),None)
        if q:runtime,entry='python',str(q.relative_to(root))
    if not entry:
        q=next((p for p in fs if p.suffix in ('.js','.mjs','.cjs')),None)
        if q:runtime,entry='node',str(q.relative_to(root))
    if not entry:
        q=next((p for p in fs if p.suffix=='.sh' and os.access(p,os.X_OK)),None)
        if q:runtime,entry='shell',str(q.relative_to(root))
    docker=any(p.name.lower() in ('dockerfile','docker-compose.yml','compose.yml','docker-compose.yaml') for p in fs)
    return {'runtime':runtime,'entry':entry,'files':len(fs),'docker':docker,'pyproject':(root/'pyproject.toml').exists(),'lock':(root/'package-lock.json').exists()}
def extract(src,dst):
    with zipfile.ZipFile(src) as z:
        if len(z.infolist())>10000 or sum(max(0,i.file_size) for i in z.infolist())>600*1024**2:raise ValueError('Archive too large')
        root=dst.resolve()
        for i in z.infolist():
            if i.file_size>200*1024**2:raise ValueError('Archive member too large')
            if i.filename.startswith('/') or '\\' in i.filename:raise ValueError('Unsafe ZIP path')
            mode=(i.external_attr >> 16) & 0o170000
            if mode==0o120000:raise ValueError('Symlinks are not allowed')
            d=(dst/i.filename).resolve()
            if not str(d).startswith(str(root)+os.sep):raise ValueError('Unsafe ZIP path')
        z.extractall(dst)
async def upload_prompt(update,context):states[update.effective_user.id]={'step':'file'};await update.callback_query.edit_message_text('📤 Send a ZIP or source file. /cancel to stop.',reply_markup=K(back()))
async def document(update,context):
    uid=update.effective_user.id;u=user(uid,update)
    if u['blocked']:return await update.message.reply_text('🚫 Account blocked.')
    if setting('maintenance','0')=='1' and not admin(uid):return await update.message.reply_text('🛠️ Maintenance mode is active.')
    if not admin(uid) and not u['approved']:return await update.message.reply_text('🛡️ Access approval is required. Use /start first.')
    if not await joined(context,uid):return await update.message.reply_text('🔒 Join the required channel first.',reply_markup=join_kb())
    _,_,lim,_=limits(u);d=update.message.document;n=Path(d.file_name or 'upload.zip').name
    if d.file_size and d.file_size>lim:return await update.message.reply_text(f'❌ Upload limit: {lim//1024//1024} MB')
    if not n.lower().endswith(('.zip','.py','.js','.mjs','.cjs','.sh','.json','.toml','.txt')):return await update.message.reply_text('❌ Unsupported file extension.')
    tmp=DATA/f'tmp-{uuid.uuid4().hex}';tmp.mkdir();src=tmp/n;await (await d.get_file()).download_to_drive(str(src));states[uid]={'step':'name','tmp':str(tmp),'src':str(src),'name':n};await update.message.reply_text('✅ File received. Send a unique project name.')
async def create(update,name):
    uid=update.effective_user.id;s=states.pop(uid,None)
    if not s:return await update.message.reply_text('Start with Upload Project.')
    u=user(uid,update);mx,quota,_,_=limits(u); root=None
    try:
        if count(uid)>=mx:raise ValueError(f'Project limit: {mx}')
        with conn() as c:
            if c.execute('SELECT 1 FROM projects WHERE user_id=? AND name=?',(uid,safe(name))).fetchone():raise ValueError('Duplicate project name')
        pid=uuid.uuid4().hex[:12];root=PROJECTS/f'{uid}-{pid}';root.mkdir();src=Path(s['src'])
        extract(src,root) if s['name'].lower().endswith('.zip') else shutil.copy2(src,root/src.name)
        if size(root)>quota:raise ValueError('Project exceeds storage quota')
        x=scan(root)
        if x['runtime'] not in ('python','node','shell'):raise ValueError('No supported Python/Node/Shell entrypoint found')
        with conn() as c:c.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?,?)',(pid,uid,safe(name),str(root),x['runtime'],x['entry'],'stopped',0,now(),now()))
        audit(uid,'project_create',pid);warn=' ⚠️ Docker/Compose detected (report only; never executed).' if x['docker'] else ''
        await update.message.reply_text(f"✅ <b>Project created</b>\n\n📦 {esc(safe(name))}\n🧩 {x['runtime']}\n📄 <code>{esc(x['entry'])}</code>\n📁 Files: {x['files']}{warn}",parse_mode=ParseMode.HTML,reply_markup=K([[btn('▶️ Run',f'run:{pid}')],*back('projects')]))
    except Exception as e:
        if root:shutil.rmtree(root,ignore_errors=True)
        await update.message.reply_text(f'❌ {esc(e)}')
    finally:shutil.rmtree(s['tmp'],ignore_errors=True)
async def create_payment_photo(update,context):
    uid=update.effective_user.id;s=states.get(uid,{})
    if s.get('step')!='receipt':return
    m=s.get('months');
    if m not in (1,2,3):return await update.message.reply_text('❌ Invalid Premium duration.')
    fid=update.message.photo[-1].file_id
    with conn() as c:
        if c.execute("SELECT id FROM payments WHERE user_id=? AND status='pending'",(uid,)).fetchone():return await update.message.reply_text('⚠️ You already have a pending receipt. Please wait for admin review.')
        if c.execute('SELECT id FROM payments WHERE user_id=? AND receipt_file_id=?',(uid,fid)).fetchone():return await update.message.reply_text('⚠️ Receipt already submitted.')
        pid='PM-'+uuid.uuid4().hex[:10].upper();c.execute('INSERT INTO payments VALUES(?,?,?,?,?,?,?,?,?)',(pid,uid,m,m*5000,'pending',fid,'',now(),''))
    states.pop(uid);audit(uid,'payment_submit',pid);await update.message.reply_text(f'✅ Receipt received. Payment ID: <code>{pid}</code>',parse_mode=ParseMode.HTML)
    await retry(lambda:context.bot.send_photo(ADMIN_ID,fid,caption=f'💳 Payment\nUser: <code>{uid}</code>\nPlan: {m} month(s)\nAmount: {m*5000:,} MMK\nID: <code>{pid}</code>',parse_mode=ParseMode.HTML,reply_markup=K([[btn('✅ Approve',f'pa:{pid}'),btn('❌ Reject',f'pr:{pid}')]])))
async def text(update,context):
    uid=update.effective_user.id;s=states.get(uid,{});t=(update.message.text or '').strip()
    if s.get('step')=='name':return await create(update,t)
    if s.get('step')=='months':
        if t not in ('1','2','3'):return await update.message.reply_text('Choose 1, 2, or 3.')
        states[uid]={'step':'receipt','months':int(t)};m=int(t);return await update.message.reply_text(f'💳 {m} month(s): {m*5000:,} MMK\nWavePay: {WAVEPAY[0]} — {WAVEPAY[1]}\nKBZPay: {KBZPAY[0]} — {KBZPAY[1]}\n\nSend receipt photo.',parse_mode=ParseMode.HTML)
    if s.get('step')=='envkey':
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',t):return await update.message.reply_text('Invalid variable name.')
        states[uid]={'step':'envvalue','pid':s['pid'],'key':t};return await update.message.reply_text(f'Value for {t} (masked after save):')
    if s.get('step')=='envvalue':
        with conn() as c:c.execute('INSERT INTO env(project_id,key,value) VALUES(?,?,?) ON CONFLICT(project_id,key) DO UPDATE SET value=excluded.value',(s['pid'],s['key'],update.message.text))
        states.pop(uid,None);audit(uid,'env_upsert',s['pid']);return await update.message.reply_text('✅ Saved. Secret values are masked.')
    if s.get('step')=='reject':
        pid=s['pid'];states.pop(uid,None)
        with conn() as c:c.execute("UPDATE payments SET status='rejected',reviewed_at=?,reason=? WHERE id=? AND status='pending'",(now(),t[:200],pid));p=c.execute('SELECT user_id FROM payments WHERE id=?',(pid,)).fetchone()
        if p:
            with conn() as c:c.execute('INSERT INTO notifications(user_id,title,body,created_at) VALUES(?,?,?,?)',(p['user_id'],'Payment rejected',t[:200],now()))
            await retry(lambda:context.bot.send_message(p['user_id'],f'❌ Payment rejected. Reason: {esc(t[:200])}',parse_mode=ParseMode.HTML))
        audit(uid,'payment_reject',pid);return await update.message.reply_text('❌ Rejected with reason.',reply_markup=K(back('admin')))
    if s.get('step')=='reject_pick' and admin(uid):
        with conn() as c:p=c.execute("SELECT id FROM payments WHERE id=? AND status='pending'",(t,)).fetchone()
        if not p:return await update.message.reply_text('Pending payment ID not found. Try again or /cancel.')
        states[uid]={'step':'reject','pid':t};return await update.message.reply_text('Send rejection reason, or /cancel.')
    if s.get('step')=='search':return await show_projects(update.callback_query,uid,t) if update.callback_query else await update.message.reply_text('Use Projects search button.')
    await update.message.reply_text('Use /start to open the menu.')
async def photo(update,context):
    if states.get(update.effective_user.id,{}).get('step')=='receipt':return await create_payment_photo(update,context)
async def show_projects(q,uid,search=''):
    with conn() as c:r=c.execute('SELECT * FROM projects WHERE user_id=? AND name LIKE ?',(uid,'%'+search+'%')).fetchall()
    kb=[[btn(('🟢 ' if p['status']=='running' else '🔴 ')+p['name'],f'project:{p["id"]}')] for p in r];kb += [[btn('🔎 Search','psearch'),btn('📤 Upload Project','upload')],*back()]
    return await q.edit_message_text(('📂 <b>My Projects</b>'+ (f' — {esc(search)}' if search else '')+'\n\n'+('\n'.join(esc(p['name']) for p in r) if r else 'No projects yet.')),parse_mode=ParseMode.HTML,reply_markup=K(kb))
async def show_project(q,pid,uid):
    with conn() as c:p=c.execute('SELECT * FROM projects WHERE id=? AND user_id=?',(pid,uid)).fetchone()
    if not p:return await q.edit_message_text('❌ Project not found.')
    await q.edit_message_text(f"📦 <b>{esc(p['name'])}</b>\n\nRuntime: {p['runtime']}\nEntrypoint: <code>{esc(p['entrypoint'])}</code>\nStatus: {p['status']}\nStorage: {size(Path(p['path']))/1024/1024:.1f} MB\nAuto restart: {'Enabled' if p['auto_restart'] else 'Disabled'}",parse_mode=ParseMode.HTML,reply_markup=K([[btn('⏹️ Stop' if p['status']=='running' else '▶️ Run',f"{'stop' if p['status']=='running' else 'run'}:{pid}"),btn('🔄 Restart',f'restart:{pid}')],[btn('📜 Logs',f'logs:{pid}'),btn('⚙️ Environment',f'env:{pid}')],[btn('📁 Files',f'files:{pid}'),btn('♻️ Auto Restart',f'ar:{pid}')],[btn('🗑️ Delete',f'del:{pid}')],*back('projects')]))
async def start_project(q,pid,uid):
    with conn() as c:p=c.execute('SELECT * FROM projects WHERE id=? AND user_id=?',(pid,uid)).fetchone()
    if not p:return await q.answer('Not found',show_alert=True)
    if pid in tasks:return await q.answer('Already running',show_alert=True)
    if len(tasks)>=limits(user(uid))[3]:return await q.answer('Process limit reached',show_alert=True)
    tasks[pid]=asyncio.create_task(run_project(p));audit(uid,'project_start',pid);await q.edit_message_text('⏳ Project started.',reply_markup=K([[btn('📜 Logs',f'logs:{pid}'),btn('⏹️ Stop',f'stop:{pid}')],*back(f'project:{pid}')]))
async def install(cmd,root,logp):
    proc=await asyncio.create_subprocess_exec(*cmd,cwd=str(root),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT);assert proc.stdout
    async def consume():
        with open(logp,'a',encoding='utf8') as f:
            async for line in proc.stdout:f.write(mask(line.decode(errors='replace')));f.flush()
    await asyncio.wait_for(asyncio.gather(consume(),proc.wait()),DEP_TIMEOUT)
    if proc.returncode!=0:raise RuntimeError('dependency installation failed')
async def run_project(p):
    pid=p['id'];root=Path(p['path']);lp=LOGS/f'{pid}.log';venv=VENVS/pid;restarts=0
    try:
        if p['runtime']=='python':
            if not (venv/'bin/python').exists():await install([sys.executable,'-m','venv',str(venv)],root,lp)
            if (root/'requirements.txt').exists():await install([str(venv/'bin/pip'),'install','--disable-pip-version-check','-r','requirements.txt'],root,lp)
            elif (root/'pyproject.toml').exists():await install([str(venv/'bin/pip'),'install','--disable-pip-version-check','.'],root,lp)
            cmd=[str(venv/'bin/python'),'-u',p['entrypoint']]
        elif p['runtime']=='node':
            if shutil.which('node') is None:raise RuntimeError('Node.js runtime is unavailable on this deployment')
            if (root/'package-lock.json').exists():await install(['npm','ci','--omit=dev'],root,lp)
            elif (root/'package.json').exists():await install(['npm','install','--omit=dev'],root,lp)
            cmd=['node',p['entrypoint']]
        else:
            cmd=['bash',p['entrypoint']]
        while True:
            env=os.environ.copy()
            with conn() as c:env.update({r['key']:r['value'] for r in c.execute('SELECT key,value FROM env WHERE project_id=?',(pid,)).fetchall()})
            proc=await asyncio.create_subprocess_exec(*cmd,cwd=str(root),env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,start_new_session=True);processes[pid]=proc
            with conn() as c:c.execute("UPDATE projects SET status='running',updated_at=? WHERE id=?",(now(),pid));c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,p['id'],p['user_id'],'running',proc.pid,None,str(lp),now(),None,now()))
            async def readlog():
                with open(lp,'a',encoding='utf8') as f:
                    async for line in proc.stdout:f.write(mask(line.decode(errors='replace')));f.flush()
            await asyncio.wait_for(asyncio.gather(readlog(),proc.wait()),PROCESS_TIMEOUT);code=proc.returncode;processes.pop(pid,None)
            with conn() as c:c.execute("UPDATE projects SET status='stopped',updated_at=? WHERE id=?",(now(),pid));c.execute("UPDATE jobs SET status='finished',exit_code=?,finished_at=? WHERE project_id=? AND status='running'",(code,now(),pid))
            if not p['auto_restart'] or code==0 or restarts>=MAX_RESTARTS or pid not in tasks:break
            restarts+=1;await asyncio.sleep(min(60,2**restarts))
    except asyncio.CancelledError:raise
    except asyncio.TimeoutError as e:
        log.error('runner %s timed out',pid)
        with conn() as c:c.execute("UPDATE jobs SET status='timeout',finished_at=? WHERE project_id=? AND status='running'",(now(),pid))
        with open(lp,'a',encoding='utf8') as f:f.write(f'\nTIMEOUT {mask(e)}\n')
    except Exception as e:
        log.exception('runner %s',pid)
        with conn() as c:c.execute("UPDATE jobs SET status='failed',finished_at=? WHERE project_id=? AND status='running'",(now(),pid))
        with open(lp,'a',encoding='utf8') as f:f.write(f'\nERROR {mask(e)}\n')
        with conn() as c:c.execute("UPDATE projects SET status='stopped' WHERE id=?",(pid,))
    finally:
        pr=processes.pop(pid,None)
        if pr and pr.returncode is None:
            with contextlib.suppress(ProcessLookupError):os.killpg(pr.pid,signal.SIGTERM)
        tasks.pop(pid,None)
async def stop_project(q,pid,uid):
    with conn() as c:p=c.execute('SELECT id FROM projects WHERE id=? AND user_id=?',(pid,uid)).fetchone()
    if not p:return await q.edit_message_text('❌ Project not found.')
    t=tasks.get(pid);pr=processes.get(pid)
    if t:t.cancel()
    if pr and pr.returncode is None:
        with contextlib.suppress(ProcessLookupError):os.killpg(pr.pid,signal.SIGTERM)
    with conn() as c:c.execute("UPDATE projects SET status='stopped',updated_at=? WHERE id=?",(now(),pid));audit(uid,'project_stop',pid)
    await q.edit_message_text('✅ Project stopped.',reply_markup=K(back('projects')))
async def logs(q,pid,uid):
    with conn() as c:ok=c.execute('SELECT 1 FROM projects WHERE id=? AND user_id=?',(pid,uid)).fetchone()
    if not ok:return await q.edit_message_text('❌ Project not found.')
    p=LOGS/f'{pid}.log';txt=mask(p.read_text(errors='replace')[-3500:]) if p.exists() else 'No logs yet.';await q.edit_message_text(f'📜 <b>Logs</b>\n\n<pre>{esc(txt)}</pre>',parse_mode=ParseMode.HTML,reply_markup=K([[btn('🔄 Refresh',f'logs:{pid}')],*back(f'project:{pid}')]))
async def files(q,pid,uid):
    with conn() as c:p=c.execute('SELECT path FROM projects WHERE id=? AND user_id=?',(pid,uid)).fetchone()
    if not p:return await q.edit_message_text('❌ Project not found.')
    root=Path(p['path']);items=[x for x in root.rglob('*') if x.is_file() and '.venv' not in x.parts][:40];rows=[[btn('📄 '+str(x.relative_to(root))[:35],f'fd:{pid}:{x.relative_to(root)}')] for x in items]
    await q.edit_message_text('📁 <b>Files</b>\nSelect a file to download, rename, or delete.',parse_mode=ParseMode.HTML,reply_markup=K(rows+[[btn('➕ New file','fnew:'+pid)],*back(f'project:{pid}')]))
async def file_detail(q,pid,rel,uid):
    with conn() as c:p=c.execute('SELECT path FROM projects WHERE id=? AND user_id=?',(pid,uid)).fetchone()
    if not p:return await q.edit_message_text('Not found')
    root=Path(p['path']).resolve();f=(root/rel).resolve()
    if not str(f).startswith(str(root)+os.sep) or not f.is_file():return await q.edit_message_text('❌ Unsafe or missing path.')
    await q.edit_message_text(f'📄 <code>{esc(f.relative_to(root))}</code>\nSize: {f.stat().st_size} bytes',parse_mode=ParseMode.HTML,reply_markup=K([[btn('⬇️ Download',f'fdl:{pid}:{rel}'),btn('✏️ Rename',f'frn:{pid}:{rel}')],[btn('🗑️ Delete',f'frd:{pid}:{rel}')],*back(f'files:{pid}')]))
async def env_panel(q,pid,uid):
    with conn() as c:
        ok=c.execute('SELECT 1 FROM projects WHERE id=? AND user_id=?',(pid,uid)).fetchone();rows=c.execute('SELECT key FROM env WHERE project_id=? ORDER BY key',(pid,)).fetchall()
    if not ok:return await q.edit_message_text('❌ Project not found.')
    text='⚙️ <b>Environment</b>\n\n'+ ('\n'.join(f"<code>{esc(r['key'])}</code> = ••••••" for r in rows) or 'No variables.')
    await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=K([[btn('➕ Add / Update',f'envadd:{pid}')],[btn('🗑️ Delete variable',f'envdel:{pid}')],*back(f'project:{pid}')]))
async def broadcast_run(q,context):
    s=states.get(ADMIN_ID,{});text=s.get('message');chat=s.get('chat');mid=s.get('message_id')
    with conn() as c:users=c.execute('SELECT user_id FROM users WHERE approved=1 AND blocked=0').fetchall()
    sent=failed=0
    for r in users:
        if ADMIN_ID not in broadcast_jobs:break
        try:await retry(lambda r=r:context.bot.copy_message(r['user_id'],chat,mid));sent+=1
        except (Forbidden,TelegramError):
            failed+=1
            with conn() as c:c.execute('UPDATE users SET blocked=1 WHERE user_id=?',(r['user_id'],))
        await asyncio.sleep(.04)
    audit(ADMIN_ID,'broadcast',f'sent={sent},failed={failed}');broadcast_jobs.pop(ADMIN_ID,None);states.pop(ADMIN_ID,None);await q.edit_message_text(f'✅ Broadcast complete\nSent: {sent}\nFailed/cleaned: {failed}',reply_markup=K(back('admin')))
async def callback(update,context):
    q=update.callback_query;uid=q.from_user.id;d=q.data
    try:
        await q.answer();u=user(uid,update)
        if d not in ('join','verify','menu','request'):
            if u['blocked']:return await q.edit_message_text('🚫 Account blocked.')
            if setting('maintenance','0')=='1' and not admin(uid):return await q.edit_message_text('🛠️ Maintenance mode is active.')
            if not admin(uid) and not u['approved']:return await q.edit_message_text('🛡️ Access approval is required.',reply_markup=K([[btn('📨 Request Access','request')]]))
            if not await joined(context,uid):return await q.edit_message_text('🔒 Join the required channel first.',reply_markup=join_kb())
        if d=='join':return await q.answer(url=REQUIRED_CHANNEL_URL)
        if d=='verify':return await menu(update,u) if await joined(context,uid) else await q.edit_message_text('❌ Join not found.',reply_markup=join_kb())
        if d=='menu':return await menu(update,u)
        if d in ('cancel','home'):states.pop(uid,None);return await menu(update,u)
        if d=='request':
            with conn() as c:c.execute('UPDATE users SET approved=0 WHERE user_id=?',(uid,));audit(uid,'access_request');await retry(lambda:context.bot.send_message(ADMIN_ID,f'📨 Access request from {uid}',reply_markup=K([[btn('✅ Approve',f'ua:{uid}'),btn('🚫 Block',f'ub:{uid}')]])))
            return await q.edit_message_text('✅ Request sent. Admin will review.',reply_markup=K(back()))
        if d=='projects':return await show_projects(q,uid)
        if d=='psearch':states[uid]={'step':'psearch'};return await q.edit_message_text('Send project name search text. /cancel to stop.',reply_markup=K(back('projects')))
        if d=='upload':return await upload_prompt(update,context)
        if d in ('system','health'):
            du=shutil.disk_usage(DATA);return await q.edit_message_text(f'📊 <b>Health</b>\n\n🟢 Native runner online\n🧵 Processes: {len(processes)}\n💾 DATA_DIR: <code>{esc(DATA)}</code>\n📦 Persistent volume: {"detected" if DATA.exists() else "missing"}\n🗃️ Schema: {SCHEMA_VERSION}\n💽 Free disk: {du.free//1024//1024} MB\n🐳 Docker: report-only',parse_mode=ParseMode.HTML,reply_markup=K(back('admin' if admin(uid) and d=='health' else 'menu')))
        if d=='storage':
            du=shutil.disk_usage(DATA);return await q.edit_message_text(f'💾 Storage\nUsed: {used(uid)/1024/1024:.1f} MB\nFree: {du.free/1024/1024:.0f} MB\nPath: <code>{esc(DATA)}</code>',parse_mode=ParseMode.HTML,reply_markup=K(back()))
        if d=='settings':return await q.edit_message_text('⚙️ Settings\nEnvironment values are masked. Auto-restart and runtime are managed per project.',reply_markup=K(back()))
        if d=='help':return await q.edit_message_text('Upload ZIP/source, name it, inspect files/environment, then run. Python and Node are supported. Docker/Compose files are detected and reported only.',reply_markup=K(back()))
        if d=='notifs':
            with conn() as c:rows=c.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 20',(uid,)).fetchall();c.execute('UPDATE notifications SET read=1 WHERE user_id=?',(uid,))
            return await q.edit_message_text('🔔 <b>Notifications</b>\n\n'+('\n'.join(f"{r['created_at'][:16]} — {esc(r['title'])}: {esc(r['body'])}" for r in rows) or 'None'),parse_mode=ParseMode.HTML,reply_markup=K(back()))
        if d=='premium':return await q.edit_message_text('💎 <b>Premium Plans</b>',parse_mode=ParseMode.HTML,reply_markup=K([[btn('📅 1 Month — 5,000 MMK','months:1')],[btn('📅 2 Months — 10,000 MMK','months:2')],[btn('📅 3 Months — 15,000 MMK','months:3')],[btn('📋 Payment Status','pstatus'),btn('🧾 Payment History','phistory')],*back()]))
        if d.startswith('months:'):
            try:m=int(d.split(':',1)[1])
            except ValueError:m=0
            if m not in (1,2,3):return await q.answer('Invalid plan.',show_alert=True)
            states[uid]={'step':'receipt','months':m}
            return await q.edit_message_text(f'💳 {m} month(s): {m*5000:,} MMK\nWavePay: {WAVEPAY[0]} — {WAVEPAY[1]}\nKBZPay: {KBZPAY[0]} — {KBZPAY[1]}\n\nSend receipt photo, or /cancel.',parse_mode=ParseMode.HTML,reply_markup=K(back('premium')))
        if d=='pstatus' or d=='phistory':
            with conn() as c:rows=c.execute('SELECT * FROM payments WHERE user_id=? ORDER BY created_at DESC LIMIT 10',(uid,)).fetchall()
            txt=('💳 Payment Status\n\n' if d=='pstatus' else '🧾 Payment History\n\n')+('\n'.join(f"{r['id']} — {r['status']} — {r['amount']:,} MMK" for r in rows) or 'None');return await q.edit_message_text(txt,reply_markup=K(back('premium')))
        if d=='admin' and admin(uid):return await q.edit_message_text('🛡️ <b>Admin Panel</b>',parse_mode=ParseMode.HTML,reply_markup=admin_kb())
        if d=='maint' and admin(uid):set_setting('maintenance','0' if setting('maintenance')=='1' else '1');audit(uid,'maintenance_toggle',setting('maintenance'));return await q.edit_message_text(f"Maintenance: {setting('maintenance')}",reply_markup=K(back('admin')))
        if d=='cleanup' and admin(uid):return await cleanup(update,context)
        if d=='broadcast' and admin(uid):states[uid]={'step':'broadcast'};return await q.edit_message_text('Send text or any media. It will be previewed before confirmation.',reply_markup=K(back('admin')))
        if d=='bconfirm' and admin(uid):broadcast_jobs[ADMIN_ID]=asyncio.create_task(broadcast_run(q,context));return await q.edit_message_text('📢 Broadcast running. You can pause/cancel.',reply_markup=K([[btn('⏸️ Pause','bpause'),btn('❌ Cancel','bcancel')]]))
        if d=='bpause' and admin(uid):broadcast_jobs.pop(ADMIN_ID,None);return await q.edit_message_text('⏸️ Broadcast paused.',reply_markup=K(back('admin')))
        if d=='bcancel' and admin(uid):broadcast_jobs.pop(ADMIN_ID,None);states.pop(ADMIN_ID,None);return await q.edit_message_text('❌ Broadcast cancelled.',reply_markup=K(back('admin')))
        if d=='users:0' and admin(uid):return await users_page(q,0)
        if d.startswith('users:') and admin(uid):return await users_page(q,int(d.split(':')[1]))
        if d=='payments:0' and admin(uid):return await payments_page(q,0)
        if d.startswith('payments:') and admin(uid):return await payments_page(q,int(d.split(':')[1]))
        if d.startswith('ua:') and admin(uid):return await user_action(q,int(d.split(':')[1]),'approve')
        if d.startswith('ub:') and admin(uid):return await user_action(q,int(d.split(':')[1]),'block')
        if d.startswith('unblock:') and admin(uid):return await user_action(q,int(d.split(':')[1]),'unblock')
        if d.startswith('pa:') and admin(uid):return await approve_payment(q,context,d[3:])
        if d.startswith('pr:') and admin(uid):states[uid]={'step':'reject','pid':d[3:]};return await q.edit_message_text('Send rejection reason, or /cancel.',reply_markup=K(back('admin')))
        if d=='audit:0' and admin(uid):return await audit_page(q,0)
        if d.startswith('audit:') and admin(uid):return await audit_page(q,int(d.split(':')[1]))
        if d=='backup' and admin(uid):return await make_backup(q,uid)
        if d=='backups' and admin(uid):return await backups_page(q)
        if d.startswith('bdl:') and admin(uid):
            target=BACKUPS/Path(d[4:]).name
            if not target.is_file():return await q.answer('Backup missing',show_alert=True)
            await q.message.reply_document(target.open('rb'),filename=target.name);audit(uid,'db_backup_download',target.name);return await q.edit_message_text('✅ Backup sent.',reply_markup=K(back('admin')))
        if d.startswith('restore:') and admin(uid):return await q.edit_message_text('⚠️ Restore this database?',reply_markup=K([[btn('✅ Confirm',f'restoreyes:{d[8:]}'),btn('❌ Cancel','backups')]]))
        if d.startswith('restoreyes:') and admin(uid):return await restore_backup(q,d[11:],uid)
        if d.startswith('project:'):return await show_project(q,d[8:],uid)
        if d.startswith('run:'):return await start_project(q,d[4:],uid)
        if d.startswith('stop:'):return await stop_project(q,d[5:],uid)
        if d.startswith('restart:'):await stop_project(q,d[8:],uid);return await start_project(q,d[8:],uid)
        if d.startswith('logs:'):return await logs(q,d[5:],uid)
        if d.startswith('files:'):return await files(q,d[6:],uid)
        if d.startswith('fd:'):return await file_detail(q,d.split(':',2)[1],d.split(':',2)[2],uid)
        if d.startswith('fdl:'):
            pid,rel=d[4:].split(':',1);return await send_file(q,pid,rel,uid)
        if d.startswith('frd:'):return await q.edit_message_text('Confirm delete?',reply_markup=K([[btn('✅ Confirm',f'frdy:{d[4:]}'),btn('❌ Cancel',f'files:{d[4:].split(":",1)[0]}')]]))
        if d.startswith('frdy:'):return await delete_file(q,d[5:],uid)
        if d.startswith('frn:'):states[uid]={'step':'frename','spec':d[4:]};return await q.edit_message_text('Send the new safe filename, or /cancel.')
        if d.startswith('env:'):return await env_panel(q,d[4:],uid)
        if d.startswith('envadd:'):states[uid]={'step':'envkey','pid':d[7:]};return await q.edit_message_text('Send variable name, or /cancel.',reply_markup=K(back(f'env:{d[7:]}')))
        if d.startswith('envdel:'):states[uid]={'step':'envdel','pid':d[7:]};return await q.edit_message_text('Send variable name to delete, or /cancel.')
        if d=='rejectpick' and admin(uid):
            states[uid]={'step':'reject_pick'};return await q.edit_message_text('Send the Payment ID to reject, or /cancel.')
        if d.startswith('ar:'):
            pid=d[3:]
            with conn() as c:c.execute('UPDATE projects SET auto_restart=1-auto_restart WHERE id=? AND user_id=?',(pid,uid))
            return await show_project(q,pid,uid)
        if d.startswith('del:'):return await q.edit_message_text('🗑️ Delete project?',reply_markup=K([[btn('✅ Confirm',f'dely:{d[4:]}'),btn('❌ Cancel',f'project:{d[4:]}')]]))
        if d.startswith('dely:'):
            pid=d[5:]
            with conn() as c:p=c.execute('SELECT path FROM projects WHERE id=? AND user_id=?',(pid,uid)).fetchone();c.execute('DELETE FROM projects WHERE id=? AND user_id=?',(pid,uid))
            if p:shutil.rmtree(p['path'],ignore_errors=True);shutil.rmtree(VENVS/pid,ignore_errors=True)
            audit(uid,'project_delete',pid);return await q.edit_message_text('✅ Project deleted.',reply_markup=K(back('projects')))
    except Exception as e:
        log.exception('callback');
        with contextlib.suppress(Exception):await q.edit_message_text('⚠️ Something went wrong. Try again.',reply_markup=K(back()))
async def users_page(q,page):
    with conn() as c:rows=c.execute('SELECT * FROM users ORDER BY created_at DESC LIMIT 10 OFFSET ?',(page*10,)).fetchall();total=c.execute('SELECT COUNT(*) n FROM users').fetchone()['n']
    text='👥 <b>Users</b>\n\n'+('\n'.join(f"{r['user_id']} {'✅' if r['approved'] else '⏳'} {'🚫' if r['blocked'] else ''} {esc(r['name'])}" for r in rows) or 'None');kb=[[btn(f"{r['user_id']} {'Unblock' if r['blocked'] else 'Block'}",f"{'unblock' if r['blocked'] else 'ub'}:{r['user_id']}") for r in rows[:1]]];nav=[]
    if page:nav.append(btn('⬅️ Prev',f'users:{page-1}'))
    if (page+1)*10<total:nav.append(btn('Next ➡️',f'users:{page+1}'))
    return await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=K(kb+[nav,*back('admin')]))
async def user_action(q,target,action):
    with conn() as c:
        if action=='approve':c.execute('UPDATE users SET approved=1,blocked=0 WHERE user_id=?',(target,))
        elif action=='block':c.execute('UPDATE users SET blocked=1 WHERE user_id=?',(target,))
        else:c.execute('UPDATE users SET blocked=0 WHERE user_id=?',(target,))
    audit(ADMIN_ID,f'user_{action}',str(target));return await q.edit_message_text(f'✅ User {target}: {action}.',reply_markup=K(back('admin')))
async def payments_page(q,page):
    with conn() as c:rows=c.execute("SELECT * FROM payments WHERE status='pending' ORDER BY created_at DESC LIMIT 10 OFFSET ?",(page*10,)).fetchall()
    kb=[[btn(f"{r['id']} {r['amount']:,} MMK",f'pa:{r["id"]}')] for r in rows];return await q.edit_message_text('💳 <b>Pending Payments</b>\n\n'+('\n'.join(f"{r['id']} — user {r['user_id']} — {r['amount']:,} MMK" for r in rows) or 'None'),parse_mode=ParseMode.HTML,reply_markup=K(kb+[[btn('❌ Reject reason','rejectpick')],*back('admin')]))
async def approve_payment(q,context,pid):
    with conn() as c:p=c.execute("SELECT * FROM payments WHERE id=? AND status='pending'",(pid,)).fetchone()
    if not p:return await q.edit_message_text('Already reviewed.',reply_markup=K(back('admin')))
    until=max(datetime.now(timezone.utc),datetime.fromisoformat(user(p['user_id'])['premium_until']) if user(p['user_id'])['premium_until'] else datetime.now(timezone.utc))+timedelta(days=30*p['months'])
    with conn() as c:c.execute("UPDATE payments SET status='approved',reviewed_at=? WHERE id=?",(now(),pid));c.execute("UPDATE users SET plan='premium',premium_until=? WHERE user_id=?",(until.isoformat(),p['user_id']));c.execute('INSERT INTO notifications(user_id,title,body,created_at) VALUES(?,?,?,?)',(p['user_id'],'Premium approved',f'Expires {until.date()}',now()))
    audit(ADMIN_ID,'payment_approve',pid);await retry(lambda:context.bot.send_message(p['user_id'],f'✅ Payment approved. Premium expires {until.date()}.'));return await q.edit_message_text('✅ Approved.',reply_markup=K(back('admin')))
async def audit_page(q,page):
    with conn() as c:rows=c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 15 OFFSET ?',(page*15,)).fetchall()
    return await q.edit_message_text('📈 <b>Audit Logs</b>\n\n'+('\n'.join(f"{r['created_at'][:16]} {r['user_id']} {esc(r['action'])} {esc(r['detail'])}" for r in rows) or 'None'),parse_mode=ParseMode.HTML,reply_markup=K(back('admin')))
async def make_backup(q,uid):
    target=BACKUPS/f'runner-{int(time.time())}.sqlite3';shutil.copy2(DB,target)
    with conn() as c:c.execute('INSERT INTO backups VALUES(?,?,?,?)',(target.stem,str(target),now(),target.stat().st_size))
    audit(uid,'db_backup',target.name);return await q.edit_message_text(f'✅ Backup created: <code>{target.name}</code>',parse_mode=ParseMode.HTML,reply_markup=K([[btn('⬇️ Download',f'bdl:{target.name}')],*back('admin')]))
async def backups_page(q):
    with conn() as c:rows=c.execute('SELECT * FROM backups ORDER BY created_at DESC LIMIT 10').fetchall()
    return await q.edit_message_text('💾 <b>Database Backups</b>\n\n'+('\n'.join(f"{r['id']} — {r['created_at'][:16]} — {r['size']} bytes" for r in rows) or 'None'),parse_mode=ParseMode.HTML,reply_markup=K([[btn('➕ Create Backup','backup')]]+[[btn('♻️ Restore '+r['id'],f'restore:{r["id"]}')] for r in rows]+back('admin')))
async def restore_backup(q,bid,uid):
    with conn() as c:r=c.execute('SELECT path FROM backups WHERE id=?',(bid,)).fetchone()
    if not r or not Path(r['path']).is_file():return await q.edit_message_text('Backup missing.',reply_markup=K(back('admin')))
    shutil.copy2(r['path'],DB);audit(uid,'db_restore',bid);return await q.edit_message_text('✅ Database restored. Restart the service to reload safely.',reply_markup=K(back('admin')))
async def send_file(q,pid,rel,uid):
    with conn() as c:p=c.execute('SELECT path FROM projects WHERE id=? AND user_id=?',(pid,uid)).fetchone()
    if not p:return await q.answer('Not found',show_alert=True)
    root=Path(p['path']).resolve();f=(root/rel).resolve()
    if not str(f).startswith(str(root)+os.sep) or not f.is_file():return await q.answer('Unsafe path',show_alert=True)
    await q.message.reply_document(f.open('rb'),filename=f.name);audit(uid,'file_download',rel)
async def delete_file(q,spec,uid):
    pid,rel=spec.split(':',1)
    with conn() as c:p=c.execute('SELECT path FROM projects WHERE id=? AND user_id=?',(pid,uid)).fetchone()
    if not p:return await q.edit_message_text('Missing project')
    root=Path(p['path']).resolve();f=(root/rel).resolve()
    if not str(f).startswith(str(root)+os.sep) or not f.is_file():return await q.edit_message_text('Unsafe path')
    f.unlink();audit(uid,'file_delete',rel);return await files(q,pid,uid)
async def cleanup(update,context):
    removed=0
    for d in (VENVS,DATA/'tmp'):
        if d.exists():
            for p in d.iterdir():
                if p.is_dir() and (time.time()-p.stat().st_mtime)>7*86400:shutil.rmtree(p,ignore_errors=True);removed+=1
    for pid,pr in list(processes.items()):
        if pr.returncode is not None:processes.pop(pid,None)
    if update.callback_query:return await update.callback_query.edit_message_text(f'✅ Cleanup complete. Removed {removed} stale caches.',reply_markup=K(back('admin')))
async def media_broadcast(update,context):
    uid=update.effective_user.id
    if admin(uid) and states.get(uid,{}).get('step')=='broadcast':states[uid]={'step':'confirm','chat':update.effective_chat.id,'message_id':update.effective_message.message_id};await update.effective_message.reply_text('📢 Preview ready.',reply_markup=K([[btn('✅ Confirm','bconfirm'),btn('❌ Cancel','admin')]]))
async def text_router(update,context):
    uid=update.effective_user.id;s=states.get(uid,{})
    if s.get('step')=='psearch':
        term=update.message.text.strip()[:48];states.pop(uid,None)
        with conn() as c:rows=c.execute('SELECT * FROM projects WHERE user_id=? AND name LIKE ?',(uid,'%'+term+'%')).fetchall()
        kb=[[btn(('🟢 ' if r['status']=='running' else '🔴 ')+r['name'],f'project:{r["id"]}')] for r in rows]
        return await update.message.reply_text('📂 Search results for '+esc(term)+':',parse_mode=ParseMode.HTML,reply_markup=K(kb+back('projects')))
    if s.get('step')=='frename':
        spec=s['spec'];pid,rel=spec.split(':',1);new=safe(update.message.text);states.pop(uid,None)
        with conn() as c:p=c.execute('SELECT path FROM projects WHERE id=? AND user_id=?',(pid,uid)).fetchone()
        if not p:return await update.message.reply_text('Missing project')
        root=Path(p['path']).resolve();old=(root/rel).resolve();dest=(old.parent/new).resolve()
        if not str(old).startswith(str(root)+os.sep) or not str(dest).startswith(str(root)+os.sep) or not old.is_file() or dest.exists():return await update.message.reply_text('❌ Unsafe or duplicate filename.')
        old.rename(dest);audit(uid,'file_rename',f'{rel}->{new}');return await update.message.reply_text('✅ Renamed.')
    if s.get('step')=='envdel':
        with conn() as c:c.execute('DELETE FROM env WHERE project_id=? AND key=?',(s['pid'],update.message.text.strip()))
        states.pop(uid,None);audit(uid,'env_delete',s['pid']);return await update.message.reply_text('✅ Environment variable deleted.')
    return await text(update,context)
async def cleanup_and_recover(app):
    for p in list(DATA.glob('tmp-*')):shutil.rmtree(p,ignore_errors=True)
    with conn() as c:rows=c.execute("SELECT * FROM projects WHERE status='running' AND auto_restart=1").fetchall();c.execute("UPDATE projects SET status='stopped' WHERE status='running'")
    for p in rows:tasks[p['id']]=asyncio.create_task(run_project(p))
async def expiry_job(context):
    with conn() as c:rows=c.execute("SELECT * FROM users WHERE plan='premium' AND premium_until IS NOT NULL").fetchall()
    for r in rows:
        try:
            until=datetime.fromisoformat(r['premium_until'])
            if until<=datetime.now(timezone.utc):
                with conn() as c:c.execute("UPDATE users SET plan='free' WHERE user_id=?",(r['user_id'],));c.execute('INSERT INTO notifications(user_id,title,body,created_at) VALUES(?,?,?,?)',(r['user_id'],'Premium expired','You were downgraded to Free.',now()))
                await retry(lambda r=r:context.bot.send_message(r['user_id'],'⚠️ Premium expired. Your account is now Free.'))
        except Exception:pass
async def error_handler(update,context):log.exception('telegram update error',exc_info=context.error)
def main():
    init();app=Application.builder().token(BOT_TOKEN).post_init(lambda a:cleanup_and_recover(a)).build();app.add_handler(CommandHandler('start',start));app.add_handler(CommandHandler('cancel',lambda u,c:cancel_cmd(u,c)));app.add_handler(CallbackQueryHandler(callback));app.add_handler(MessageHandler(filters.Document.ALL,document));app.add_handler(MessageHandler(filters.PHOTO,photo));app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_router),group=0);app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,media_broadcast),group=1);app.add_error_handler(error_handler);app.job_queue.run_repeating(expiry_job,interval=3600,first=30);app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=True)
async def cancel_cmd(update,context):states.pop(update.effective_user.id,None);await update.message.reply_text('✅ Cancelled.',reply_markup=main_kb(admin(update.effective_user.id)))
if __name__=='__main__':main()
