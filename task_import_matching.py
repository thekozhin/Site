"""Сопоставление импортированной неисправности с ручной заявкой в работе."""

import re
from difflib import SequenceMatcher


_SEPARATORS = re.compile(r'\s+(?:/|\|)\s+|[;\r\n]+')
_WORDS = re.compile(r'[а-яa-z0-9]+')
_SYSTEMS = {
    'электросистема', 'кузов', 'охлаждение', 'трансмиссия', 'рулевое управление',
    'тормозная система', 'двигатель', 'подвеска', 'акб тяговые',
    'отопление и климатконтроль', 'тяговый привод',
}
_HEATER_LOCATIONS = {'кабины', 'салона'}
_GENERIC = {
    'борт', 'машина', 'система', 'заявка', 'ремонт', 'неисправность', 'ошибка', 'не', 'нет',
    'работа', 'работает', 'работают', 'неработает', 'замена', 'заменить', 'диагностика',
    'проверка', 'проверить', 'устранение', 'устранить', 'требуется', 'горят', 'горит',
    'на', 'по', 'в', 'из', 'при', 'с', 'и', 'или', 'к', 'от',
    'стук', 'шум', 'течь', 'люфт', 'скрип', 'вибрация',
}


def _words(value):
    return tuple(_WORDS.findall(value.casefold().replace('ё', 'е')))


def _items(description):
    for fragment in _SEPARATORS.split(description):
        fragment = fragment.strip()
        if not fragment:
            continue
        prefix, separator, remainder = fragment.partition('.')
        defect = remainder if separator and ' '.join(_words(prefix)) in _SYSTEMS else fragment
        words = _words(defect)
        if words == ('куп',):
            yield fragment, ('кран', 'уровня', 'пола')
        elif (len(words) == 4 and words[0] in ('отопитель', 'отопители')
              and words[2] == 'и' and {words[1], words[3]} == _HEATER_LOCATIONS):
            yield fragment, ('отопитель', words[1])
            yield fragment, ('отопитель', words[3])
        elif (len(words) == 3 and words[0] == 'отопитель' and words[1] in ('печь', 'печка')
              and words[2] in _HEATER_LOCATIONS):
            yield fragment, ('отопитель', words[2])
        elif words:
            yield fragment, words


def _conflicting_positions(left, right):
    for axis in (('лев', 'прав'), ('передн', 'задн')):
        left_positions = {prefix for prefix in axis if any(word.startswith(prefix) for word in left)}
        right_positions = {prefix for prefix in axis if any(word.startswith(prefix) for word in right)}
        if left_positions and right_positions and left_positions != right_positions:
            return True
    return False


def _distinctive_words(words):
    return set(words) - _GENERIC - _SYSTEMS


def _same_defect(left, right):
    if ' '.join(left) in _SYSTEMS or ' '.join(right) in _SYSTEMS:
        return False
    if left == right:
        return bool(_distinctive_words(left))
    if _conflicting_positions(left, right):
        return False
    left_numbers = {word for word in left if word.isdigit()}
    right_numbers = {word for word in right if word.isdigit()}
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return False
    for match in SequenceMatcher(None, left, right, autojunk=False).get_matching_blocks():
        if match.a != 0 and match.b != 0:
            continue
        # «Не идёт зарядка» должно совпасть целиком, включая отрицание.
        if (match.a and left[match.a - 1] in ('не', 'нет')) or (match.b and right[match.b - 1] in ('не', 'нет')):
            continue
        if len(_distinctive_words(left[match.a:match.a + match.size])) >= 2:
            return True
    return False


def active_repairs_for_board(task_model, board):
    board = board.strip().replace('\xa0', '').replace(' ', '')
    if board.endswith('.0') and board[:-2].isdigit():
        board = board[:-2]
    board_numbers = (board, f'{board}.0') if board.isdigit() else (board,)
    return task_model.query.filter(
        task_model.board_number.in_(board_numbers), task_model.status == 'in_repair',
    )


def manual_repair_candidates(active_repairs, park, *, excluded_authors):
    return [task for task in active_repairs if task.park == park and task.id_to is None
            and task.event_type is None and task.author_open and task.author_open not in excluded_authors]


def match_imported_repair(candidates, imported_description):
    """Возвращает единственную заявку и строки с дополнительными неисправностями."""
    incoming = list(_items(imported_description))
    matches = []
    for task in candidates:
        existing = list(_items(task.description))
        covered_existing = set()
        covered_incoming = set()
        has_distinctive_match = False
        for left_index, (_, left) in enumerate(existing):
            for right_index, (_, right) in enumerate(incoming):
                same_defect = _same_defect(left, right)
                if same_defect or left == right:
                    covered_existing.add(left_index)
                    covered_incoming.add(right_index)
                    has_distinctive_match |= same_defect
        if has_distinctive_match and (len(covered_existing) == len(existing) or len(covered_incoming) == len(incoming)):
            extras = list(dict.fromkeys(fragment for index, (fragment, _) in enumerate(incoming)
                                        if index not in covered_incoming))
            matches.append((task, extras))
    if len(matches) == 1:
        return matches[0]
    return None, []
