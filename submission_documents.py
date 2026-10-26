"""Per-account acknowledgement through downloading the current submission document."""
import hashlib
import io
import time
from pathlib import Path
from flask import jsonify, request, send_file
import access_control


def register(app, db):
    path = Path(app.root_path) / 'static' / '任务上传文档.md'
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS submission_document_downloads (owner TEXT PRIMARY KEY, version TEXT NOT NULL, downloaded REAL NOT NULL)')

    def current():
        content = path.read_bytes()
        return content, hashlib.sha256(content).hexdigest()

    def status():
        _, version = current()
        with db() as c:
            row = c.execute('SELECT version FROM submission_document_downloads WHERE owner=?', (access_control.current_id(),)).fetchone()
        return dict(version=version, required=not row or row['version'] != version,
                    download_url='/api/submission-document/download')

    @app.get('/api/submission-document')
    def document_status():
        response = jsonify(status())
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/submission-document/download')
    def download_document():
        content, version = current()
        response = send_file(io.BytesIO(content), as_attachment=True,
                             download_name='任务上传文档.md', mimetype='text/markdown', conditional=False)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Document-Version'] = version
        # HEAD/previews do not count as a download action.
        if request.method == 'GET':
            with db() as c:
                c.execute('INSERT INTO submission_document_downloads(owner,version,downloaded) VALUES(?,?,?) '
                          'ON CONFLICT(owner) DO UPDATE SET version=excluded.version,downloaded=excluded.downloaded',
                          (access_control.current_id(), version, time.time()))
        return response

    @app.before_request
    def require_current_document():
        is_submission = request.method == 'POST' and (
            request.path in ('/api/jobs', '/api/uploads') or
            (request.path.startswith('/api/uploads/') and request.path.endswith('/content')))
        if is_submission:
            info = status()
            if info['required']:
                return jsonify(error='提交文档已更新，请先下载最新文档，再上传任务包。',
                               code='submission_document_required', **info), 428
