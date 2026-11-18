
import hashlib, secrets, sqlite3, threading
from datetime import date
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}
# 摘流清单抢改排队：串行化切换事务，后到者拿到旧 revision 只记争用
SWITCH_LOCK = threading.Lock()

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "入站契约"
SKELETON = "Schema 契约版本约束入站校验，破坏性变更进缓冲或拒收，并与落库结果对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS contracts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          subject TEXT NOT NULL,
          mode TEXT NOT NULL,
          track TEXT NOT NULL,
          status TEXT NOT NULL,
          required_fields TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingress_batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          payload_fields TEXT NOT NULL,
          contract_version TEXT,
          result TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS buffers(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL,
          reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cut_ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          action TEXT NOT NULL,
          path TEXT NOT NULL,
          state TEXT NOT NULL,
          base_revision INTEGER NOT NULL,
          revision INTEGER,
          operator TEXT NOT NULL,
          day TEXT NOT NULL,
          created_at TEXT NOT NULL,
          committed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS cut_directory(
          subject TEXT PRIMARY KEY,
          cut INTEGER NOT NULL,
          path TEXT,
          ledger_id INTEGER,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cut_visible(
          subject TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS cut_deviations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          subject TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cut_contention(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          action TEXT NOT NULL,
          path TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('cut_revision','0')")
        if conn.execute("SELECT COUNT(*) c FROM contracts").fetchone()["c"] == 0:
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v3","订单入站","严格","正式","启用","order_id,amount,currency"))
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v4-shadow","订单入站","兼容","影子","试跑","order_id,amount,currency,channel"))
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v2","库存入站","严格","正式","停用","sku,qty"))
        seed_cutoff(conn)


def seed_cutoff(conn):
    """摘流目录与可见集同源初始化：未登记主题默认未摘流并纳入可见集；已摘流主题不得因重启回流。"""
    subjects = [r["subject"] for r in conn.execute(
        "SELECT DISTINCT subject FROM contracts WHERE track='正式' AND status='启用' ORDER BY subject").fetchall()]
    now = datetime.now().isoformat(timespec="seconds")
    for s in subjects:
        conn.execute("INSERT OR IGNORE INTO cut_directory(subject,cut,path,ledger_id,updated_at) VALUES (?,0,NULL,NULL,?)", (s, now))
    conn.execute("INSERT OR IGNORE INTO cut_visible(subject) SELECT d.subject FROM cut_directory d "
                 "WHERE d.cut=0 AND NOT EXISTS(SELECT 1 FROM cut_visible v WHERE v.subject=d.subject)")


def cut_revision(conn) -> int:
    return int(conn.execute("SELECT value FROM meta WHERE key='cut_revision'").fetchone()["value"])


def _bump_revision(conn):
    conn.execute("UPDATE meta SET value=? WHERE key='cut_revision'", (cut_revision(conn) + 1,))


def _write_visible(conn, subject: str, visible: bool):
    """可见集落库；与摘流清单必须在同一事务内调用。"""
    if visible:
        conn.execute("INSERT OR IGNORE INTO cut_visible(subject) VALUES (?)", (subject,))
    else:
        conn.execute("DELETE FROM cut_visible WHERE subject=?", (subject,))


def record_deviation(conn, kind: str, subject: str, detail: str):
    conn.execute("INSERT INTO cut_deviations(kind,subject,detail,created_at) VALUES (?,?,?,?)",
                 (kind, subject, detail, datetime.now().isoformat(timespec="seconds")))

@app.on_event("startup")
def _s():
    init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM contracts").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "contracts": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/contracts")
def contracts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM contracts ORDER BY id").fetchall()]}

class IngressIn(BaseModel):
    subject: str
    payload_fields: str  # comma fields present

class SwitchIn(BaseModel):
    subject: str
    action: str  # 摘流 | 解除
    path: str    # 甲 = 同一事务；乙 = 草稿确认后原子切
    expected_revision: int


class CommitIn(BaseModel):
    draft_id: int
    expected_revision: int | None = None


INGRESS_POLICY = {
    "cut": "已摘流主题禁止正式通过；未纳入可见集的主题禁止偷接",
    "visible": "入站裁决只认可见集，可见集由摘流切换簿维护",
    "path_a": "甲：摘流清单与可见集同一事务，任一失败整笔回滚",
    "path_b": "乙：先落草稿，确认后一次原子切到正式可见集；确认前按旧可见清单裁决",
    "same_day": "当日选定路径写入摘流切换簿后不许更换，偏离即拒绝并记摘流偏了",
    "contention": "两值班同时抢改排队，后到只记摘流争用，不改清单",
}


@app.get("/api/cutoff/policy")
def cutoff_policy(authorization: str | None = Header(default=None)):
    """策略附页：与摘流目录、可见集、入站详情、对账账册同源。"""
    require_user(authorization)
    with connect() as c:
        return {"policy": INGRESS_POLICY, "revision": cut_revision(c),
                "subjects": [r["subject"] for r in c.execute(
                    "SELECT subject FROM cut_directory ORDER BY subject").fetchall()]}


@app.get("/api/cutoff")
def cutoff(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {
            "revision": cut_revision(c),
            "directory": [dict(r) for r in c.execute(
                "SELECT subject,cut,path,ledger_id,updated_at FROM cut_directory ORDER BY subject").fetchall()],
            "visible": [dict(r) for r in c.execute("SELECT subject FROM cut_visible ORDER BY subject").fetchall()],
            "ledger": [dict(r) for r in c.execute(
                "SELECT * FROM cut_ledger ORDER BY id DESC LIMIT 20").fetchall()],
            "deviations": [dict(r) for r in c.execute(
                "SELECT * FROM cut_deviations ORDER BY id DESC LIMIT 20").fetchall()],
            "contention": [dict(r) for r in c.execute(
                "SELECT * FROM cut_contention ORDER BY id DESC LIMIT 20").fetchall()],
            "drafts_pending": [dict(r) for r in c.execute(
                "SELECT * FROM cut_ledger WHERE state='草稿' ORDER BY id").fetchall()],
        }


def _apply_cutoff(conn, subject: str, action: str, path: str, ledger_id: int):
    """摘流清单与可见集同成同败：任一步失败整笔回滚。"""
    cut = 1 if action == "摘流" else 0
    conn.execute("INSERT INTO cut_directory(subject,cut,path,ledger_id,updated_at) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(subject) DO UPDATE SET cut=excluded.cut,path=excluded.path,"
                 "ledger_id=excluded.ledger_id,updated_at=excluded.updated_at",
                 (subject, cut, path, ledger_id, datetime.now().isoformat(timespec="seconds")))
    _write_visible(conn, subject, visible=(cut == 0))


def _reject(conn, status: int, detail: str, kind: str | None = None, subject: str = ""):
    if kind:
        record_deviation(conn, kind, subject, detail)
    conn.commit()
    raise HTTPException(status, detail)


def begin_write(conn):
    """切换簿写入前取写锁：排队先到者先改，后到者拿到旧 revision 只记争用。"""
    conn.execute("BEGIN IMMEDIATE")


@app.post("/api/cutoff/switch")
def switch(body: SwitchIn, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    if body.action not in ("摘流", "解除"):
        raise HTTPException(422, "动作只能是摘流或解除")
    if body.path not in ("甲", "乙"):
        raise HTTPException(422, "切换路径只能是甲或乙，不得混用")
    today = date.today().isoformat()
    with SWITCH_LOCK, connect() as c:
        begin_write(c)
        if not c.execute("SELECT 1 FROM cut_directory WHERE subject=?", (body.subject,)).fetchone():
            raise HTTPException(404, "摘流目录无此主题")
        if body.expected_revision != cut_revision(c):
            c.execute("INSERT INTO cut_contention(subject,action,path,detail,created_at) VALUES (?,?,?,?,?)",
                      (body.subject, body.action, body.path, "后到，revision 已过期",
                       datetime.now().isoformat(timespec="seconds")))
            c.commit()
            raise HTTPException(409, "摘流争用：后到不改清单")
        prior = c.execute("SELECT * FROM cut_ledger WHERE subject=? AND day=? ORDER BY id",
                          (body.subject, today)).fetchall()
        if prior:
            if any(p["path"] != body.path for p in prior):
                _reject(c, 409, "摘流偏了：当日已选定另一路径，不许换", "摘流偏了", body.subject)
            if any(p["state"] == "草稿" for p in prior):
                _reject(c, 409, "摘流偏了：已有待确认草稿，先确认或撤销再切", "摘流偏了", body.subject)
            if any(p["state"] == "正式" for p in prior):
                _reject(c, 409, "摘流偏了：当日摘流切换已生效，不再重复切换", "摘流偏了", body.subject)
        cur = c.execute("INSERT INTO cut_ledger(subject,action,path,state,base_revision,revision,operator,day,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (body.subject, body.action, body.path, "草稿",
                         body.expected_revision, None, operator, today,
                         datetime.now().isoformat(timespec="seconds")))
        ledger_id = cur.lastrowid
        if body.path == "乙":
            c.commit()
            return {"state": "草稿", "draft_id": ledger_id, "ledger_id": ledger_id,
                    "revision": cut_revision(c), "detail": "草稿待确认，切换前仍按旧可见清单裁决"}
        # 甲路径：摘流清单与可见集同一事务，失败整笔回滚
        _apply_cutoff(c, body.subject, body.action, body.path, ledger_id)
        _bump_revision(c)
        rev = cut_revision(c)
        c.execute("UPDATE cut_ledger SET state='正式',revision=?,committed_at=? WHERE id=?",
                  (rev, datetime.now().isoformat(timespec="seconds"), ledger_id))
        c.commit()
        return {"state": "正式", "ledger_id": ledger_id, "revision": rev}


@app.post("/api/cutoff/commit")
def commit(body: CommitIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with SWITCH_LOCK, connect() as c:
        begin_write(c)
        d = c.execute("SELECT * FROM cut_ledger WHERE id=?", (body.draft_id,)).fetchone()
        if not d or d["state"] != "草稿" or d["path"] != "乙":
            raise HTTPException(409, "只有乙路径草稿可确认切换")
        if body.expected_revision is not None and body.expected_revision != cut_revision(c):
            c.execute("INSERT INTO cut_contention(subject,action,path,detail,created_at) VALUES (?,?,?,?,?)",
                      (d["subject"], d["action"], d["path"], "确认时 revision 已过期",
                       datetime.now().isoformat(timespec="seconds")))
            c.commit()
            raise HTTPException(409, "摘流争用：后到不改清单")
        # 一次原子切到正式可见集
        _apply_cutoff(c, d["subject"], d["action"], d["path"], d["id"])
        _bump_revision(c)
        rev = cut_revision(c)
        c.execute("UPDATE cut_ledger SET state='正式',revision=?,committed_at=? WHERE id=?",
                  (rev, datetime.now().isoformat(timespec="seconds"), d["id"]))
        c.commit()
        return {"state": "正式", "ledger_id": d["id"], "revision": rev}


@app.post("/api/ingress/validate")
def validate(body: IngressIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    fields = {x.strip() for x in body.payload_fields.split(",") if x.strip()}
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        visible = c.execute("SELECT 1 FROM cut_visible WHERE subject=?", (body.subject,)).fetchone()
        directory = c.execute("SELECT cut FROM cut_directory WHERE subject=?", (body.subject,)).fetchone()
        if directory and directory["cut"] == 1:
            c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) "
                      "VALUES (?,?,?,?,?,?)", (body.subject, body.payload_fields, None, "拒收",
                                               "已摘流主题禁止正式通过", now))
            _reject(c, 409, "已摘流主题禁止正式通过，摘流偏了", "摘流偏了", body.subject)
        if not visible:
            enabled = c.execute("SELECT 1 FROM contracts WHERE subject=? AND track='正式' AND status='启用'",
                                (body.subject,)).fetchone()
            if not enabled:
                raise HTTPException(404, "无启用正式契约")
            c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) "
                      "VALUES (?,?,?,?,?,?)", (body.subject, body.payload_fields, None, "拒收",
                                               "未纳入可见集", now))
            _reject(c, 409, "未纳入可见集，禁止未纳入主题偷接", "偷接", body.subject)
        ctr = c.execute("SELECT * FROM contracts WHERE subject=? AND track='正式' AND status='启用' ORDER BY id DESC LIMIT 1", (body.subject,)).fetchone()
        if not ctr:
            raise HTTPException(404, "无启用正式契约")
        need = {x.strip() for x in ctr["required_fields"].split(",") if x.strip()}
        missing = sorted(need - fields)
        if missing:
            if ctr["mode"] == "兼容":
                cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                                (body.subject, body.payload_fields, ctr["version"], "缓冲", "缺字段:"+ ",".join(missing), now))
                c.execute("INSERT INTO buffers(batch_id,reason) VALUES (?,?)", (cur.lastrowid, "缺字段进双写缓冲"))
                return {"result":"缓冲", "version": ctr["version"], "missing": missing, "batch_id": cur.lastrowid}
            cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                            (body.subject, body.payload_fields, ctr["version"], "拒收", "缺字段:"+ ",".join(missing), now))
            return {"result":"拒收", "version": ctr["version"], "missing": missing, "batch_id": cur.lastrowid}
        cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                        (body.subject, body.payload_fields, ctr["version"], "通过", "字段齐全", now))
        return {"result":"通过", "version": ctr["version"], "batch_id": cur.lastrowid}

@app.get("/api/ingress")
def ingress(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM ingress_batches ORDER BY id DESC").fetchall()]}

@app.get("/api/buffers")
def buffers(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT b.*, i.subject, i.payload_fields FROM buffers b JOIN ingress_batches i ON i.id=b.batch_id ORDER BY b.id DESC""").fetchall()
        return {"items":[dict(r) for r in rows]}

@app.get("/api/reconcile")
def reconcile(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        total = c.execute("SELECT COUNT(*) c FROM ingress_batches").fetchone()["c"]
        passed = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='通过'").fetchone()["c"]
        buffered = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='缓冲'").fetchone()["c"]
        rejected = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='拒收'").fetchone()["c"]
        buf_rows = c.execute("SELECT COUNT(*) c FROM buffers").fetchone()["c"]
        cut_active = c.execute("SELECT COUNT(*) c FROM cut_directory WHERE cut=1").fetchone()["c"]
        visible_rows = c.execute("SELECT COUNT(*) c FROM cut_visible").fetchone()["c"]
        dir_rows = c.execute("SELECT COUNT(*) c FROM cut_directory").fetchone()["c"]
        pending = c.execute("SELECT COUNT(*) c FROM cut_ledger WHERE state='草稿'").fetchone()["c"]
        deviations = c.execute("SELECT COUNT(*) c FROM cut_deviations").fetchone()["c"]
        contention = c.execute("SELECT COUNT(*) c FROM cut_contention").fetchone()["c"]
        # 可见集必须与摘流目录未摘流项逐一对应，缺一即错位
        aligned = c.execute(
            "SELECT COUNT(*) c FROM cut_directory d WHERE d.cut=0 AND NOT EXISTS("
            "SELECT 1 FROM cut_visible v WHERE v.subject=d.subject)").fetchone()["c"] == 0 and \
            c.execute(
            "SELECT COUNT(*) c FROM cut_visible v WHERE NOT EXISTS("
            "SELECT 1 FROM cut_directory d WHERE d.subject=v.subject AND d.cut=0)").fetchone()["c"] == 0
    return {"ingress_total": total, "passed": passed, "buffered": buffered, "rejected": rejected,
            "buffer_rows": buf_rows, "aligned": buffered == buf_rows,
            "cutoff_active": cut_active, "cutoff_directory_rows": dir_rows,
            "visible_subjects": visible_rows, "cutoff_drafts_pending": pending,
            "cutoff_deviations": deviations, "cutoff_contention": contention,
            "cutoff_visible_aligned": aligned}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
