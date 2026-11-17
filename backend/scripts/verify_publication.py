"""公开发布台账回归测试（sqlite 内存库，无需外部服务）。

用法（backend 目录）：
    python -m scripts.verify_publication
"""

import json
import os
import sys
import tempfile

if 'DATABASE_URL' not in os.environ:
    os.environ['DATABASE_URL'] = 'sqlite+pysqlite://'
os.environ.setdefault('JWT_SECRET', 'verify-secret')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.config.database as cfg  # noqa: E402
from src.config.database import Base  # noqa: E402
from src.models import entities as E  # noqa: E402
from src.services import publication as P  # noqa: E402
from src.utils.security import hash_password  # noqa: E402

PASSWORD = 'Demo123!'


def build():
    engine = create_engine(os.environ['DATABASE_URL'],
                           connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    cfg.engine = engine
    cfg.Session = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    s = cfg.Session()
    for role in ('admin', 'analyst', 'viewer'):
        s.add(E.User(email=role + '@demo.local', password=hash_password(PASSWORD), role=role))
    # QH-03 结课只有 3 人，属于小班；实到上限很大，单独一个合计无法定位它。
    spec = {'QH-01': (120, 40), 'QH-02': (140, 52), 'QH-03': (130, 3), 'QH-04': (110, 44)}
    facts = [{'class_code': code, 'day': '2026-11-01', 'mode': '线上',
              'enrolled': attended, 'attended': attended,
              'completed': completed, 'refunds': 0}
             for code, (attended, completed) in spec.items()]
    metric = E.Metric(code='completion', version=1, name='结课达成率',
                      numerator='completed', denominator='attended',
                      numerator_mode='全部', denominator_mode='全部')
    s.add(metric)
    s.commit()
    snapshot = E.Snapshot(metric_id=metric.id, date_from='2026-10-17', date_to='2026-11-15',
                          state='done', approval='approved', approved_by='admin@demo.local',
                          note='自检快照',
                          result={'value': 0.3, 'metric': {}, 'snapshot': {'facts': facts}})
    s.add(snapshot)
    s.commit()
    return s, snapshot.id


def main():
    s, sid = build()
    from src.main import app
    client = TestClient(app)

    def login(email):
        response = client.post('/auth/login', json={'email': email, 'password': PASSWORD})
        assert response.status_code == 200, response.text
        return {'Authorization': 'Bearer ' + response.json()['token']}

    admin, analyst, viewer = (login(r + '@demo.local') for r in ('admin', 'analyst', 'viewer'))

    def create(combos, key):
        return client.post('/snapshots/%d/publications' % sid,
                           json={'combos': combos, 'idempotency_key': key}, headers=admin)

    def publish(pid, key=None):
        return client.post('/publications/%d/publish' % pid,
                           json={'idempotency_key': key} if key else None, headers=admin)

    # 预检不落账，仅 admin 可用；单独一个全体合计不会定位到任何单班。
    everybody = [{'label': '四班合计', 'class_codes': ['QH-01', 'QH-02', 'QH-03', 'QH-04']}]
    assert client.post('/snapshots/%d/publications/precheck' % sid,
                       json={'combos': everybody}, headers=admin).json()['passed']
    assert s.query(E.Publication).count() == 0
    assert client.post('/snapshots/%d/publications/precheck' % sid,
                       json={'combos': everybody}, headers=analyst).status_code == 403

    # 两个待检包在各自创建时都合法；先后发布会互相削弱，所以发布时要重新联审。
    later = create(everybody, 'k1')
    assert later.status_code == 200 and later.json()['state'] == 'pending'
    later_id = later.json()['id']
    assert create(everybody, 'k1').json()['id'] == later_id  # 幂等
    subset = create([{'label': '一二四', 'class_codes': ['QH-01', 'QH-02', 'QH-04']}], 'k2')
    assert subset.status_code == 200
    subset_id = subset.json()['id']
    assert client.get('/publications/public', headers=viewer).json() == []  # 待检不公开

    assert publish(subset_id, 'k2').json()['state'] == 'published'
    refused = publish(later_id, 'k1')
    assert refused.status_code == 409 and '重新联审未通过' in refused.json()['detail']
    detail = client.get('/publications/%d' % later_id, headers=admin).json()
    assert detail['state'] == 'pending' and detail['revision'] == 2 and not detail['passed']
    leaking = detail['check_result']['leaking']
    assert leaking and leaking[0]['class_code'] == 'QH-03'
    assert any(row['source'] == 'history' for row in leaking[0]['combinations'])

    # 公开页只给组合名称与合计，隐藏班级明细、范围与诊断。
    public = client.get('/publications/public', headers=viewer).json()
    assert public and public[0]['items'][0]['total_completed'] == 136
    blob = json.dumps(public, ensure_ascii=False)
    assert 'QH-0' not in blob and 'class_codes' not in blob and 'ranges' not in blob
    assert publish(subset_id, 'k2').json()['state'] == 'published'  # 重试幂等
    assert s.query(E.PublicationItem).filter_by(snapshot_id=sid).count() == 1

    # 检查后新增公开合计：同一全体合计再次申请会被历史披露直接拒绝。
    assert create(everybody, 'k3').status_code == 409

    # 撤下后公开页为空，但历史披露仍计入，泄露判定不变，也不能再下载。
    assert client.post('/publications/%d/withdraw' % subset_id, headers=admin).status_code == 200
    assert client.get('/publications/public', headers=viewer).json() == []
    still = client.post('/snapshots/%d/publications/precheck' % sid,
                        json={'combos': everybody}, headers=admin).json()
    assert not still['passed']
    assert client.get('/publications/%d/export' % subset_id, headers=viewer).status_code == 409

    # 中断恢复：遗留在“公开中”的包由恢复流程幂等补完。
    crash = create([{'label': '一二三', 'class_codes': ['QH-01', 'QH-02', 'QH-03']}], 'k4')
    crash_id = crash.json()['id']
    row = s.get(E.Publication, crash_id)
    row.state = 'publishing'
    row.published_at = P.now()
    row.published_by = 'admin@demo.local'
    s.commit()
    assert P.recover_all(s) >= 1
    s.expire_all()
    assert s.get(E.Publication, crash_id).state in ('published', 'pending')
    before = s.query(E.PublicationItem).count()
    P.recover_all(s)
    assert s.query(E.PublicationItem).count() == before

    # 原签核快照不受公开台账影响。
    s.expire_all()
    snapshot = s.get(E.Snapshot, sid)
    assert snapshot.approval == 'approved' and snapshot.state == 'done'
    print('发布台账回归测试通过：预检/待检/发布/重新联审/撤下/恢复均符合预期。')


if __name__ == '__main__':
    try:
        main()
    except AssertionError:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
