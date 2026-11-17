"""披露检查自检脚本（无需数据库）。

用法（backend 目录）：
    python -m scripts.verify_disclosure

内容：
1. 用穷举枚举对照精确整数范围算法（随机小规模）；
2. 复现业务规则：小班范围宽度阈值、整包拒发、泄露组合指认；
3. 校验指纹与历史披露规则（撤下计入、草稿不计入由发布层保证）。
"""

import itertools
import random
import sys

from src.services.disclosure import (
    MIN_WIDTH,
    SMALL_COMPLETED,
    Infeasible,
    evaluate_disclosure,
    integer_ranges,
)


def brute_ranges(upper, constraints):
    codes = sorted(upper)
    index = {c: i for i, c in enumerate(codes)}
    solutions = []
    for values in itertools.product(*[range(upper[c] + 1) for c in codes]):
        if all(sum(values[index[c]] for c in members) == total
               for members, total in constraints):
            solutions.append(values)
    if not solutions:
        return None
    return {c: {'min': min(s[i] for s in solutions), 'max': max(s[i] for s in solutions)}
            for i, c in enumerate(codes)}


def check_against_brute_force(trials=4000, seed=3111):
    rng = random.Random(seed)
    for _ in range(trials):
        n = rng.randint(1, 5)
        codes = ['C%d' % i for i in range(n)]
        upper = {c: rng.randint(0, 4) for c in codes}
        constraints = []
        for _ in range(rng.randint(0, 4)):
            members = rng.sample(codes, rng.randint(1, n))
            constraints.append((members, rng.randint(0, sum(upper[c] for c in members))))
        expected = brute_ranges(upper, constraints)
        try:
            actual = integer_ranges(upper, constraints)
        except Infeasible:
            assert expected is None, ('误判不可行', upper, constraints, expected)
            continue
        assert expected is not None, ('漏判不可行', upper, constraints, actual)
        for code in codes:
            assert (actual[code]['min'], actual[code]['max']) == \
                   (expected[code]['min'], expected[code]['max']), \
                   ('范围不一致', upper, constraints, code, actual[code], expected[code])


def facts(*rows):
    return [{'class_code': code, 'attended': attended, 'completed': completed}
            for code, attended, completed in rows]


def combos(*rows):
    return [{'label': label, 'class_codes': list(codes)} for label, codes in rows]


def check_business_rules():
    # 两组组合叠加才让全班合计与子组合合计相减，从而锁定小班 C。
    result = evaluate_disclosure(
        facts(('A', 10, 4), ('B', 10, 3), ('C', 10, 2)), [],
        combos(('AB', ['A', 'B']), ('全体', ['A', 'B', 'C'])))
    assert not result['passed']
    c = next(x for x in result['leaking'] if x['class_code'] == 'C')
    assert c['width'] == 0
    assert any(x['label'] == '全体' for x in c['combinations'])
    assert '泄露组合' in result['reason']

    # 单独一个全体合计不会定位到任何单个班级：各班范围仍宽，整包放行。
    result = evaluate_disclosure(
        facts(('A', 10, 4), ('B', 10, 3), ('C', 10, 2)), [],
        combos(('全体', ['A', 'B', 'C'])))
    assert result['passed'], result['reason']
    assert all(x['width'] >= MIN_WIDTH for x in result['ranges'])

    # 宽度恰好为阈值时放行：历史 AB=7 后，A 的范围宽度仍 >= 3。
    history = [{'publication_id': 1, 'label': 'AB', 'class_codes': ['A', 'B'],
                'total_completed': 7, 'withdrawn': False}]
    result = evaluate_disclosure(
        facts(('A', 10, 4), ('B', 10, 3), ('C', 10, 8)), history,
        combos(('AC', ['A', 'C'])))
    assert result['passed'], result['reason']

    # 检查后新增历史合计使小班被锁定：AC=12 结合 AB=7 得到 A=4。
    result = evaluate_disclosure(
        facts(('A', 10, 4), ('B', 10, 3), ('C', 10, 8)), history,
        combos(('AC', ['A', 'C']), ('AC+BC', ['A', 'B', 'C'])))
    c = next(x for x in result['ranges'] if x['class_code'] == 'C')
    assert c['min'] == c['max'] == 8, c

    # 撤下的历史仍计入披露。
    withdrawn = [dict(history[0], withdrawn=True)]
    result = evaluate_disclosure(
        facts(('A', 10, 4), ('B', 10, 3), ('C', 10, 2)), withdrawn,
        combos(('AC', ['A', 'C'])))
    assert any(x['class_code'] == 'A' for x in result['ranges'])

    # 结构错误整包拒发，不抛 500。
    result = evaluate_disclosure(facts(('A', 10, 4)), [], combos(('单班', ['A'])))
    assert not result['passed'] and '至少包含两个班级' in result['reason']

    # 合计与实到上限矛盾：不存在非负整数方案。
    result = evaluate_disclosure(
        facts(('A', 30, 2), ('B', 30, 2)), [], combos(('AB', ['A', 'B'])))
    assert result['passed'], result['reason']
    result = evaluate_disclosure(
        facts(('A', 3, 2), ('B', 3, 2), ('C', 3, 1)),
        [{'publication_id': 1, 'label': 'AB old', 'class_codes': ['A', 'B'],
          'total_completed': 99, 'withdrawn': False}],
        combos(('ABC new', ['A', 'B', 'C'])))
    assert not result['passed']
    assert '整数方案' in result['reason']

    # 重复组合（含历史）不得再次发布，避免合计重复出现。
    result = evaluate_disclosure(
        facts(('A', 10, 4), ('B', 10, 3), ('C', 10, 8)), history,
        combos(('AB again', ['B', 'A'])))
    assert not result['passed'] and '重复发布' in result['reason']


def main():
    check_against_brute_force()
    check_business_rules()
    print('披露检查自检通过：整数范围与穷举一致，业务规则符合“小班<%d 且宽度<%d 整包拒发”。'
          % (SMALL_COMPLETED, MIN_WIDTH))


if __name__ == '__main__':
    try:
        main()
    except AssertionError as exc:
        print('自检失败：', exc, file=sys.stderr)
        raise SystemExit(1)
