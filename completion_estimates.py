"""Display-only duration ranges. Never used by admission, deadlines or workers."""
import json
import math
import statistics
import threading
from pathlib import Path
from common import atomic_json


def positive(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


class CompletionEstimates:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.saved_at = 0
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.records = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.records = {}

    def apply(self, job, history, now, online=True):
        with self.lock:
            result = self._estimate(job, history, now, online)
            # A bounded display cache survives a Web reload; it cannot change jobs.
            if now-self.saved_at >= 60:
                self.records = {k:v for k,v in self.records.items() if now-v.get('seen', 0)<14*86400}
                try:
                    atomic_json(self.path, self.records)
                    self.saved_at = now
                except OSError:
                    pass  # A display cache failure must not affect computation.
            job['completion_estimate'] = result
            return result

    def _estimate(self, j, history, now, online):
        status = j.get('status')
        if status != 'running' or not online:
            return dict(state='unavailable', reason='等待计算开始或连接恢复')
        elapsed = j.get('elapsed_seconds')
        if not positive(elapsed):
            return dict(state='learning', reason='正在收集运行速度')
        p = j.get('progress', {})
        stage = p.get('stage') or j.get('stage') or '计算'
        if stage == 'installing':
            return dict(state='learning', reason='准备依赖，尚不能估计计算结束时间')
        generation = [j.get('compute_started'), j.get('node')]
        r = self.records.get(j['id'], {})
        if r.get('generation') != generation:
            r = dict(generation=generation, points=[])
        self.records[j['id']] = r
        r['seen'] = now
        if j.get('progress_fresh') is False:
            return dict(state='stale', reason='进度暂未更新，完成时间重新估算中', stage=stage)
        if r.get('stage') != stage:
            r.update(stage=stage, points=[], published=None, last_advance=None)
        history = [v for v in history if positive(v)]
        if p.get('scope', 'overall') == 'overall' and positive(p.get('total')) and p.get('completed') == p['total']:
            return dict(state='finalizing', reason='计算步骤结束，正在整理结果', stage=stage)
        done, total = p.get('completed'), p.get('total')
        overall = (j.get('progress_fresh') is True and p.get('scope', 'overall') == 'overall'
                   and positive(done) and positive(total) and done < total and elapsed >= 60)
        candidates = []
        sources = []
        if history:
            median = statistics.median(history)
            if median > elapsed:
                candidates.append((max(1, min(history)*.8-elapsed), max(history)*1.25-elapsed))
                sources.append(j.get('duration_history_source', '同主机同任务历史耗时'))
        points = r['points']
        if overall:
            signature = [total, p.get('unit')]
            if r.get('goal') != signature or (points and done < points[-1][1]):
                r.update(goal=signature, points=[], published=None)
                points = r['points']
            if not points or done > points[-1][1]:r['last_advance'] = elapsed
            if not points or elapsed-points[-1][0] >= 60:
                points.append([elapsed, done])
            points[:] = [x for x in points if elapsed-x[0] <= 7200][-121:]
            # Whole-run average is only an initial, deliberately broad estimate.
            remaining = elapsed*(total-done)/done
            candidates.append((remaining*.65, remaining*1.6))
            sources.append('累计整体进度')
            rates = [(done-x[1])/(elapsed-x[0]) for x in points
                     if 300 <= elapsed-x[0] <= 7200 and done > x[1]]
            if rates:
                candidates.append(((total-done)/max(rates)*.85, (total-done)/min(rates)*1.25))
                sources.append('当前阶段近期速度')
            # Do not keep a reassuring estimate when measured work stops advancing.
            if r.get('last_advance') is not None and elapsed-r['last_advance']>=900:
                return dict(state='learning', reason='整体进度暂未推进，完成时间重新估算中', stage=stage)
        if not candidates:
            return dict(state='learning', reason='缺少可比历史或整体进度，暂不能估计', stage=stage)
        low, high = min(x[0] for x in candidates), max(x[1] for x in candidates)
        # Ranges are conservative envelopes, not calibrated probability intervals.
        confidence = '参考范围' if history and len(history)>=3 and len(sources)>=3 else '初步范围'
        quantum = 900 if high>=21600 else 300 if high>=3600 else 60
        start = math.ceil((now+max(1, low))/quantum)*quantum
        end = math.ceil((now+high)/quantum)*quantum
        published = r.get('published')
        interval = 900 if high>=3600 else 60
        if published and published['end'] > now and now-published['updated_at'] < interval:
            result = dict(published)
        else:
            # Small movement inside an existing window needs no new timestamps.
            if (published and published['start'] > now and start>=published['start']
                    and end<=published['end']):
                result = dict(published, updated_at=now)
            else:
                result = dict(state='range', start=start, end=max(start+quantum,end), updated_at=now)
            r['published'] = dict(result)
        result.update(stage=stage, confidence=confidence, source=' + '.join(sources), history_samples=len(history))
        return result
