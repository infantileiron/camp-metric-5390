"""签核快照的公开发布台账。

发布状态：
- pending 待检：已通过创建时检查，但尚未公开，不计入历史披露；
- publishing 公开中：复核已通过并提交的中间态，明细尚未落账；
- published 已公开：明细已记账，进入公开页与历史披露；
- withdrawn 已撤下：从公开页移除，但明细仍计入历史披露。

公开包一旦通过检查，其组合与合计即冻结在 check_result['items'] 中；
发布与重试只按该冻结内容记账，重试绝不重复累加。

发布在快照行锁内完成“按最新历史重新联审”，然后把状态置为公开中并提交
（第一段）；明细落账与转为已公开在第二段幂等完成。进程在任一段中断，
启动恢复或下一次发布都会重新联审并补完，且不会重复记账。公开中的冻结
组合已计入历史披露，因此并发发布不能各自基于旧历史通过检查。
"""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.models.entities import Publication, PublicationItem, Snapshot
from src.services.disclosure import evaluate_disclosure, history_fingerprint

ACTIVE_STATES = ('published', 'withdrawn')


def now():
    return datetime.now(timezone.utc).isoformat()


def _history_rows(s, snapshot_id, exclude_publication_id=None):
    """该快照历次已公开（含已撤下）的组合合计；草稿/待检不计入。

    “公开中”包已提交发布意图，其冻结组合同样计入，这样并发发布不会各自
    通过检查后在落账阶段互相撞车。草稿与待检包不计入。
    """
    rows = []
    seen = set()
    query = (s.query(PublicationItem, Publication)
             .join(Publication, Publication.id == PublicationItem.publication_id)
             .filter(PublicationItem.snapshot_id == snapshot_id,
                     Publication.state.in_(ACTIVE_STATES)))
    if exclude_publication_id is not None:
        query = query.filter(Publication.id != exclude_publication_id)
    for item, pub in query.order_by(PublicationItem.id).all():
        rows.append({
            'publication_id': pub.id,
            'label': item.label,
            'class_codes': list(item.class_codes or []),
            'combo_key': item.combo_key,
            'total_completed': item.total_completed,
            'withdrawn': pub.state == 'withdrawn',
        })
        seen.add((pub.id, item.combo_key))
    publishing = (s.query(Publication)
                  .filter(Publication.snapshot_id == snapshot_id,
                          Publication.state == 'publishing')
                  .order_by(Publication.id).all())
    for pub in publishing:
        if exclude_publication_id is not None and pub.id == exclude_publication_id:
            continue
        for item in (pub.check_result or {}).get('items') or []:
            if (pub.id, item['combo_key']) in seen:
                continue
            rows.append({
                'publication_id': pub.id,
                'label': item['label'],
                'class_codes': list(item['class_codes']),
                'combo_key': item['combo_key'],
                'total_completed': item['total_completed'],
                'withdrawn': False,
            })
    # 同一快照同一组合只作为一条披露约束。
    unique = {}
    for row in rows:
        unique.setdefault(row['combo_key'], row)
    return list(unique.values())


def history_rows(s, snapshot_id):
    return _history_rows(s, snapshot_id)


def combos_payload(combos):
    """规范化发布组合；同时接受 Pydantic 模型与 dict。"""
    result = []
    for combo in combos or []:
        label = combo.label if not isinstance(combo, dict) else combo.get('label')
        codes = combo.class_codes if not isinstance(combo, dict) else combo.get('class_codes')
        result.append({'label': label, 'class_codes': list(codes or [])})
    return result


def checked_snapshot(s, snapshot_id):
    """公开台账只接受已签核的快照；不改变原签核记录。"""
    row = s.get(Snapshot, snapshot_id)
    if not row:
        raise HTTPException(404, '试算快照不存在')
    if row.state != 'done' or row.approval != 'approved' or not row.result:
        raise HTTPException(409, '仅已签核的成功快照可以编制公开发布')
    return row


def frozen_facts(snapshot):
    try:
        facts = snapshot.result['snapshot']['facts']
    except (KeyError, TypeError):
        raise HTTPException(409, '快照缺少冻结事实，无法按班聚合')
    return facts


def _check(snapshot, combos, history):
    return evaluate_disclosure(frozen_facts(snapshot), history, combos)


def _public_items(check_result):
    return [{k: item[k] for k in ('position', 'label', 'total_completed')}
            for item in check_result.get('items', [])]


def public_payload(pub):
    """公开页面/下载：只给通过检查的组合名称与合计，隐藏班级明细和范围。"""
    return {
        'publication_id': pub.id,
        'snapshot_id': pub.snapshot_id,
        'published_at': pub.published_at,
        'revision': pub.revision,
        'items': _public_items(pub.check_result or {}),
    }


def ledger_row(pub):
    result = pub.check_result or {}
    return {
        'id': pub.id,
        'snapshot_id': pub.snapshot_id,
        'state': pub.state,
        'revision': pub.revision,
        'created': pub.created,
        'created_by': pub.created_by,
        'checked_at': pub.checked_at,
        'checked_by': pub.checked_by,
        'published_at': pub.published_at,
        'published_by': pub.published_by,
        'withdrawn_at': pub.withdrawn_at,
        'withdrawn_by': pub.withdrawn_by,
        'idempotency_key': pub.idempotency_key,
        'passed': bool(result.get('passed')),
        'reason': result.get('reason', ''),
        'items': _public_items(result),
    }


def detail_payload(pub):
    """管理页：包含按班聚合、整数可行范围和泄露组合；仍不改变签核。"""
    row = ledger_row(pub)
    row['check_result'] = pub.check_result or {}
    row['combos'] = combos_payload(pub.combos or [])
    row['check_history_fingerprint'] = pub.check_history_fingerprint
    row['check_package_fingerprint'] = pub.check_package_fingerprint
    return row


def precheck(s, snapshot, combos):
    """预检：按当前历史披露计算范围并给出拒发原因，不落账。"""
    return _check(snapshot, combos, _history_rows(s, snapshot.id))


def create(s, snapshot, combos, user, idempotency_key=None):
    """创建待检发布；检查不通过则整包拒发且不落账。"""
    if idempotency_key:
        existing = (s.query(Publication)
                    .filter_by(snapshot_id=snapshot.id, idempotency_key=idempotency_key)
                    .first())
        if existing:
            stored = combos_payload(existing.combos or [])
            wanted = combos_payload(combos)
            if [(c['label'], sorted(c['class_codes'])) for c in stored] != \
                    [(c['label'], sorted(c['class_codes'])) for c in wanted]:
                raise HTTPException(409, '同一幂等键已用于不同的发布内容')
            return existing, False
    history = _history_rows(s, snapshot.id)
    result = _check(snapshot, combos, history)
    if not result['passed']:
        raise HTTPException(409, result['reason'])
    pub = Publication(
        snapshot_id=snapshot.id,
        state='pending',
        combos=combos_payload(combos),
        check_result=result,
        check_history_fingerprint=result['history_fingerprint'],
        check_package_fingerprint=result['package_fingerprint'],
        checked_at=now(),
        checked_by=user.email,
        created=now(),
        created_by=user.email,
        idempotency_key=idempotency_key,
        revision=1,
    )
    s.add(pub)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        # 并发重试同一幂等键：回读已落账的待检包，按幂等语义返回。
        existing = (s.query(Publication)
                    .filter_by(snapshot_id=snapshot.id, idempotency_key=idempotency_key)
                    .first()) if idempotency_key else None
        if existing:
            stored = combos_payload(existing.combos or [])
            wanted = combos_payload(combos)
            if [(c['label'], sorted(c['class_codes'])) for c in stored] == \
                    [(c['label'], sorted(c['class_codes'])) for c in wanted]:
                return existing, False
        raise HTTPException(409, '发布待检创建冲突，请刷新后重试')
    return pub, True


def _lock_snapshot(s, snapshot_id):
    row = s.query(Snapshot).filter(Snapshot.id == snapshot_id).with_for_update().first()
    if not row:
        raise HTTPException(404, '试算快照不存在')
    return row


def _lock_publication(s, pub_id):
    row = s.query(Publication).filter(Publication.id == pub_id).with_for_update().first()
    if not row:
        raise HTTPException(404, '发布台账不存在')
    return row


def _record_recheck(s, pub, result, user):
    pub.check_result = result
    pub.check_history_fingerprint = result['history_fingerprint']
    pub.check_package_fingerprint = result['package_fingerprint']
    pub.checked_at = now()
    pub.checked_by = user.email
    pub.revision = (pub.revision or 1) + 1


def _recheck(s, pub, user):
    """按除本包以外的历史重新联审，返回检查结果。

    检查后新增的公开合计、并发发布或中断期间他人落账都会改变历史指纹，
    从而使旧检查失效。每个发布都会在锁内重新计算，不使用缓存结论。
    """
    history = _history_rows(s, pub.snapshot_id, exclude_publication_id=pub.id)
    snapshot = checked_snapshot(s, pub.snapshot_id)
    result = _check(snapshot, combos_payload(pub.combos or []), history)
    stale = (history_fingerprint(history) != pub.check_history_fingerprint
             or result['package_fingerprint'] != pub.check_package_fingerprint)
    if stale:
        _record_recheck(s, pub, result, user)
    return result, stale


def _record_items(s, pub):
    """把公开包的冻结组合幂等落账，重复调用不会重复记账。"""
    from sqlalchemy.exc import IntegrityError

    result = pub.check_result or {}
    items = result.get('items') or []
    if not items:
        raise HTTPException(409, '发布包缺少冻结的检查结果，无法记账')
    existing = {item.combo_key for item in
                s.query(PublicationItem).filter_by(publication_id=pub.id).all()}
    for item in items:
        if item['combo_key'] in existing:
            continue
        clash = (s.query(PublicationItem)
                 .filter_by(snapshot_id=pub.snapshot_id, combo_key=item['combo_key'])
                 .first())
        if clash:
            raise HTTPException(409, '相同班级组合已由其他发布记账，请重新联审')
        s.add(PublicationItem(
            publication_id=pub.id,
            snapshot_id=pub.snapshot_id,
            combo_key=item['combo_key'],
            label=item['label'],
            class_codes=list(item['class_codes']),
            total_completed=int(item['total_completed']),
            created=now(),
        ))
    try:
        s.flush()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, '相同班级组合已被并发发布记账，请重新联审')


def _finalize(s, pub, user):
    """发布/恢复的落账流程；调用方必须已持有快照行锁。

    分两段提交：
    1) 复核通过后把状态置为“公开中”并提交——此后该包的冻结合计即进入
       历史披露（见 _history_rows），其他发布不会基于旧历史通过检查；
    2) 幂等写入明细并转为“已公开”。
    若进程在 1 与 2 之间中断，恢复流程会重新联审并重做第 2 段；重复调用
    不会重复记账。
    """
    if pub.state == 'published':
        return pub, False
    if pub.state == 'withdrawn':
        raise HTTPException(409, '已撤下的发布不可重新公开，请新建待检包')
    if pub.state not in ('pending', 'publishing'):
        raise HTTPException(409, '当前状态不可发布')
    result, stale = _recheck(s, pub, user)
    if not result['passed']:
        pub.state = 'pending'
        s.commit()
        raise HTTPException(409, '重新联审未通过：' + result['reason'])
    if pub.state == 'pending':
        pub.published_at = now()
        pub.published_by = user.email
        pub.state = 'publishing'
        s.commit()
        # 第二段重新取行，避免长事务持锁；此间的并发发布会在
        # _record_items 的组合冲突检查中暴露。
        pub = _lock_publication(s, pub.id)
        if pub.state == 'published':
            return pub, False
        if pub.state != 'publishing':
            raise HTTPException(409, '发布状态已变化，请刷新后重试')
    _record_items(s, pub)
    pub.state = 'published'
    s.commit()
    return pub, True


def _recover_snapshot(s, snapshot_id, actor):
    """在独立事务中补完该快照遗留的“公开中”包，并返回处理数量。"""
    rows = (s.query(Publication)
            .filter_by(snapshot_id=snapshot_id, state='publishing')
            .order_by(Publication.id).all())
    recovered = 0
    for row in rows:
        try:
            _lock_snapshot(s, snapshot_id)
            pub = _lock_publication(s, row.id)
            if pub.state != 'publishing':
                s.rollback()
                continue
            _finalize(s, pub, actor)
            recovered += 1
        except HTTPException:
            s.rollback()
        except Exception:
            s.rollback()
            raise
    return recovered


def publish(s, pub, user):
    """待检 → 已公开，带重试幂等与中断恢复。"""
    # 先补完同一快照更早的中断包，保证按创建顺序披露；每包独立事务。
    _recover_snapshot(s, pub.snapshot_id, user)
    _lock_snapshot(s, pub.snapshot_id)
    pub = _lock_publication(s, pub.id)
    if pub.state == 'published':
        return pub, False
    if pub.state == 'withdrawn':
        raise HTTPException(409, '已撤下的发布不可重新公开，请新建待检包')
    return _finalize(s, pub, user)


def withdraw(s, pub, user):
    _lock_snapshot(s, pub.snapshot_id)
    pub = _lock_publication(s, pub.id)
    if pub.state == 'withdrawn':
        return pub
    if pub.state == 'publishing':
        pub, _ = _finalize(s, pub, user)
        pub = _lock_publication(s, pub.id)
    if pub.state != 'published':
        raise HTTPException(409, '仅已公开的发布可以撤下')
    pub.state = 'withdrawn'
    pub.withdrawn_at = now()
    pub.withdrawn_by = user.email
    s.commit()
    return pub


def recover_all(s):
    """启动恢复：补完中断在 publishing 的发布包，重新联审后再落账。"""
    import logging

    class _Actor:
        email = 'system@recovery'

    pending = (s.query(Publication).filter_by(state='publishing')
               .order_by(Publication.id).all())
    recovered = 0
    for pub in pending:
        try:
            _lock_snapshot(s, pub.snapshot_id)
            row = _lock_publication(s, pub.id)
            if row.state != 'publishing':
                s.rollback()
                continue
            actor = _Actor()
            actor.email = row.published_by or 'system@recovery'
            _finalize(s, row, actor)
            recovered += 1
        except HTTPException as exc:
            s.rollback()
            logging.warning('publication recovery deferred id=%s: %s', pub.id, exc.detail)
        except Exception:
            s.rollback()
            logging.exception('publication recovery failed id=%s', pub.id)
    return recovered


def public_list(s):
    rows = (s.query(Publication)
            .filter(Publication.state == 'published')
            .order_by(Publication.published_at.desc(), Publication.id.desc())
            .all())
    return [public_payload(row) for row in rows]
