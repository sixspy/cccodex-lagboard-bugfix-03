
import hashlib, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS restore_attempts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          rev INTEGER NOT NULL,
          outcome TEXT NOT NULL,
          desk TEXT NOT NULL,
          path TEXT NOT NULL,
          reason TEXT NOT NULL,
          audit_id INTEGER,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cutover_cards(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          path TEXT NOT NULL,
          sealed_at TEXT NOT NULL,
          sealed INTEGER NOT NULL,
          note TEXT NOT NULL
        );
        """)
        # 存量库迁移：sources.rev 为事件版本，恢复成功不推版本，再冻结才推。
        src_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
        if "rev" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN rev INTEGER NOT NULL DEFAULT 0")
        cup_cols = {r["name"] for r in conn.execute("PRAGMA table_info(catchups)")}
        if "rev" not in cup_cols:
            conn.execute("ALTER TABLE catchups ADD COLUMN rev INTEGER NOT NULL DEFAULT 0")
        if "desk" not in cup_cols:
            conn.execute("ALTER TABLE catchups ADD COLUMN desk TEXT NOT NULL DEFAULT '值班席'")
        # 甲径唯一成功兜底：同一源同一事件版本只允许一条成功账。
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_restore_unique_success
                        ON restore_attempts(source, rev) WHERE outcome='成功'""")
        # 双成切断卡写入数据区并封档：择定甲径，当日不改口。
        conn.execute(
            """INSERT OR IGNORE INTO cutover_cards(name,path,sealed_at,sealed,note)
               VALUES (?,?,?,?,?)""",
            ("restore-unique-success", "甲", "2026-11-19", 1,
             "甲径：恢复加锁只允许一次成功，第二次整笔拒绝并保留首次审计；乙径不启用"),
        )
        # 封档后钉死：任何改口（UPDATE/DELETE）直接 ABORT。
        conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_cutover_sealed_no_update
                        BEFORE UPDATE ON cutover_cards WHEN OLD.sealed=1
                        BEGIN SELECT RAISE(ABORT,'双成切断卡已封档，当日不许改口'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_cutover_sealed_no_delete
                        BEFORE DELETE ON cutover_cards WHEN OLD.sealed=1
                        BEGIN SELECT RAISE(ABORT,'双成切断卡已封档，当日不许改口'); END""")
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源","2026-11-16T08:00:00","2026-11-16T08:35:00",35,"告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源","2026-11-16T07:00:00","2026-11-16T09:10:00",130,"违约"))

def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"

def mode_of(level):
    return {"正常":"可写", "告警":"降级只读", "违约":"冻结"}[level]

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}

class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str

@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        # lag by minute rough parse
        def parse(t):
            return datetime.fromisoformat(t)
        lag = int((parse(body.ingest_clock) - parse(body.source_clock)).total_seconds() // 60)
        if lag < 0: lag = 0
        lv = level_of(lag, src["sla_minutes"])
        mode = mode_of(lv)
        c.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                  (body.source, body.source_clock, body.ingest_clock, lag, lv))
        # 每次到达事件推一次源版本；恢复成功本身不推版本，故同版本只可成功恢复一次。
        c.execute("UPDATE sources SET output_mode=?, rev=rev+1 WHERE name=?", (mode, body.source))
        return {"lag_minutes": lag, "level": lv, "output_mode": mode}

@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}

class CatchupIn(BaseModel):
    desk: str = "值班席"
    path: str = "甲"


def _record_attempt(conn, source, rev, outcome, desk, path, reason, audit_id=None):
    conn.execute(
        """INSERT INTO restore_attempts(source,rev,outcome,desk,path,reason,audit_id,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (source, rev, outcome, desk, path, reason, audit_id,
         datetime.now().isoformat(timespec="seconds")),
    )


def sealed_cutover_path() -> str:
    """读取双成切断卡封档口径；入口只按封档路径执行，不在这里另立规则。"""
    with connect() as c:
        row = c.execute(
            "SELECT path FROM cutover_cards WHERE name=? AND sealed=1",
            ("restore-unique-success",),
        ).fetchone()
    return row["path"] if row else "甲"


@app.get("/api/cutover-card")
def cutover_card(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute(
            "SELECT * FROM cutover_cards WHERE name=?",
            ("restore-unique-success",),
        ).fetchone()
    return dict(row) if row else {}


@app.post("/api/catchup/{source}")
def catchup(source: str, body: CatchupIn | None = None,
            authorization: str | None = Header(default=None)):
    """同源恢复唯一成功切断（甲径）。

    甲径：恢复加锁，只允许一次成功；第二次整笔拒绝，首次审计保留。
    乙径不启用；偏离双成切断卡则拒绝并记「双成偏了」。乙径与甲径不得混用。
    """
    require_user(authorization)
    body = body or CatchupIn()
    desk = body.desk or "值班席"
    sealed_path = sealed_cutover_path()
    path = body.path or sealed_path

    conn = connect()
    conn.isolation_level = None  # 显式事务，恢复锁由 BEGIN IMMEDIATE 手动控制
    try:
        src = conn.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if not src:
            raise HTTPException(404, "未知源")
        rev = src["rev"]

        if path != sealed_path:
            # 偏离封档的甲径（含乙径或甲乙混用）：零改，只留偏卡痕迹。
            _record_attempt(conn, source, rev, "双成偏了", desk, path,
                            f"双成偏了：切断卡钉死{sealed_path}径，收到 {path} 径，整笔拒绝零改")
            return JSONResponse(status_code=409, content={
                "ok": False, "outcome": "双成偏了", "source": source,
                "detail": "双成偏了：偏离双成切断卡，已拒绝且零改",
            })

        # 甲径：先占同源恢复锁（写锁），再判唯一成功，再落审计。
        conn.execute("BEGIN IMMEDIATE")
        try:
            src = conn.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
            if not src:
                conn.execute("ROLLBACK")
                raise HTTPException(404, "未知源")
            rev = src["rev"]
            prior = conn.execute(
                "SELECT id FROM restore_attempts WHERE source=? AND rev=? AND outcome='成功'",
                (source, rev),
            ).fetchone()
            if prior:
                # 后交整笔拒绝：不改源、不写第二条成功审计，只留争用痕。
                conn.execute("ROLLBACK")
                _record_attempt(
                    conn, source, rev, "双成功争用", desk, path,
                    "双成功争用：同源同版本已有成功恢复，后交整笔拒绝，保留首次审计",
                )
                return JSONResponse(status_code=409, content={
                    "ok": False, "outcome": "双成功争用", "source": source,
                    "detail": "双成功争用：同源已有一次成功恢复，后交整笔拒绝",
                })
            if src["output_mode"] == "可写":
                # 本版本本就无需恢复：拒绝且零改，不产生第二条成功账。
                conn.execute("ROLLBACK")
                _record_attempt(
                    conn, source, rev, "无需恢复", desk, path,
                    "无需恢复：源当前已是可写且本版本无成功账，拒绝且零改",
                )
                return JSONResponse(status_code=409, content={
                    "ok": False, "outcome": "无需恢复", "source": source,
                    "detail": "无需恢复：源当前已是可写，未重复记成功",
                })

            now = datetime.now().isoformat(timespec="seconds")
            cur = conn.execute(
                """INSERT INTO catchups(source,status,restored_mode,note,created_at,rev,desk)
                   VALUES (?,?,?,?,?,?,?)""",
                (source, "成功", "可写", "补齐后恢复可写", now, rev, desk),
            )
            audit_id = cur.lastrowid
            _record_attempt(conn, source, rev, "成功", desk, path,
                            "同源恢复唯一成功，已封账", audit_id)
            conn.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
            conn.execute("COMMIT")
        except HTTPException:
            raise
        except sqlite3.IntegrityError:
            # 唯一成功索引兜底：并发下也只可能有一条成功。
            conn.execute("ROLLBACK")
            _record_attempt(
                conn, source, rev, "双成功争用", desk, path,
                "双成功争用：唯一成功索引拦截，后交整笔拒绝，保留首次审计",
            )
            return JSONResponse(status_code=409, content={
                "ok": False, "outcome": "双成功争用", "source": source,
                "detail": "双成功争用：同源已有一次成功恢复，后交整笔拒绝",
            })

        return {"ok": True, "output_mode": "可写", "audit_id": audit_id,
                "source": source, "rev": rev, "desk": desk, "path": path}
    finally:
        conn.close()

@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}

def _success_counts(c):
    return {r["source"]: r["n"] for r in c.execute(
        "SELECT source, COUNT(*) n FROM restore_attempts WHERE outcome='成功' GROUP BY source")}

@app.get("/api/audit-appendix")
def audit_appendix(authorization: str | None = Header(default=None)):
    """审计附页：成功、双成功争用、双成偏了全量留痕，成功只出现一次。"""
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute(
            "SELECT * FROM restore_attempts ORDER BY id DESC").fetchall()]}

@app.get("/api/water-level-card")
def water_level_card(authorization: str | None = Header(default=None)):
    """水位卡片：每源当前输出态与恢复成功次数（同源同版本只计一次）。"""
    require_user(authorization)
    with connect() as c:
        counts = _success_counts(c)
        items = []
        for s in c.execute("SELECT * FROM sources ORDER BY id").fetchall():
            items.append({"source": s["name"], "output_mode": s["output_mode"],
                          "rev": s["rev"], "restore_success_count": counts.get(s["name"], 0)})
        return {"items": items}

@app.get("/api/supplement-list")
def supplement_list(authorization: str | None = Header(default=None)):
    """补采列表：只列成功恢复，后交争用不进列表。"""
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute(
            "SELECT * FROM catchups WHERE status='成功' ORDER BY id DESC").fetchall()]}

@app.get("/api/source-catalog")
def source_catalog(authorization: str | None = Header(default=None)):
    """源目录：源清单与恢复成功次数。"""
    require_user(authorization)
    return water_level_card(authorization)

@app.get("/api/handover-card")
def handover_card(authorization: str | None = Header(default=None)):
    """交接卡：只交一次成功恢复，争用与偏卡单列备查。"""
    require_user(authorization)
    with connect() as c:
        restored = [dict(r) for r in c.execute(
            "SELECT * FROM catchups WHERE status='成功' ORDER BY id DESC").fetchall()]
        contentions = c.execute(
            "SELECT COUNT(*) n FROM restore_attempts WHERE outcome='双成功争用'").fetchone()["n"]
        drift = c.execute(
            "SELECT COUNT(*) n FROM restore_attempts WHERE outcome='双成偏了'").fetchone()["n"]
        return {"restored": restored, "contention_count": contentions, "drift_count": drift}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
