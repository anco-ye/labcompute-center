"""Authenticated issue evidence and durable feedback; no task-control effects."""
import io
import json
import math
import re
import time
import task_health

from flask import abort, jsonify, request, send_file


def register_routes(app, db, audit):
    """Use the center's existing authentication/CSRF before_request policy."""
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS issue_feedback(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT NOT NULL,first_seen REAL NOT NULL,request_id TEXT NOT NULL,
            node TEXT NOT NULL,task TEXT NOT NULL,message TEXT NOT NULL,
            created REAL NOT NULL,issue_state TEXT NOT NULL,
            UNIQUE(report_id,first_seen,request_id))''')

    def lookup(c, report_id):
        if not re.fullmatch(r'[0-9a-f]{24}', report_id):
            abort(404)
        matches = []
        for raw in c.execute('SELECT * FROM task_issues'):
            row = dict(raw)
            try:
                data = json.loads(row['data'])
                if data.get('error_report', {}).get('report_id') != report_id:
                    continue
            except (ValueError, TypeError, AttributeError):
                continue
            row['data'] = task_health.memory_budget_guidance(data)
            matches.append(row)
        if not matches:
            abort(404)
        if len(matches) != 1:
            abort(409, '报告身份不唯一，请由中心核查。')
        return matches[0]

    def feedback_rows(c, report_id, first_seen):
        return [dict(row) for row in c.execute(
            'SELECT id,request_id,message,created,issue_state FROM issue_feedback '
            'WHERE report_id=? AND first_seen=? ORDER BY id', (report_id, first_seen))]

    def snapshot(c, report_id):
        issue = lookup(c, report_id)
        return dict(report_id=report_id, issue=issue,
                    feedback=feedback_rows(c, report_id, issue['first_seen']))

    @app.get('/api/issues/<report_id>')
    def issue_detail(report_id):
        with db() as c:
            payload = snapshot(c, report_id)
        return jsonify(payload)

    @app.get('/api/issues/<report_id>/report')
    def issue_report(report_id):
        with db() as c:
            payload = snapshot(c, report_id)
        payload['schema_version'] = 1
        payload['report'] = payload['issue']['data']['error_report']
        return send_file(io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')),
                         mimetype='application/json', as_attachment=True,
                         download_name='issue-'+report_id+'.json')

    @app.post('/api/issues/<report_id>/feedback')
    def issue_feedback(report_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400, '请提交 JSON 反馈。')
        first_seen, request_id, message = (data.get(k) for k in ('first_seen', 'request_id', 'message'))
        if (isinstance(first_seen, bool) or not isinstance(first_seen, (int, float))
                or not math.isfinite(first_seen) or first_seen <= 0):
            abort(400, '反馈必须绑定当前故障实例。')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_id):
            abort(400, '反馈请求标识无效。')
        if not isinstance(message, str) or not 1 <= len(message) <= 2000 or not message.strip() or '\x00' in message:
            abort(400, '反馈请输入 1–2000 字的有效文本。')
        with db() as c:
            # Bind validation and insertion to one snapshot even if a heartbeat
            # concurrently resolves or reopens the same task's incident.
            c.execute('BEGIN IMMEDIATE')
            issue = lookup(c, report_id)
            if first_seen != issue['first_seen']:
                abort(409, '故障实例已变化，请刷新详情后重新反馈；本次未记录。')
            previous = c.execute('SELECT id,message FROM issue_feedback WHERE report_id=? '
                                 'AND first_seen=? AND request_id=?',
                                 (report_id, first_seen, request_id)).fetchone()
            duplicate = previous is not None
            if previous and previous['message'] != message:
                abort(409, '该请求标识已用于其他反馈，请刷新后重试；本次未记录。')
            if not previous:
                c.execute('INSERT INTO issue_feedback(report_id,first_seen,request_id,node,task,message,created,issue_state) '
                          'VALUES(?,?,?,?,?,?,?,?)', (report_id, first_seen, request_id, issue['node'],
                            issue['task'], message, time.time(), issue['state']))
                audit(c, 'issue_feedback_recorded', json.dumps(dict(report_id=report_id,
                    first_seen=first_seen, request_id=request_id), ensure_ascii=False))
            feedback = next(row for row in feedback_rows(c, report_id, first_seen)
                            if row['request_id'] == request_id)
        notice = ('补充反馈已记录；问题保持已解决状态。' if issue['state'] == 'resolved'
                  else '反馈已记录，供中心处理时查看；问题仍待解决。')
        return jsonify(ok=True, recorded=True, duplicate=duplicate, feedback=feedback,
                       issue_state=issue['state'], message=notice), 200 if duplicate else 201
