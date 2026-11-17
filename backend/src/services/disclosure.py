"""公开发布披露检查（纯函数，便于独立验证）。

口径：
- 只使用签核快照冻结的班级日事实，按班级聚合结课人次；
- 每个班级的可行人数被限制在 [0, 实到人次] 的非负整数；
- 公开组合的合计等于组合内各班结课人次之和；
- 结合该快照历次已发布（含已撤下，不含草稿）组合合计，求每个班级的
  精确整数可行范围，不做线性松弛；
- 结课不足 5 人的班级，若范围宽度（最大值 - 最小值）小于 3，则整包拒发，
  并指出造成收窄的泄露组合。
"""

import hashlib
import json
import time

SMALL_COMPLETED = 5
MIN_WIDTH = 3
MAX_COMBOS = 50
MAX_CLASSES_PER_COMBO = 50
MAX_CODE_LENGTH = 50
MAX_COMBO_KEY_LENGTH = 900


class DisclosureError(ValueError):
    """组合结构或事实不满足检查前提。"""


class Infeasible(DisclosureError):
    """披露合计与班级人数上限矛盾，不存在整数方案。"""


def _checked_key(codes, label='组合'):
    key = combo_key(codes)
    if len(key) > MAX_COMBO_KEY_LENGTH:
        raise DisclosureError('%s的班级代码总长度超出上限：%s' % (label, key[:60]))
    return key


def combo_key(class_codes):
    """组合的稳定键：班级代码排序后连接。"""
    return ','.join(sorted(str(c) for c in class_codes))


def normalize_codes(class_codes):
    if isinstance(class_codes, str):
        class_codes = [c for c in class_codes.replace('，', ',').split(',')]
    codes = []
    for code in class_codes or []:
        code = str(code).strip()
        if not code:
            continue
        if len(code) > MAX_CODE_LENGTH:
            raise DisclosureError('班级代码过长：' + code[:MAX_CODE_LENGTH])
        if code in codes:
            raise DisclosureError('同一组合内班级代码不得重复：' + code)
        codes.append(code)
    return codes


def aggregate_facts(facts):
    """按班级聚合冻结事实，返回有序字典。

    人数上限取各班实到人次；同时保留结课人次供内部判定“小班”。
    """
    agg = {}
    for fact in facts or []:
        try:
            code = str(fact['class_code'])
            attended = int(fact['attended'])
            completed = int(fact['completed'])
        except (KeyError, TypeError, ValueError):
            raise DisclosureError('冻结事实缺少班级/实到/结课字段，无法聚合')
        if not code:
            raise DisclosureError('冻结事实存在空班级代码')
        if attended < 0 or completed < 0:
            raise DisclosureError('冻结事实存在负数人次：' + code)
        if completed > attended:
            raise DisclosureError('冻结事实结课大于实到：' + code)
        row = agg.setdefault(code, {'class_code': code, 'attended': 0, 'completed': 0})
        row['attended'] += attended
        row['completed'] += completed
    return {code: agg[code] for code in sorted(agg)}


def _merge_constraints(constraints):
    """按班级集合合并披露约束；同一集合出现不同合计视为矛盾。"""
    merged = {}
    for codes, total in constraints:
        key = frozenset(codes)
        if not key:
            raise DisclosureError('组合至少包含一个班级')
        total = int(total)
        if total < 0:
            raise DisclosureError('公开合计不能为负数')
        if key in merged and merged[key] != total:
            raise Infeasible('同一班级组合存在互相矛盾的公开合计，无法形成整数方案')
        merged[key] = total
    return sorted(merged.items(), key=lambda item: combo_key(item[0]))


MAX_NODES = 200000
MAX_PROJECTION_VARS = 64
MAX_SEARCH_SECONDS = 2.0
MAX_DIAGNOSTIC_SECONDS = 2.0
MAX_DIAGNOSTIC_ROWS = 40


def _propagate_bounds(classes, upper, constraints):
    """用各组合合计对班级上下界做区间传播，返回 (lb, ub)。

    只使用可靠的不等式推导，不会把不可行系统误判为可行。
    """
    lb = {c: 0 for c in classes}
    ub = {c: int(upper[c]) for c in classes}
    for _ in range(len(classes) + 2):
        changed = False
        for members, total in constraints:
            total = int(total)
            span_ub = sum(ub[c] for c in members)
            span_lb = sum(lb[c] for c in members)
            if total > span_ub:
                raise Infeasible('公开合计超过组合内班级实到上限，无法形成整数方案')
            if total < span_lb:
                raise Infeasible('公开合计低于组合内班级已确定下限，无法形成整数方案')
            for c in members:
                low = total - (span_ub - ub[c])
                high = total - (span_lb - lb[c])
                if low > lb[c]:
                    lb[c] = low
                    changed = True
                if high < ub[c]:
                    ub[c] = high
                    changed = True
                if lb[c] > ub[c]:
                    raise Infeasible('公开合计与班级实到上限矛盾，无法形成整数方案')
        if not changed:
            break
    return lb, ub


def _gcd(a, b):
    a, b = abs(a), abs(b)
    while b:
        a, b = b, a % b
    return a


def _rref(rows, totals, columns):
    """有理数高斯消元；返回 (矩阵, 主元列列表, 秩)。"""
    from fractions import Fraction
    matrix = [[Fraction(v) for v in row] + [Fraction(t)] for row, t in zip(rows, totals)]
    m = len(matrix)
    rank = 0
    pivots = []
    for col in columns:
        pivot_row = None
        for i in range(rank, m):
            if matrix[i][col] != 0:
                pivot_row = i
                break
        if pivot_row is None:
            continue
        matrix[rank], matrix[pivot_row] = matrix[pivot_row], matrix[rank]
        factor = matrix[rank][col]
        matrix[rank] = [v / factor for v in matrix[rank]]
        for i in range(m):
            if i != rank and matrix[i][col] != 0:
                factor = matrix[i][col]
                matrix[i] = [a - factor * b for a, b in zip(matrix[i], matrix[rank])]
        pivots.append(col)
        rank += 1
        if rank == m:
            break
    return matrix, pivots, rank


def _projection_ranges(upper, constraints, hint=None):
    """精确整数投影范围。

    变量 x_i 为非负整数且 0 <= x_i <= upper[i]；每个约束为
    sum(x_i for i in members) == total。先合并同构班级，再用有理数消元
    得到以自由变量表示的主元方程；随后用“区间传播 + 约束传播”的深度
    优先搜索，逐个锁定变量：对一个变量加 x_i <= k 后若仍有整数解，
    则 k 不小于其最小值。这样得到的是精确边界，不是线性松弛。

    搜索只为判定可行性并求边界，不枚举全部解；节点预算超限时整包拒发，
    宁可拒绝也不放宽泄露底线。
    """
    if not constraints:
        return {}
    classes = sorted({c for members, _ in constraints for c in members})
    for c in classes:
        if c not in upper:
            raise DisclosureError('组合包含快照中不存在的班级：' + c)
        if int(upper[c]) < 0:
            raise DisclosureError('班级人数上限不能为负数')
    if len(classes) > MAX_PROJECTION_VARS:
        raise DisclosureError('组合覆盖的班级数量超出可精确核验范围，整包拒发')
    merged = {}
    for members, total in constraints:
        key = frozenset(members)
        total = int(total)
        if key in merged and merged[key] != total:
            raise Infeasible('同一班级组合存在互相矛盾的公开合计，无法形成整数方案')
        merged[key] = total
    ordered = sorted(merged.items(), key=lambda item: combo_key(item[0]))
    # 同构班级：在全部约束中的成员身份完全相同，合并为一个组变量。
    signature = {}
    group_of = {}
    group_members = []
    for c in classes:
        key = tuple(1 if c in members else 0 for members, _ in ordered)
        if key not in signature:
            signature[key] = len(group_members)
            group_members.append([])
        gi = signature[key]
        group_members[gi].append(c)
        group_of[c] = gi
    group_upper = {gi: sum(int(upper[c]) for c in members)
                   for gi, members in enumerate(group_members)}
    group_hint = {}
    if hint:
        # 真实结课人次是一组已知可行整数解，用来引导搜索先找到可行解，
        # 再向外收敛边界；它只影响速度，不影响精确性。
        for gi, members in enumerate(group_members):
            if all(c in hint for c in members):
                group_hint[gi] = sum(int(hint[c]) for c in members)
    group_constraints = []
    for members, total in ordered:
        group_constraints.append((sorted({group_of[c] for c in members}), total))
    # 每个组变量内部是同构的一次变量，组总量可自由拆分到成员之间。
    n = len(group_members)
    base_lb, base_ub = _propagate_bounds(list(range(n)), group_upper, group_constraints)
    rows = []
    totals = []
    for members, total in group_constraints:
        row = [0] * n
        for gi in members:
            row[gi] = 1
        rows.append(row)
        totals.append(total)
    if n == 1:
        return _spread_groups(group_members, [base_lb[0]], [base_ub[0]], upper, classes)

    deadline = [time.monotonic() + MAX_SEARCH_SECONDS]

    def feasible(extra_lb, extra_ub):
        """在额外边界下做整数可行性判定；成功返回一组可行组总量。"""
        bound_lb = {i: max(base_lb[i], extra_lb.get(i, base_lb[i])) for i in range(n)}
        bound_ub = {i: min(base_ub[i], extra_ub.get(i, base_ub[i])) for i in range(n)}
        if any(bound_lb[i] > bound_ub[i] for i in range(n)):
            return None
        reduced = []
        for members, total in group_constraints:
            reduced.append((members, total))
        try:
            lb, ub = _propagate_bounds(list(range(n)), bound_ub, reduced)
        except Infeasible:
            return None
        for i in range(n):
            lb[i] = max(lb[i], bound_lb[i])
            if lb[i] > ub[i]:
                return None
        # 主元方程：用自由变量表示被约束变量，检查同余并搜索首解。
        order = sorted(range(n), key=lambda i: (-ub[i], i))
        matrix, pivots, rank = _rref(rows, totals, order)
        for i in range(rank, len(matrix)):
            if matrix[i][n] != 0:
                return None
        pivot_set = set(pivots)
        free = [i for i in range(n) if i not in pivot_set]
        equations = []
        for p, col in enumerate(pivots):
            row = matrix[p]
            coeffs = [(i, -row[i]) for i in free if row[i] != 0]
            constant = row[n]
            denom = constant.denominator
            for _, value in coeffs:
                denom = denom * value.denominator // _gcd(denom, value.denominator)
            equations.append({
                'col': col, 'denom': denom, 'const': int(constant * denom),
                'coeffs': [(i, int(value * denom)) for i, value in coeffs],
            })
        nodes = [0]
        solution = [None]

        def propagate(partial):
            """已定变量之外，重新传播剩余组合合计的边界。"""
            current_lb = dict(lb)
            current_ub = dict(ub)
            if not partial:
                return current_lb, current_ub
            remaining = [i for i in range(n) if i not in partial]
            remaining_upper = {i: ub[i] for i in remaining}
            reduced = []
            for members, total in group_constraints:
                residual = int(total) - sum(partial.get(i, 0) for i in members)
                rest = [i for i in members if i not in partial]
                if not rest:
                    if residual != 0:
                        return None
                    continue
                reduced.append((rest, residual))
            if not reduced:
                return current_lb, current_ub
            try:
                sub_lb, sub_ub = _propagate_bounds(remaining, remaining_upper, reduced)
            except Infeasible:
                return None
            for i in remaining:
                current_lb[i] = max(current_lb[i], sub_lb[i])
                current_ub[i] = min(current_ub[i], sub_ub[i])
                if current_lb[i] > current_ub[i]:
                    return None
            return current_lb, current_ub

        def equation_ok(eq, partial, current_lb, current_ub):
            """主元方程的必要条件；不满足则当前部分赋值不可能有整数解。

            x_col = (const + Σ coef_j x_j) / denom 必须是整数并落在 x_col
            的当前区间内。未定变量只按区间与同余做必要剪枝，不做任何会
            放宽结论的近似。
            """
            assigned = sum(coef * partial[j] for j, coef in eq['coeffs'] if j in partial)
            constant = eq['const'] + assigned
            low = high = constant
            g = eq['denom']
            has_free = False
            for j, coef in eq['coeffs']:
                if j in partial:
                    continue
                has_free = True
                low += min(coef * current_lb[j], coef * current_ub[j])
                high += max(coef * current_lb[j], coef * current_ub[j])
                g = _gcd(g, coef)
            need_low = eq['denom'] * current_lb[eq['col']]
            need_high = eq['denom'] * current_ub[eq['col']]
            n_lo = max(low, need_low)
            n_hi = min(high, need_high)
            if n_lo > n_hi:
                return False
            if not has_free:
                if constant % eq['denom']:
                    return False
                value = constant // eq['denom']
                return current_lb[eq['col']] <= value <= current_ub[eq['col']]
            # 需要 N ≡ 0 (mod denom) 且 N ≡ constant (mod g) 的整数 N
            # 落在 [n_lo, n_hi]；先做整除必要性，再按 CRT 求余类。
            from math import gcd as _g
            step = _g(eq['denom'], g)
            if constant % step:
                return False
            denom_part = eq['denom'] // step
            g_part = g // step
            if g_part == 1:
                period = eq['denom']
                residue = 0
            else:
                multiplier = (constant // step) * pow(denom_part % g_part, -1, g_part)
                multiplier %= g_part
                period = eq['denom'] * g_part
                residue = (eq['denom'] * multiplier) % period
            first = n_lo + ((residue - n_lo) % period)
            return first <= n_hi

        def resolve(partial, current_lb, current_ub):
            """自由变量确定后解出主元变量，并校验整数与区间。"""
            values = dict(partial)
            for eq in equations:
                numerator = eq['const'] + sum(coef * values[j] for j, coef in eq['coeffs'])
                if numerator % eq['denom']:
                    return None
                value = numerator // eq['denom']
                if value < current_lb[eq['col']] or value > current_ub[eq['col']]:
                    return None
                values[eq['col']] = value
            return values

        def search(partial):
            nodes[0] += 1
            if nodes[0] > MAX_NODES or (nodes[0] % 512 == 0 and time.monotonic() > deadline[0]):
                raise DisclosureError('披露组合约束规模超出可精确核验范围，整包拒发')
            propagated = propagate(partial)
            if propagated is None:
                return False
            current_lb, current_ub = propagated
            for eq in equations:
                if eq['col'] in partial:
                    continue
                if not equation_ok(eq, partial, current_lb, current_ub):
                    return False
            if len(partial) == len(free):
                values = resolve(partial, current_lb, current_ub)
                if values is None:
                    return False
                solution[0] = values
                return True
            # 只在自由变量上分支；主元变量由方程唯一确定，分支它们只会
            # 放大搜索树。优先选传播后区间最窄的自由变量，先试中点。
            candidates = [(current_ub[i] - current_lb[i], i) for i in free if i not in partial]
            _, pick = min(candidates)
            low, high = current_lb[pick], current_ub[pick]
            middle = group_hint.get(pick)
            if middle is None or not low <= middle <= high:
                middle = (low + high) // 2
            ordered_values = [middle]
            for offset in range(1, max(middle - low, high - middle) + 1):
                if middle - offset >= low:
                    ordered_values.append(middle - offset)
                if middle + offset <= high:
                    ordered_values.append(middle + offset)
            for value in ordered_values:
                partial[pick] = value
                if search(partial):
                    del partial[pick]
                    return True
            del partial[pick]
            return False

        if not search({}):
            return None
        return solution[0]

    ranges = {}
    if feasible({}, {}) is None:
        raise Infeasible('公开合计与班级实到上限矛盾，无法形成整数方案')
    for i in range(n):
        if time.monotonic() > deadline[0]:
            raise DisclosureError('披露组合约束规模超出可精确核验范围，整包拒发')
        # 最小值：在 x_i <= k 下可行则 k 不小于最小值，二分最小的可行上界。
        value_low, value_high = base_lb[i], base_ub[i]
        minimum = value_high
        lo, hi = value_low, value_high
        while lo <= hi:
            mid = (lo + hi) // 2
            probe = feasible({}, {i: mid})
            if probe is not None:
                minimum = mid
                hi = mid - 1
            else:
                lo = mid + 1
        maximum = value_low
        lo, hi = value_low, value_high
        while lo <= hi:
            mid = (lo + hi) // 2
            probe = feasible({i: mid}, {})
            if probe is not None:
                maximum = mid
                lo = mid + 1
            else:
                hi = mid - 1
        ranges[i] = {'min': minimum, 'max': maximum}
    return _spread_groups(group_members, [ranges[i]['min'] for i in range(n)],
                         [ranges[i]['max'] for i in range(n)], upper, classes)


def _spread_groups(group_members, group_min, group_max, upper, classes):
    """把组变量的范围还原为组内每个班级的精确范围。"""
    result = {}
    for gi, members in enumerate(group_members):
        span = sum(int(upper[c]) for c in members)
        for c in members:
            low = max(0, group_min[gi] - (span - int(upper[c])))
            high = min(int(upper[c]), group_max[gi])
            if low > high:
                raise Infeasible('公开合计与班级实到上限矛盾，无法形成整数方案')
            result[c] = {'min': low, 'max': high}
    return {c: result[c] for c in classes}


def integer_ranges(upper, constraints, hint=None):
    """返回所有班级的可行范围：有约束的精确投影，未覆盖的为 [0, 上限]。"""
    merged = _merge_constraints(constraints)
    projected = _projection_ranges(upper, merged, hint)
    result = {}
    for code in sorted(upper):
        if code in projected:
            result[code] = projected[code]
        else:
            result[code] = {'min': 0, 'max': int(upper[code])}
    return result


def width_of(rng):
    return int(rng['max']) - int(rng['min'])


def history_fingerprint(history):
    """已发布合计集合指纹（撤下仍计入，草稿不进入该列表）。"""
    rows = {}
    for row in history or []:
        rows[combo_key(row['class_codes'])] = int(row['total_completed'])
    payload = [[key, rows[key]] for key in sorted(rows)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def package_fingerprint(items):
    """当前包公开合计指纹；检查后包内容变化必须重新联审。"""
    rows = sorted([[combo_key(item['class_codes']), int(item['total_completed'])] for item in items or []])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _combos_with_totals(classes, combos):
    """校验并计算当前包的组合合计；合计由服务端按冻结事实计算。"""
    if not combos:
        raise DisclosureError('至少需要一个公开组合')
    if len(combos) > MAX_COMBOS:
        raise DisclosureError('单次发布组合数量不能超过 %d' % MAX_COMBOS)
    seen = set()
    items = []
    for order, combo in enumerate(combos):
        label = str(combo.get('label') or '').strip()
        if not label:
            raise DisclosureError('公开组合必须填写名称')
        codes = normalize_codes(combo.get('class_codes'))
        if len(codes) < 2:
            raise DisclosureError('公开组合至少包含两个班级：' + label)
        if len(codes) > MAX_CLASSES_PER_COMBO:
            raise DisclosureError('单个组合班级数量不能超过 %d' % MAX_CLASSES_PER_COMBO)
        for code in codes:
            if code not in classes:
                raise DisclosureError('组合 %s 包含快照中不存在的班级：%s' % (label, code))
        key = _checked_key(codes, '组合 ' + label + ' ')
        if key in seen:
            raise DisclosureError('同一发布包内存在重复组合：' + label)
        seen.add(key)
        items.append({
            'position': order,
            'label': label,
            'class_codes': codes,
            'combo_key': key,
            'total_completed': sum(classes[c]['completed'] for c in codes),
        })
    return items


def _history_rows(history):
    rows = []
    seen = set()
    for row in history or []:
        codes = normalize_codes(row.get('class_codes'))
        if not codes:
            raise DisclosureError('历史披露存在空组合')
        for code in codes:
            if len(code) > MAX_CODE_LENGTH:
                raise DisclosureError('历史披露存在过长班级代码')
        key = _checked_key(codes, '历史披露 ')
        total = int(row.get('total_completed'))
        if total < 0:
            raise DisclosureError('历史披露合计不能为负数：' + key)
        if key in seen:
            # 账本按 (快照, 组合键) 唯一，正常不会重复；重复时按同一约束处理。
            continue
        seen.add(key)
        rows.append({
            'publication_id': row.get('publication_id'),
            'label': row.get('label') or '',
            'class_codes': codes,
            'combo_key': key,
            'total_completed': total,
            'withdrawn': bool(row.get('withdrawn')),
        })
    return rows


def _describe(unsafe):
    parts = []
    for entry in unsafe:
        names = '、'.join(item['label'] + '(' + ','.join(item['class_codes']) + ')' for item in entry['combinations'])
        parts.append('%s 结课%d人(<%d)，可行范围 [%d,%d] 宽度%d(<%d)%s' % (
            entry['class_code'], entry['completed'], SMALL_COMPLETED,
            entry['range']['min'], entry['range']['max'], entry['width'], MIN_WIDTH,
            ('；泄露组合：' + names) if names else ''
        ))
    return '整包拒发：' + '；'.join(parts)


def evaluate_disclosure(facts, history, combos):
    """执行一次披露检查。

    返回的 dict 可直接作为检查快照保存；不接触数据库。
    结构或事实错误以 passed=False 返回，避免把可解释的业务拒绝变成 500。
    """
    try:
        classes = aggregate_facts(facts)
        items = _combos_with_totals(classes, combos)
        hist = _history_rows(history)
        disclosed = {row['combo_key'] for row in hist}
        duplicated = [item['label'] for item in items if item['combo_key'] in disclosed]
        if duplicated:
            raise DisclosureError('以下组合已由历史披露，重复发布会让公开合计重复出现：' +
                                  '、'.join(duplicated))
        upper = {code: row['attended'] for code, row in classes.items()}
        for row in hist:
            for code in row['class_codes']:
                if code not in upper:
                    raise DisclosureError('历史披露包含当前快照中不存在的班级：' + code)
        all_constraints = [(row['class_codes'], row['total_completed']) for row in hist]
        all_constraints += [(item['class_codes'], item['total_completed']) for item in items]
        # 冻结事实里的结课人次天然是一组可行整数解，作为搜索提示。
        hint = {code: row['completed'] for code, row in classes.items()}
        ranges = integer_ranges(upper, all_constraints, hint)
        covered = {code for row in hist for code in row['class_codes']}
        covered.update(code for item in items for code in item['class_codes'])
        class_rows = []
        unsafe = []
        for code, row in classes.items():
            rng = ranges[code]
            width = width_of(rng)
            small = row['completed'] < SMALL_COMPLETED
            is_covered = code in covered
            class_rows.append({
                'class_code': code,
                'attended': row['attended'],
                'completed': row['completed'],
                'covered': is_covered,
                'small': small,
                'range': rng,
                'width': width,
                'possible_values': width + 1,
            })
            # 结课不足 5 人的班级，只要其可行范围被公开合计收窄到宽度小于 3
            # 就整包拒发。未被任何公开组合覆盖的班级范围仍是 [0, 实到]，
            # 既不来自披露、也无泄露组合可指认，因此不计入拒发。
            if small and is_covered and width < MIN_WIDTH:
                unsafe.append({
                    'class_code': code,
                    'completed': row['completed'],
                    'attended': row['attended'],
                    'range': rng,
                    'width': width,
                    'possible_values': width + 1,
                    'covered': is_covered,
                })
        # 诊断泄露组合：逐个去掉一条披露合计，看该班范围是否恢复到阈值
        # 以上；能恢复的即为泄露所必需。相减型泄露要同时指出含该班的组合
        # 和不含该班的对照组合，因此遍历全部历史与本次组合，而不是只看
        # 包含该班的组合。
        for entry in unsafe:
            code = entry['class_code']
            all_rows = ([{'source': 'history', 'publication_id': row['publication_id'],
                          'label': row['label'], 'class_codes': row['class_codes'],
                          'total_completed': row['total_completed'],
                          'withdrawn': row['withdrawn'],
                          'combo_key': row['combo_key']} for row in hist] +
                        [{'source': 'current', 'label': item['label'],
                          'class_codes': item['class_codes'],
                          'total_completed': item['total_completed'],
                          'combo_key': item['combo_key']} for item in items])
            leaks = []
            diagnostic_deadline = time.monotonic() + MAX_DIAGNOSTIC_SECONDS
            for candidate in all_rows[:MAX_DIAGNOSTIC_ROWS]:
                if time.monotonic() > diagnostic_deadline:
                    break
                remaining = [(row['class_codes'], row['total_completed'])
                             for row in hist if row['combo_key'] != candidate['combo_key']]
                remaining += [(item['class_codes'], item['total_completed'])
                              for item in items if item['combo_key'] != candidate['combo_key']]
                try:
                    remaining_ranges = integer_ranges(upper, remaining, hint)
                except Infeasible:
                    # 去掉后其余合计互相矛盾，说明该组合参与了整体约束。
                    leaks.append(dict(candidate))
                    continue
                except DisclosureError:
                    # 诊断本身超出预算时不追加结论，保持已有拒发原因。
                    continue
                if width_of(remaining_ranges[code]) >= MIN_WIDTH:
                    leaks.append(dict(candidate))
            if not leaks:
                # 无法归因到单条合计时，至少列出包含该班的披露组合。
                leaks = [dict(row) for row in all_rows if code in row['class_codes']]
            entry['historical'] = any(row['source'] == 'history' for row in leaks)
            entry['combinations'] = leaks
        passed = not unsafe
        return {
            'passed': passed,
            'reason': '' if passed else _describe(unsafe),
            'classes': class_rows,
            'ranges': [{
                'class_code': row['class_code'],
                'min': row['range']['min'],
                'max': row['range']['max'],
                'width': row['width'],
                'possible_values': row['possible_values'],
                'completed': row['completed'],
                'attended': row['attended'],
                'covered': row['covered'],
                'small': row['small'],
            } for row in class_rows],
            'leaking': unsafe,
            'items': items,
            'history': hist,
            'history_fingerprint': history_fingerprint(hist),
            'package_fingerprint': package_fingerprint(items),
            'thresholds': {'small_completed': SMALL_COMPLETED, 'min_width': MIN_WIDTH},
        }
    except Infeasible as exc:
        return {
            'passed': False,
            'reason': '整包拒发：' + str(exc),
            'classes': [],
            'ranges': [],
            'leaking': [],
            'items': [],
            'history': [],
            'history_fingerprint': history_fingerprint([]),
            'package_fingerprint': package_fingerprint([]),
            'thresholds': {'small_completed': SMALL_COMPLETED, 'min_width': MIN_WIDTH},
            'error': str(exc),
        }
    except DisclosureError as exc:
        return {
            'passed': False,
            'reason': '整包拒发：' + str(exc),
            'classes': [],
            'ranges': [],
            'leaking': [],
            'items': [],
            'history': [],
            'history_fingerprint': history_fingerprint([]),
            'package_fingerprint': package_fingerprint([]),
            'thresholds': {'small_completed': SMALL_COMPLETED, 'min_width': MIN_WIDTH},
            'error': str(exc),
        }
