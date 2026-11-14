#!/bin/sh
set -eu
export DATA_DIR="${DATA_DIR:-/tmp/vps-bot-self-check}"
rm -rf "$DATA_DIR"
python -m py_compile finalhost.py
python - <<'PY'
import tempfile, zipfile
from pathlib import Path
import finalhost
assert finalhost.BOT_TOKEN == "8717606762:AAHW5_QA3UeTJE7VjgEMJX82GdO2ZhgiQlo"
assert finalhost.ADMIN_ID == 6858000955
assert finalhost.REQUIRED_CHANNEL == "@TeleNexusIo"
assert finalhost.WAVEPAY == ("Phyoe Zaw Htet", "09408026025")
assert finalhost.KBZPAY == ("Wai Phyoe Aung", "09407562154")
assert finalhost.FREE_PROJECTS == 1 and finalhost.PREMIUM_PROJECTS == 5
assert finalhost.FREE_UPLOAD == 100*1024**2 and finalhost.PREMIUM_UPLOAD == 500*1024**2
finalhost.init()
with tempfile.TemporaryDirectory() as d:
    src=Path(d)/'bad.zip'; dst=Path(d)/'out'; dst.mkdir()
    with zipfile.ZipFile(src,'w') as z: z.writestr('../escape.txt','x')
    try: finalhost.extract(src,dst)
    except ValueError: pass
    else: raise SystemExit('unsafe ZIP was accepted')
    link=Path(d)/'link.zip'; out=Path(d)/'link-out'; out.mkdir()
    zi=zipfile.ZipInfo('link'); zi.create_system=3; zi.external_attr=(0o120000 << 16)
    with zipfile.ZipFile(link,'w') as z: z.writestr(zi,'/etc/passwd')
    try: finalhost.extract(link,out)
    except ValueError: pass
    else: raise SystemExit('symlink ZIP was accepted')
    sh=Path(d)/'entry.sh'; sh.write_text('#!/bin/sh\nexit 0\n'); sh.chmod(0o755)
    result=finalhost.scan(Path(d))
    assert result['runtime']=='shell' and result['entry']=='entry.sh'
print('self-check ok')
PY

test -f pyproject.toml
test -f package-lock.json
test -f railway.toml
test -f nixpacks.toml
test -x railway-start.sh || chmod +x railway-start.sh
echo "deployment metadata ok"
