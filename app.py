"""
동물구조단체 홈페이지·CMS 데모 (위시켓 158999 수주 제안용 프로토타입)
Flask + SQLite + Pillow 기반

실행: bash run.sh  (또는 python3 app.py)
접속: http://localhost:8080 (공개) / http://localhost:8080/admin (관리자, 로그인 필요)

데모 계정: admin / demo1234  (로그인 페이지에 표시됨)
"""
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime
from functools import wraps
from html import escape as html_escape
from html.parser import HTMLParser

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.utils import secure_filename

import imageopt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'shelter.db')
UPLOAD_DIR = os.path.join(BASE_DIR, 'static', 'uploads')

app = Flask(__name__)
# 데모용 고정 키. 실제 구축 시 환경변수로 교체 (README 참조)
app.secret_key = os.environ.get('SECRET_KEY', 'demo-only-secret-key')

# ---- 데모 관리자 계정 (실제 구축 시 DB 기반 계정 + bcrypt 해시로 교체) ----
ADMIN_USER = 'admin'
ADMIN_PASS = 'demo1234'

STATUSES = ['구조', '치료중', '보호중', '입양심사중', '임시보호중', '입양완료']
APP_STATUSES = ['접수', '검토중', '승인', '거절']
ALLOWED_EXT = {'jpg', 'jpeg', 'png', 'webp'}
MAX_IMG_DIM = 1200  # 긴 변 최대 px (트래픽 최적화)

MENUS = [  # 메인 + 6개 상위 메뉴 (공고 과업 1)
    ('/', '홈'),
    ('/about', '단체소개'),
    ('/animals', '입양안내'),
    ('/process', '입양절차'),
    ('/support', '후원안내'),
    ('/volunteer', '봉사신청'),
    ('/notices', '소식'),
]


# ---------- DB ----------
def get_db():
    db = getattr(g, '_db', None)
    if db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, '_db', None)
    if db is not None:
        db.close()


def _has_col(db, table, col):
    return any(r['name'] == col for r in db.execute(f'PRAGMA table_info({table})'))


def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS animal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            species TEXT NOT NULL,
            breed TEXT, age TEXT, gender TEXT,
            status TEXT NOT NULL DEFAULT '보호중',
            rescue_date TEXT, rescue_place TEXT,
            description TEXT, photo_emoji TEXT DEFAULT '🐾',
            created_at TEXT, status_changed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS application (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            animal_id INTEGER NOT NULL REFERENCES animal(id),
            applicant_name TEXT NOT NULL,
            phone TEXT NOT NULL, email TEXT,
            address TEXT, housing_type TEXT,
            motive TEXT, status TEXT NOT NULL DEFAULT '접수',
            review_note TEXT,
            applied_at TEXT, processed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notice (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, content TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS volunteer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, phone TEXT NOT NULL,
            area TEXT, message TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS redirect_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            old_path TEXT NOT NULL UNIQUE,
            new_path TEXT NOT NULL,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS medical (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            animal_id INTEGER NOT NULL REFERENCES animal(id),
            record_date TEXT NOT NULL,
            kind TEXT NOT NULL,
            note TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS animal_photo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            animal_id INTEGER NOT NULL REFERENCES animal(id),
            photo_path TEXT NOT NULL,
            photo_sm_path TEXT,
            orig_kb REAL, webp_kb REAL,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            animal_id INTEGER REFERENCES animal(id),
            animal_name TEXT,
            title TEXT NOT NULL,
            content TEXT,
            author TEXT,
            visibility TEXT DEFAULT '공개',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL DEFAULT 'admin',
            action TEXT NOT NULL,
            target_type TEXT,
            target_id INTEGER,
            detail TEXT,
            created_at TEXT
        );
    ''')
    # 스키마 마이그레이션 (기존 DB 호환)
    for col, ddl in [
        ('photo_path', 'TEXT'), ('photo_orig_kb', 'REAL'),
        ('photo_webp_kb', 'REAL'), ('private_note', 'TEXT'),
        ('photo_sm_path', 'TEXT'),
        ('photo_avif_path', 'TEXT'), ('photo_sm_avif_path', 'TEXT'),
        ('photo_blur', 'TEXT'),
    ]:
        if not _has_col(db, 'animal', col):
            db.execute(f'ALTER TABLE animal ADD COLUMN {col} {ddl}')
    for col, ddl in [
        ('photo_avif_path', 'TEXT'), ('photo_sm_avif_path', 'TEXT'),
        ('photo_blur', 'TEXT'),
    ]:
        if not _has_col(db, 'animal_photo', col):
            db.execute(f'ALTER TABLE animal_photo ADD COLUMN {col} {ddl}')
    if not _has_col(db, 'notice', 'visibility'):
        db.execute("ALTER TABLE notice ADD COLUMN visibility TEXT DEFAULT '공개'")
    db.commit()

    if db.execute('SELECT COUNT(*) FROM animal').fetchone()[0] == 0:
        seed(db)
    if db.execute('SELECT COUNT(*) FROM redirect_map').fetchone()[0] == 0:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for old, new in [('/board/notice', '/notices'),
                         ('/freeboard', '/notices'),
                         ('/adopt/list.php', '/animals')]:
            db.execute("INSERT INTO redirect_map (old_path, new_path, created_at) VALUES (?,?,?)",
                       (old, new, now))
    db.commit()


def seed(db):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    animals = [
        ('복실이', '개', '말티즈', '3살', '암컷', '보호중', '2026-09-12', '용인시 처인구',
         '사람을 좋아하고 산책을 즐기는 순한 성격입니다. 기본 훈련이 되어 있어요.', '🐩'),
        ('나비', '고양이', '코리안숏헤어', '1살', '암컷', '보호중', '2026-09-28', '용인시 기흥구',
         '호기심 많고 장난꾸러기예요. 다른 고양이와도 잘 어울립니다.', '🐈'),
        ('초코', '개', '진돗개 믹스', '2살', '수컷', '치료중', '2026-10-02', '용인시 수지구',
         '다리 치료 중입니다. 치료가 끝나면 입양 공고 예정이에요.', '🐕'),
        ('콩이', '개', '포메라니안', '4살', '수컷', '입양심사중', '2026-08-20', '성남시 분당구',
         '활발하고 애교가 많아요. 현재 입양 심사 진행 중입니다.', '🦮'),
        ('야옹이', '고양이', '러시안블루 믹스', '2살', '암컷', '보호중', '2026-09-05', '용인시 처인구',
         '조용하고 얌전한 성격. 혼자 있는 시간을 좋아해요.', '🐈‍⬛'),
        ('해피', '개', '골든리트리버', '5살', '암컷', '입양완료', '2026-07-15', '수원시 영통구',
         '2026년 9월 따뜻한 가족을 만났습니다. 행복하세요!', '🐶'),
        ('토리', '고양이', '스코티시폴드', '1살', '수컷', '임시보호중', '2026-10-05', '용인시 처인구',
         '임시보호 가정에서 돌봄 중입니다.', '😻'),
        ('밤이', '개', '시바견', '3살', '수컷', '보호중', '2026-09-18', '용인시 기흥구',
         '똑똑하고 충성심이 강해요. 야외 활동을 좋아합니다.', '🐕‍🦺'),
    ]
    for a in animals:
        db.execute('''INSERT INTO animal
            (name, species, breed, age, gender, status, rescue_date, rescue_place,
             description, photo_emoji, created_at, status_changed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (*a, now, now))
    kong_id = db.execute("SELECT id FROM animal WHERE name='콩이'").fetchone()[0]
    boksil_id = db.execute("SELECT id FROM animal WHERE name='복실이'").fetchone()[0]
    choco_id = db.execute("SELECT id FROM animal WHERE name='초코'").fetchone()[0]
    db.execute('''INSERT INTO application
        (animal_id, applicant_name, phone, email, address, housing_type, motive, status, applied_at)
        VALUES (?,?,?,?,?,?,?, '검토중', ?)''',
        (kong_id, '김민지', '010-1234-5678', 'minji@example.com', '경기도 용인시', '아파트',
         '어릴 때부터 강아지와 함께 자랐고, 충분한 산책 시간을 확보할 수 있습니다.', now))
    db.execute('''INSERT INTO application
        (animal_id, applicant_name, phone, email, address, housing_type, motive, status, applied_at)
        VALUES (?,?,?,?,?,?,?, '접수', ?)''',
        (boksil_id, '박준호', '010-9876-5432', 'junho@example.com', '경기도 성남시', '단독주택',
         '마당이 있는 집에서 강아지를 키우고 싶습니다.', now))
    # 초코에게는 비공개 메모 시연용 데이터
    db.execute("UPDATE animal SET private_note=? WHERE id=?",
               ('치료비 후원자 협의 중 — 외부 비공개. 담당자: 김봉사', choco_id))
    db.execute("INSERT INTO notice (title, content, visibility, created_at) VALUES (?,?,?,?)",
               ("10월 입양 캠페인 '가을, 가족이 되어주세요' 시작합니다",
                "10월 한 달간 입양 상담소를 운영합니다. 많은 관심 부탁드립니다.", '공개', now))
    db.execute("INSERT INTO notice (title, content, visibility, created_at) VALUES (?,?,?,?)",
               ("해피 입양 후기 - 새로운 가족을 만났어요",
                "골든리트리버 해피가 9월 따뜻한 가족의 품에 안겼습니다.", '공개', now))
    db.execute("INSERT INTO notice (title, content, visibility, created_at) VALUES (?,?,?,?)",
               ("[내부] 11월 이사회 안건 초안",
                "운영진 내부 공유용 글입니다. 로그인한 관리자만 볼 수 있습니다.", '비공개', now))
    # 301 리디렉션 시연용 (구 카페24 URL → 신규 URL)
    for old, new in [('/board/notice', '/notices'),
                     ('/freeboard', '/notices'),
                     ('/adopt/list.php', '/animals')]:
        db.execute("INSERT INTO redirect_map (old_path, new_path, created_at) VALUES (?,?,?)",
                   (old, new, now))
    # 진료 기록 시연용 (초코: 치료중)
    for d, k, note in [('2026-10-03', '진료', '우측 뒷다리 엑스레이 — 골절 소견, 깁스 처치'),
                       ('2026-10-06', '예방접종', '종합백신 2차 접종 완료'),
                       ('2026-10-10', '진료', '깁스 상태 양호, 재활 산책 가능 소견')]:
        db.execute("INSERT INTO medical (animal_id, record_date, kind, note, created_at) VALUES (?,?,?,?,?)",
                   (choco_id, d, k, note, now))
    db.execute("INSERT INTO medical (animal_id, record_date, kind, note, created_at) VALUES (?,?,?,?,?)",
               (boksil_id, '2026-09-20', '예방접종', '종합백신·광견병 접종 완료', now))
    # 입양 후기 시연용 (해피: 입양완료)
    happy_id = db.execute("SELECT id FROM animal WHERE name='해피'").fetchone()[0]
    db.execute("""INSERT INTO review (animal_id, animal_name, title, content, author, visibility, created_at)
                  VALUES (?,?,?,?,?,'공개',?)""",
               (happy_id, '해피', '해피가 우리 가족이 되었어요',
                '처음엔 낯을 가리더니 일주일 만에 무릎 위가 제자리가 되었네요. 산책할 때 꼬리 흔드는 모습을 보면 입양하길 잘했다는 생각이 듭니다.',
                '해피 가족', now))


# ---------- 인증 / CSRF ----------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('admin_logged_in'):
            flash('관리자 로그인이 필요합니다.', 'error')
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return wrapper


@app.before_request
def _csrf_protect():
    if request.method == 'POST':
        tok = session.get('csrf_token')
        sent = request.form.get('csrf_token', '')
        if not tok or not sent or tok != sent:
            abort(400, description='CSRF 검증에 실패했습니다. 페이지를 새로고침 후 다시 시도해주세요.')


@app.context_processor
def _inject_common():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(16)
    return dict(csrf_token=session['csrf_token'], menus=MENUS,
                logged_in=session.get('admin_logged_in', False))


@app.before_request
def _redirect_legacy():
    """구 URL → 신규 URL 301 리디렉션 (데이터 이전 과업 시연)"""
    if request.method == 'GET' and not request.path.startswith('/static/'):
        row = get_db().execute('SELECT new_path FROM redirect_map WHERE old_path=?',
                               (request.path,)).fetchone()
        if row:
            return redirect(row['new_path'], code=301)


# ---------- 이미지 최적화 (트래픽 과업 시연) ----------
# WebP + AVIF + blur-up 플레이스홀더. 실제 변환은 imageopt 모듈.


def save_optimized_image(file_storage):
    """업로드 이미지를 최적화 포맷으로 변환.
    dict 반환: path, sm_path, avif_path, avif_sm_path, blur, orig_kb, opt_kb"""
    return imageopt.optimize_upload(file_storage, UPLOAD_DIR)


# ---------- HTML 정화 (WYSIWYG 에디터 XSS 방지) ----------
_ALLOWED_TAGS = {'p', 'br', 'b', 'strong', 'i', 'em', 'u', 's',
                 'h2', 'h3', 'h4', 'ul', 'ol', 'li', 'blockquote', 'a', 'img', 'hr'}


def sanitize_html(html: str) -> str:
    """WYSIWYG 입력 HTML을 allowlist 기준으로 정화.
    허용: 서식/제목/목록/링크/이미지 태그. script·이벤트핸들러·외부 URL 차단."""
    class _San(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out = []

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag not in _ALLOWED_TAGS:
                return
            at = {k.lower(): v for k, v in attrs}
            if tag == 'a':
                href = (at.get('href') or '').strip()
                if not re.match(r'^(https?://|mailto:|/|#)', href):
                    return
                self.out.append(f'<a href="{html_escape(href)}" target="_blank" rel="noopener">')
            elif tag == 'img':
                src = (at.get('src') or '').strip()
                # 로컬 업로드 경로만 허용 (외부 URL 핫링크·추적 차단)
                if not (src.startswith('uploads/') or src.startswith('/static/uploads/')):
                    return
                alt = html_escape(at.get('alt') or '')
                self.out.append(f'<img src="{html_escape(src)}" alt="{alt}" loading="lazy">')
            elif tag in ('br', 'hr'):
                self.out.append(f'<{tag}>')
            else:
                self.out.append(f'<{tag}>')

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag in _ALLOWED_TAGS and tag not in ('br', 'hr', 'img'):
                self.out.append(f'</{tag}>')

        def handle_data(self, data):
            self.out.append(html_escape(data))

    s = _San()
    s.feed(html or '')
    return ''.join(s.out)


def _req(form, key, label):
    v = (form.get(key) or '').strip()
    if not v:
        raise ValueError(f'{label}을(를) 입력해주세요.')
    return v


def log_action(action, target_type=None, target_id=None, detail=''):
    """관리자 변경 이력 기록 (CMS 감사 로그)"""
    db = get_db()
    db.execute("""INSERT INTO audit_log (actor, action, target_type, target_id, detail, created_at)
                  VALUES (?,?,?,?,?,?)""",
               (session.get('admin_user', 'admin'), action, target_type, target_id, detail,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    db.commit()


@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def internal_error(e):
    return render_template('500.html'), 500


@app.after_request
def _security_and_cache_headers(resp):
    """보안 헤더 + 정적 파일 캐싱 (트래픽 최적화)"""
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if request.path.startswith('/static/'):
        # 정적 파일은 7일간 브라우저 캐시 (트래픽 분산)
        resp.headers['Cache-Control'] = 'public, max-age=604800, immutable'
    return resp


# ---------- 공개: 메인 + 6개 메뉴 ----------
@app.route('/')
def index():
    import json as _json
    db = get_db()
    animals = db.execute(
        "SELECT * FROM animal WHERE status NOT IN ('입양완료','구조') ORDER BY id DESC LIMIT 4").fetchall()
    notices = db.execute(
        "SELECT * FROM notice WHERE visibility='공개' ORDER BY id DESC LIMIT 3").fetchall()
    total = db.execute("SELECT COUNT(*) FROM animal").fetchone()[0]
    adopted = db.execute("SELECT COUNT(*) FROM animal WHERE status='입양완료'").fetchone()[0]
    json_ld = _json.dumps({
        "@context": "https://schema.org",
        "@type": "NGO",
        "name": "행복한발 보호소",
        "description": "위기 동물을 구조하고 평생 가족을 찾아주는 동물보호소",
        "url": "https://example.org/",
    }, ensure_ascii=False)
    return render_template('index.html', animals=animals, notices=notices,
                           total=total, adopted=adopted, json_ld=json_ld)


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/process')
def process():
    return render_template('process.html')


@app.route('/support')
def support():
    return render_template('support.html')


@app.route('/volunteer', methods=['GET', 'POST'])
def volunteer():
    if request.method == 'POST':
        try:
            name = _req(request.form, 'name', '이름')
            phone = _req(request.form, 'phone', '휴대폰 번호')
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(url_for('volunteer'))
        db = get_db()
        db.execute('INSERT INTO volunteer (name, phone, area, message, created_at) VALUES (?,?,?,?,?)',
                   (name, phone, request.form.get('area', '').strip(),
                    request.form.get('message', '').strip(),
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        db.commit()
        return redirect(url_for('volunteer_done'))
    return render_template('volunteer.html')


@app.route('/volunteer/done')
def volunteer_done():
    return render_template('volunteer-done.html')


@app.route('/animals')
def animal_list():
    db = get_db()
    status = request.args.get('status', '')
    q = "SELECT * FROM animal WHERE status != '입양완료'"
    params = []
    if status:
        q += " AND status=?"
        params.append(status)
    q += " ORDER BY id DESC"
    animals = db.execute(q, params).fetchall()
    return render_template('animals.html', animals=animals, status=status, statuses=STATUSES)


@app.route('/animals/<int:aid>')
def detail(aid):
    db = get_db()
    a = db.execute("SELECT * FROM animal WHERE id=?", (aid,)).fetchone()
    if not a:
        return redirect(url_for('animal_list'))
    medical = db.execute("SELECT * FROM medical WHERE animal_id=? ORDER BY record_date DESC",
                         (aid,)).fetchall()
    photos = db.execute("SELECT * FROM animal_photo WHERE animal_id=? ORDER BY sort_order",
                        (aid,)).fetchall()
    return render_template('detail.html', animal=a,
                           adoptable=a['status'] != '입양완료',
                           medical=medical, photos=photos)


@app.route('/apply/<int:aid>', methods=['GET', 'POST'])
def apply(aid):
    db = get_db()
    a = db.execute("SELECT * FROM animal WHERE id=?", (aid,)).fetchone()
    if not a or a['status'] == '입양완료':
        return redirect(url_for('animal_list'))
    if request.method == 'POST':
        try:
            name = _req(request.form, 'applicant_name', '신청자 이름')
            phone = _req(request.form, 'phone', '휴대폰 번호')
            address = _req(request.form, 'address', '주소')
            motive = _req(request.form, 'motive', '입양 동기')
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(url_for('apply', aid=aid))
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db.execute('''INSERT INTO application
            (animal_id, applicant_name, phone, email, address, housing_type, motive, status, applied_at)
            VALUES (?,?,?,?,?,?,?,'접수',?)''',
            (aid, name, phone, request.form.get('email', '').strip(),
             address, request.form.get('housing_type', ''), motive, now))
        if a['status'] == '보호중':
            db.execute("UPDATE animal SET status='입양심사중', status_changed_at=? WHERE id=?",
                       (now, aid))
        db.commit()
        return redirect(url_for('apply_done'))
    return render_template('apply.html', animal=a)


@app.route('/apply/done')
def apply_done():
    return render_template('apply-done.html')


@app.route('/notices')
def notice_list():
    db = get_db()
    notices = db.execute(
        "SELECT * FROM notice WHERE visibility='공개' ORDER BY id DESC").fetchall()
    return render_template('notices.html', notices=notices)


@app.route('/notices/<int:nid>')
def notice_detail(nid):
    db = get_db()
    n = db.execute("SELECT * FROM notice WHERE id=? AND visibility='공개'",
                   (nid,)).fetchone()
    if not n:
        # 비공개 글은 링크를 알아도 열람 불가 (공고 pain 대응)
        abort(404)
    return render_template('notice-detail.html', notice=n)


# ---------- 로그인 ----------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if (request.form.get('username', '').strip() == ADMIN_USER
                and request.form.get('password', '') == ADMIN_PASS):
            session['admin_logged_in'] = True
            session['admin_user'] = ADMIN_USER
            session.permanent = True
            nxt = request.args.get('next') or url_for('admin_dashboard')
            return redirect(nxt)
        flash('아이디 또는 비밀번호가 올바르지 않습니다.', 'error')
    return render_template('login.html', demo_user=ADMIN_USER, demo_pass=ADMIN_PASS)


@app.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('index'))


# ---------- 관리자 (로그인 필요) ----------
@app.route('/admin')
@login_required
def admin_dashboard():
    db = get_db()
    counts = {s: db.execute("SELECT COUNT(*) FROM animal WHERE status=?", (s,)).fetchone()[0]
              for s in STATUSES}
    total = db.execute("SELECT COUNT(*) FROM animal").fetchone()[0]
    pending = db.execute(
        "SELECT COUNT(*) FROM application WHERE status IN ('접수','검토중')").fetchone()[0]
    volunteers = db.execute("SELECT COUNT(*) FROM volunteer").fetchone()[0]
    recent_animals = db.execute("SELECT * FROM animal ORDER BY id DESC LIMIT 5").fetchall()
    recent_apps = db.execute('''SELECT ap.*, an.name AS animal_name FROM application ap
        JOIN animal an ON ap.animal_id = an.id ORDER BY ap.id DESC LIMIT 5''').fetchall()
    return render_template('admin/dashboard.html', total=total, counts=counts,
                           pending_apps=pending, volunteers=volunteers,
                           recent_animals=recent_animals, recent_apps=recent_apps,
                           statuses=STATUSES)


@app.route('/admin/animals')
@login_required
def admin_animals():
    db = get_db()
    q = "SELECT * FROM animal WHERE 1=1"
    params = []
    kw = request.args.get('keyword', '').strip()
    species = request.args.get('species', '')
    status = request.args.get('status', '')
    if kw:
        q += " AND (name LIKE ? OR breed LIKE ?)"
        params += [f'%{kw}%', f'%{kw}%']
    if species:
        q += " AND species=?"
        params.append(species)
    if status:
        q += " AND status=?"
        params.append(status)
    q += " ORDER BY id DESC"
    animals = db.execute(q, params).fetchall()
    return render_template('admin/animals.html', animals=animals, keyword=kw,
                           species=species, status=status, statuses=STATUSES)


@app.route('/admin/animals/new')
@login_required
def animal_new():
    return render_template('admin/animal-form.html', animal=None, statuses=STATUSES)


@app.route('/admin/animals/<int:aid>/edit')
@login_required
def animal_edit(aid):
    db = get_db()
    a = db.execute("SELECT * FROM animal WHERE id=?", (aid,)).fetchone()
    if not a:
        return redirect(url_for('admin_animals'))
    medical = db.execute("SELECT * FROM medical WHERE animal_id=? ORDER BY record_date DESC",
                         (aid,)).fetchall()
    photos = db.execute("SELECT * FROM animal_photo WHERE animal_id=? ORDER BY sort_order",
                        (aid,)).fetchall()
    return render_template('admin/animal-form.html', animal=a, statuses=STATUSES,
                           medical=medical, photos=photos)


@app.route('/admin/animals/save', methods=['POST'])
@login_required
def animal_save():
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    f = request.form
    try:
        name = _req(f, 'name', '이름')
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(url_for('animal_new') if not f.get('id')
                        else url_for('animal_edit', aid=f.get('id')))
    # 사진 업로드 → WebP/AVIF 자동 변환·리사이즈 + blur 플레이스홀더 (트래픽 최적화 시연)
    photo = {'path': f.get('existing_photo', '') or None, 'sm_path': None,
             'avif_path': None, 'avif_sm_path': None, 'blur': None,
             'orig_kb': None, 'opt_kb': None}
    upload = request.files.get('photo')
    if upload and upload.filename:
        try:
            photo = save_optimized_image(upload)
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(url_for('animal_new') if not f.get('id')
                            else url_for('animal_edit', aid=f.get('id')))
    aid = f.get('id')
    if aid:
        old = db.execute("""SELECT photo_path, photo_sm_path, photo_avif_path,
            photo_sm_avif_path, photo_blur, photo_orig_kb, photo_webp_kb
            FROM animal WHERE id=?""", (aid,)).fetchone()
        if photo['path'] is None and old:
            photo = {'path': old['photo_path'], 'sm_path': old['photo_sm_path'],
                     'avif_path': old['photo_avif_path'],
                     'avif_sm_path': old['photo_sm_avif_path'],
                     'blur': old['photo_blur'],
                     'orig_kb': old['photo_orig_kb'], 'opt_kb': old['photo_webp_kb']}
        db.execute('''UPDATE animal SET name=?, species=?, breed=?, age=?, gender=?,
            status=?, rescue_date=?, rescue_place=?, description=?, photo_emoji=?,
            photo_path=?, photo_sm_path=?, photo_avif_path=?, photo_sm_avif_path=?,
            photo_blur=?, photo_orig_kb=?, photo_webp_kb=?, private_note=?,
            status_changed_at=? WHERE id=?''',
            (name, f['species'], f.get('breed', ''), f.get('age', ''),
             f['gender'], f['status'], f.get('rescue_date', ''),
             f.get('rescue_place', ''), f.get('description', ''),
             f.get('photo_emoji', '🐾'), photo['path'], photo['sm_path'],
             photo['avif_path'], photo['avif_sm_path'], photo['blur'],
             photo['orig_kb'], photo['opt_kb'],
             f.get('private_note', '').strip(), now, aid))
        if photo['opt_kb'] is not None and old and old['photo_path'] \
                and old['photo_path'] != photo['path']:
            for p in (old['photo_path'], old['photo_sm_path'],
                      old['photo_avif_path'], old['photo_sm_avif_path']):
                if p:
                    try:
                        os.remove(os.path.join(BASE_DIR, 'static', p))
                    except OSError:
                        pass
        log_action('개체 수정', 'animal', aid, f'{name} 정보 수정')
    else:
        cur = db.execute('''INSERT INTO animal
            (name, species, breed, age, gender, status, rescue_date, rescue_place,
             description, photo_emoji, photo_path, photo_sm_path, photo_avif_path,
             photo_sm_avif_path, photo_blur, photo_orig_kb, photo_webp_kb,
             private_note, created_at, status_changed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (name, f['species'], f.get('breed', ''), f.get('age', ''),
             f['gender'], f.get('status', '보호중'), f.get('rescue_date', ''),
             f.get('rescue_place', ''), f.get('description', ''),
             f.get('photo_emoji', '🐾'), photo['path'], photo['sm_path'],
             photo['avif_path'], photo['avif_sm_path'], photo['blur'],
             photo['orig_kb'], photo['opt_kb'],
             f.get('private_note', '').strip(), now, now))
        log_action('개체 등록', 'animal', cur.lastrowid, f'{name} 신규 등록')
    db.commit()
    if photo['opt_kb'] is not None and photo['orig_kb']:
        flash(f"사진이 자동 최적화되었습니다. ({photo['orig_kb']}KB → {photo['opt_kb']}KB"
              + (", AVIF 포함" if photo['avif_path'] else "") + ")", 'success')
    return redirect(url_for('admin_animals'))


@app.route('/admin/animals/<int:aid>/status', methods=['POST'])
@login_required
def animal_status(aid):
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new_status = request.form.get('status', '')
    if new_status not in STATUSES:
        abort(400)
    db.execute("UPDATE animal SET status=?, status_changed_at=? WHERE id=?",
               (new_status, now, aid))
    if new_status == '입양완료':
        db.execute('''UPDATE application SET status='거절',
            review_note='해당 개체 입양 완료로 인한 자동 마감', processed_at=?
            WHERE animal_id=? AND status IN ('접수','검토중')''', (now, aid))
    db.commit()
    log_action('상태 변경', 'animal', aid, f'상태 → {new_status}')
    return redirect(url_for('admin_animals'))


@app.route('/admin/animals/<int:aid>/delete', methods=['POST'])
@login_required
def animal_delete(aid):
    db = get_db()
    row = db.execute("SELECT name FROM animal WHERE id=?", (aid,)).fetchone()
    _delete_animal_files(db, aid)
    db.execute("DELETE FROM application WHERE animal_id=?", (aid,))
    db.execute("DELETE FROM medical WHERE animal_id=?", (aid,))
    db.execute("DELETE FROM animal_photo WHERE animal_id=?", (aid,))
    db.execute("DELETE FROM animal WHERE id=?", (aid,))
    db.commit()
    log_action('개체 삭제', 'animal', aid, row['name'] if row else '')
    return redirect(url_for('admin_animals'))


@app.route('/admin/applications')
@login_required
def admin_apps():
    db = get_db()
    status = request.args.get('status', '')
    q = '''SELECT ap.*, an.name AS animal_name FROM application ap
           JOIN animal an ON ap.animal_id = an.id'''
    params = []
    if status:
        q += " WHERE ap.status=?"
        params.append(status)
    q += " ORDER BY ap.id DESC"
    apps = db.execute(q, params).fetchall()
    return render_template('admin/applications.html', apps=apps, status=status,
                           app_statuses=APP_STATUSES)


@app.route('/admin/applications/<int:pid>')
@login_required
def app_detail(pid):
    db = get_db()
    a = db.execute('''SELECT ap.*, an.name AS animal_name,
                      an.species AS animal_species, an.breed AS animal_breed
                      FROM application ap JOIN animal an ON ap.animal_id = an.id
                      WHERE ap.id=?''', (pid,)).fetchone()
    if not a:
        return redirect(url_for('admin_apps'))
    return render_template('admin/application-detail.html', app=a)


@app.route('/admin/applications/<int:pid>/review', methods=['POST'])
@login_required
def app_review(pid):
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    decision = request.form.get('decision', '')
    if decision not in ('승인', '거절'):
        abort(400)
    note = request.form.get('note', '').strip()
    row = db.execute("SELECT animal_id FROM application WHERE id=?", (pid,)).fetchone()
    if not row:
        return redirect(url_for('admin_apps'))
    aid = row[0]
    db.execute("UPDATE application SET status=?, review_note=?, processed_at=? WHERE id=?",
               (decision, note, now, pid))
    if decision == '승인':
        db.execute("UPDATE animal SET status='입양완료', status_changed_at=? WHERE id=?",
                   (now, aid))
        db.execute('''UPDATE application SET status='거절',
            review_note='해당 개체 입양 완료로 인한 자동 마감', processed_at=?
            WHERE animal_id=? AND status IN ('접수','검토중') AND id != ?''',
            (now, aid, pid))
    elif decision == '거절':
        left = db.execute('''SELECT COUNT(*) FROM application
            WHERE animal_id=? AND status IN ('접수','검토중') AND id != ?''',
            (aid, pid)).fetchone()[0]
        cur = db.execute("SELECT status FROM animal WHERE id=?", (aid,)).fetchone()[0]
        if left == 0 and cur == '입양심사중':
            db.execute("UPDATE animal SET status='보호중', status_changed_at=? WHERE id=?",
                       (now, aid))
    db.commit()
    log_action(f'입양신청 {decision}', 'application', pid, f'개체#{aid} / 메모: {note[:30]}')
    return redirect(url_for('admin_apps'))


@app.route('/admin/notices')
@login_required
def admin_notices():
    db = get_db()
    notices = db.execute("SELECT * FROM notice ORDER BY id DESC").fetchall()
    return render_template('admin/notices.html', notices=notices)


@app.route('/admin/notices/new')
@login_required
def notice_new():
    return render_template('admin/notice-form.html', notice=None)


@app.route('/admin/notices/<int:nid>/edit')
@login_required
def notice_edit(nid):
    db = get_db()
    notice = db.execute("SELECT * FROM notice WHERE id=?", (nid,)).fetchone()
    if not notice:
        abort(404)
    return render_template('admin/notice-form.html', notice=notice)


@app.route('/admin/upload_image', methods=['POST'])
@login_required
def upload_image():
    """WYSIWYG 에디터용 이미지 업로드 — 최적화 후 JSON URL 반환"""
    up = request.files.get('image')
    if not up or not up.filename:
        return jsonify({'ok': False, 'error': '이미지를 선택해주세요.'}), 400
    try:
        opt = save_optimized_image(up)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    log_action('에디터 이미지 업로드', 'notice', None, up.filename)
    return jsonify({'ok': True,
                    'url': url_for('static', filename=opt['path']),
                    'orig_kb': opt['orig_kb'], 'opt_kb': opt['opt_kb']})


@app.route('/admin/notices/save', methods=['POST'])
@login_required
def notice_save():
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        title = _req(request.form, 'title', '제목')
        content = _req(request.form, 'content', '내용')
    except ValueError as e:
        flash(str(e), 'error')
        _nid = request.form.get('id')
        return redirect(url_for('notice_edit', nid=_nid) if _nid else url_for('notice_new'))
    content = sanitize_html(content)  # WYSIWYG HTML 정화 (XSS 방지)
    if not content.strip():
        flash('내용을 입력해주세요.', 'error')
        _nid = request.form.get('id')
        return redirect(url_for('notice_edit', nid=_nid) if _nid else url_for('notice_new'))
    visibility = request.form.get('visibility', '공개')
    if visibility not in ('공개', '비공개'):
        visibility = '공개'
    nid = request.form.get('id')
    if nid:  # 수정
        db.execute("UPDATE notice SET title=?, content=?, visibility=? WHERE id=?",
                   (title, content, visibility, nid))
        db.commit()
        log_action('소식 수정', 'notice', nid, title)
        flash('소식을 수정했습니다.', 'success')
    else:  # 신규
        db.execute("INSERT INTO notice (title, content, visibility, created_at) VALUES (?,?,?,?)",
                   (title, content, visibility, now))
        db.commit()
        log_action('소식 등록', 'notice', None, title)
        flash('소식을 등록했습니다.', 'success')
    return redirect(url_for('admin_notices'))


@app.route('/admin/notices/<int:nid>/delete', methods=['POST'])
@login_required
def notice_delete(nid):
    db = get_db()
    db.execute("DELETE FROM notice WHERE id=?", (nid,))
    db.commit()
    log_action('소식 삭제', 'notice', nid, '')
    return redirect(url_for('admin_notices'))


@app.route('/admin/volunteers')
@login_required
def admin_volunteers():
    db = get_db()
    volunteers = db.execute("SELECT * FROM volunteer ORDER BY id DESC").fetchall()
    return render_template('admin/volunteers.html', volunteers=volunteers)


@app.route('/admin/redirects')
@login_required
def admin_redirects():
    """301 리디렉션 맵 관리 (데이터 이전 과업 시연)"""
    db = get_db()
    redirects = db.execute("SELECT * FROM redirect_map ORDER BY id").fetchall()
    return render_template('admin/redirects.html', redirects=redirects)


@app.route('/admin/redirects/add', methods=['POST'])
@login_required
def redirect_add():
    try:
        old = _req(request.form, 'old_path', '구 URL')
        new = _req(request.form, 'new_path', '신규 URL')
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(url_for('admin_redirects'))
    if not old.startswith('/'):
        old = '/' + old
    if not new.startswith('/'):
        new = '/' + new
    db = get_db()
    try:
        db.execute("INSERT INTO redirect_map (old_path, new_path, created_at) VALUES (?,?,?)",
                   (old, new, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        db.commit()
        log_action('URL 이전 등록', 'redirect', None, f'{old} → {new}')
    except sqlite3.IntegrityError:
        flash('이미 등록된 구 URL입니다.', 'error')
    return redirect(url_for('admin_redirects'))


@app.route('/admin/redirects/<int:rid>/delete', methods=['POST'])
@login_required
def redirect_delete(rid):
    db = get_db()
    db.execute("DELETE FROM redirect_map WHERE id=?", (rid,))
    db.commit()
    return redirect(url_for('admin_redirects'))


# ---------- 변경 이력 (감사 로그) ----------
@app.route('/admin/audit')
@login_required
def admin_audit():
    db = get_db()
    logs = db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200").fetchall()
    return render_template('admin/audit.html', logs=logs)


# ---------- 일괄 작업 ----------
def _delete_animal_files(db, aid):
    """개체 삭제 시 관련 이미지 파일 정리 (대표+갤러리, WebP/AVIF 포함)"""
    row = db.execute("""SELECT photo_path, photo_sm_path, photo_avif_path,
        photo_sm_avif_path FROM animal WHERE id=?""", (aid,)).fetchone()
    paths = []
    if row:
        paths += [row['photo_path'], row['photo_sm_path'],
                  row['photo_avif_path'], row['photo_sm_avif_path']]
    for r in db.execute("""SELECT photo_path, photo_sm_path, photo_avif_path,
        photo_sm_avif_path FROM animal_photo WHERE animal_id=?""", (aid,)):
        paths += [r['photo_path'], r['photo_sm_path'],
                  r['photo_avif_path'], r['photo_sm_avif_path']]
    for p in paths:
        if p:
            try:
                os.remove(os.path.join(BASE_DIR, 'static', p))
            except OSError:
                pass


@app.route('/admin/animals/bulk', methods=['POST'])
@login_required
def animal_bulk():
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    ids = request.form.getlist('ids')
    op = request.form.get('op', '')
    if not ids:
        flash('선택된 개체가 없습니다.', 'error')
        return redirect(url_for('admin_animals'))
    ids = [int(i) for i in ids if i.isdigit()]
    if op == 'delete':
        for aid in ids:
            _delete_animal_files(db, aid)
            db.execute("DELETE FROM application WHERE animal_id=?", (aid,))
            db.execute("DELETE FROM medical WHERE animal_id=?", (aid,))
            db.execute("DELETE FROM animal_photo WHERE animal_id=?", (aid,))
            db.execute("DELETE FROM animal WHERE id=?", (aid,))
        db.commit()
        log_action('개체 일괄 삭제', 'animal', None, f'{len(ids)}건')
        flash(f'{len(ids)}건을 삭제했습니다.', 'success')
    elif op in STATUSES:
        db.executemany("UPDATE animal SET status=?, status_changed_at=? WHERE id=?",
                       [(op, now, aid) for aid in ids])
        db.commit()
        log_action('개체 일괄 상태변경', 'animal', None, f'{len(ids)}건 → {op}')
        flash(f'{len(ids)}건의 상태를 "{op}"(으)로 변경했습니다.', 'success')
    return redirect(url_for('admin_animals'))


# ---------- 미리보기 (저장 전 확인) ----------
@app.route('/admin/animals/preview', methods=['POST'])
@login_required
def animal_preview():
    """저장하지 않은 폼 내용을 공개 상세 화면으로 미리보기"""
    f = request.form
    preview = {
        'id': 0, 'name': f.get('name', '(이름 미입력)'),
        'species': f.get('species', '개'), 'breed': f.get('breed', ''),
        'age': f.get('age', ''), 'gender': f.get('gender', ''),
        'status': f.get('status', '보호중'),
        'rescue_date': f.get('rescue_date', ''), 'rescue_place': f.get('rescue_place', ''),
        'description': f.get('description', ''),
        'photo_emoji': f.get('photo_emoji', '🐾'),
        'photo_path': f.get('existing_photo', '') or None,
        'photo_sm_path': None,
    }
    return render_template('detail.html', animal=preview, adoptable=True,
                           is_preview=True, medical=[], photos=[])


@app.route('/admin/notices/preview', methods=['POST'])
@login_required
def notice_preview():
    preview = {'id': 0, 'title': request.form.get('title', '(제목 미입력)'),
               'content': sanitize_html(request.form.get('content', '')),
               'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    return render_template('notice-detail.html', notice=preview, is_preview=True)


# ---------- 진료 기록 ----------
@app.route('/admin/medical/add', methods=['POST'])
@login_required
def medical_add():
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        aid = int(_req(request.form, 'animal_id', '개체'))
        date = _req(request.form, 'record_date', '날짜')
        kind = _req(request.form, 'kind', '구분')
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(url_for('admin_animals'))
    db.execute("INSERT INTO medical (animal_id, record_date, kind, note, created_at) VALUES (?,?,?,?,?)",
               (aid, date, kind, request.form.get('note', '').strip(), now))
    db.commit()
    log_action('진료기록 등록', 'medical', aid, f'{date} {kind}')
    return redirect(url_for('animal_edit', aid=aid))


@app.route('/admin/medical/<int:mid>/delete', methods=['POST'])
@login_required
def medical_delete(mid):
    db = get_db()
    row = db.execute("SELECT animal_id FROM medical WHERE id=?", (mid,)).fetchone()
    db.execute("DELETE FROM medical WHERE id=?", (mid,))
    db.commit()
    log_action('진료기록 삭제', 'medical', mid, '')
    return redirect(url_for('animal_edit', aid=row['animal_id'] if row else 0))


# ---------- 사진 갤러리 ----------
@app.route('/admin/photos/add', methods=['POST'])
@login_required
def photo_add():
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        aid = int(_req(request.form, 'animal_id', '개체'))
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(url_for('admin_animals'))
    files = request.files.getlist('photos')
    max_order = db.execute("SELECT COALESCE(MAX(sort_order),0) FROM animal_photo WHERE animal_id=?",
                           (aid,)).fetchone()[0]
    added = 0
    for up in files:
        if not up or not up.filename:
            continue
        try:
            opt = save_optimized_image(up)
        except ValueError as e:
            flash(str(e), 'error')
            continue
        max_order += 1
        db.execute("""INSERT INTO animal_photo
            (animal_id, photo_path, photo_sm_path, photo_avif_path, photo_sm_avif_path,
             photo_blur, orig_kb, webp_kb, sort_order, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (aid, opt['path'], opt['sm_path'], opt['avif_path'], opt['avif_sm_path'],
             opt['blur'], opt['orig_kb'], opt['opt_kb'], max_order, now))
        added += 1
    db.commit()
    if added:
        log_action('갤러리 사진 추가', 'animal_photo', aid, f'{added}장')
        flash(f'사진 {added}장을 추가했습니다.', 'success')
    return redirect(url_for('animal_edit', aid=aid))


@app.route('/admin/photos/<int:pid>/delete', methods=['POST'])
@login_required
def photo_delete(pid):
    db = get_db()
    row = db.execute("""SELECT animal_id, photo_path, photo_sm_path, photo_avif_path,
        photo_sm_avif_path FROM animal_photo WHERE id=?""", (pid,)).fetchone()
    if row:
        for p in (row['photo_path'], row['photo_sm_path'],
                  row['photo_avif_path'], row['photo_sm_avif_path']):
            if p:
                try:
                    os.remove(os.path.join(BASE_DIR, 'static', p))
                except OSError:
                    pass
        db.execute("DELETE FROM animal_photo WHERE id=?", (pid,))
        db.commit()
        log_action('갤러리 사진 삭제', 'animal_photo', pid, '')
    return redirect(url_for('animal_edit', aid=row['animal_id'] if row else 0))


# ---------- 입양 후기 ----------
@app.route('/reviews')
def review_list():
    db = get_db()
    reviews = db.execute("SELECT * FROM review WHERE visibility='공개' ORDER BY id DESC").fetchall()
    return render_template('reviews.html', reviews=reviews)


@app.route('/admin/reviews')
@login_required
def admin_reviews():
    db = get_db()
    reviews = db.execute("SELECT * FROM review ORDER BY id DESC").fetchall()
    animals = db.execute("SELECT id, name FROM animal WHERE status='입양완료' ORDER BY name").fetchall()
    return render_template('admin/reviews.html', reviews=reviews, animals=animals)


@app.route('/admin/reviews/save', methods=['POST'])
@login_required
def review_save():
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        title = _req(request.form, 'title', '제목')
        content = _req(request.form, 'content', '내용')
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(url_for('admin_reviews'))
    aid = request.form.get('animal_id') or None
    aname = ''
    if aid:
        r = db.execute("SELECT name FROM animal WHERE id=?", (aid,)).fetchone()
        aname = r['name'] if r else ''
    else:
        aname = request.form.get('animal_name', '').strip()
    db.execute("""INSERT INTO review (animal_id, animal_name, title, content, author, visibility, created_at)
                  VALUES (?,?,?,?,?,?,?)""",
               (aid, aname, title, content, request.form.get('author', '').strip(),
                request.form.get('visibility', '공개'), now))
    db.commit()
    log_action('입양후기 등록', 'review', None, title)
    return redirect(url_for('admin_reviews'))


@app.route('/admin/reviews/<int:rid>/delete', methods=['POST'])
@login_required
def review_delete(rid):
    db = get_db()
    db.execute("DELETE FROM review WHERE id=?", (rid,))
    db.commit()
    log_action('입양후기 삭제', 'review', rid, '')
    return redirect(url_for('admin_reviews'))


# ---------- CSV 일괄 가져오기 (데이터 이전) ----------
CSV_COLUMNS = ['name', 'species', 'breed', 'age', 'gender', 'status',
               'rescue_date', 'rescue_place', 'description']


@app.route('/admin/import', methods=['GET', 'POST'])
@login_required
def admin_import():
    """구 시스템(카페24 등) 엑셀→CSV 내보내기를 일괄 등록"""
    result = None
    if request.method == 'POST':
        import csv
        up = request.files.get('csvfile')
        if not up or not up.filename:
            flash('CSV 파일을 선택해주세요.', 'error')
            return redirect(url_for('admin_import'))
        try:
            text = up.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            flash('UTF-8 인코딩의 CSV 파일만 가능합니다.', 'error')
            return redirect(url_for('admin_import'))
        reader = csv.DictReader(text.splitlines())
        db = get_db()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ok, skipped = 0, []
        for i, row in enumerate(reader, start=2):
            name = (row.get('name') or '').strip()
            species = (row.get('species') or '개').strip()
            if not name:
                skipped.append(f'{i}행: 이름 없음'); continue
            status = (row.get('status') or '보호중').strip()
            if status not in STATUSES:
                status = '보호중'
            if species not in ('개', '고양이', '기타'):
                species = '기타'
            db.execute('''INSERT INTO animal
                (name, species, breed, age, gender, status, rescue_date, rescue_place,
                 description, photo_emoji, created_at, status_changed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (name, species, (row.get('breed') or '').strip(),
                 (row.get('age') or '').strip(), (row.get('gender') or '').strip(),
                 status, (row.get('rescue_date') or '').strip(),
                 (row.get('rescue_place') or '').strip(),
                 (row.get('description') or '').strip(), '🐾', now, now))
            ok += 1
        db.commit()
        log_action('CSV 일괄 가져오기', 'animal', None, f'{ok}건 등록, {len(skipped)}건 제외')
        result = {'ok': ok, 'skipped': skipped}
    return render_template('admin/import.html', result=result, columns=CSV_COLUMNS)


@app.route('/admin/import/sample')
@login_required
def import_sample():
    """CSV 양식 다운로드"""
    from flask import Response
    header = ','.join(CSV_COLUMNS)
    sample = (header + '\n'
              '몽이,개,믹스,2살,수컷,보호중,2026-10-01,용인시 처인구,활발하고 사람을 좋아해요\n'
              '루나,고양이,코리안숏헤어,1살,암컷,보호중,2026-10-05,용인시 기흥구,조용하고 애교가 많아요\n')
    return Response('\ufeff' + sample, mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=animal_import_sample.csv'})


# ---------- 사이트맵 (SEO) ----------
@app.route('/sitemap.xml')
def sitemap():
    db = get_db()
    urls = ['/', '/about', '/animals', '/process', '/support', '/volunteer', '/notices', '/reviews']
    for r in db.execute("SELECT id FROM animal WHERE status != '입양완료'"):
        urls.append(f"/animals/{r['id']}")
    for r in db.execute("SELECT id FROM notice WHERE visibility='공개'"):
        urls.append(f"/notices/{r['id']}")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           + ''.join(f'  <url><loc>https://example-shelter.org{u}</loc></url>\n' for u in urls)
           + '</urlset>')
    return app.response_class(xml, mimetype='application/xml')


if __name__ == '__main__':
    with app.app_context():
        init_db()
    print("=" * 50)
    print("🐾 행복한발 보호소 데모 실행 중")
    print("   공개: http://localhost:8080")
    print("   관리자: http://localhost:8080/admin  (admin / demo1234)")
    print("=" * 50)
    app.run(host='0.0.0.0', port=8080, debug=False)
