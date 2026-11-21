"""Чтение CSV-файлов по настройкам из реестра (rules/bb_load.yaml).

Ридер ничего не исправляет в значениях: он только разбирает структуру файла
и возвращает строки как текст. Всё, что похоже на повреждение формата,
считается и возвращается в report, а не скрывается.
"""
import csv
import io
import re
from dataclasses import dataclass, field


@dataclass
class ReadResult:
    header: list
    rows: list                      # список списков строк (все значения - текст)
    lines_total: int = 0            # физических строк в файле
    lines_blank: int = 0            # пустых строк (пропущено)
    rows_bad_length: int = 0        # записей с неверным числом полей (отброшено)
    stray_quotes: int = 0           # одиночных кавычек внутри значений (удалено)
    bad_samples: list = field(default_factory=list)


def _is_blank(line: str) -> bool:
    return not line.strip().strip(";").strip()


def _read_text(path: str, encoding: str) -> str:
    with open(path, "r", encoding=encoding, newline="") as f:
        return f.read()


def read_plain(path: str, fmt: dict) -> ReadResult:
    """Обычный CSV: один разделитель, заголовок, без особенностей."""
    text = _read_text(path, fmt["encoding"])
    res = ReadResult(header=[], rows=[])
    res.lines_total = text.count("\n") + 1
    rd = csv.reader(io.StringIO(text, newline=""), delimiter=fmt["delimiter"])
    header = None
    for rec in rd:
        if header is None:
            header = [h.strip() for h in rec]
            res.header = header
            continue
        if not any(x.strip() for x in rec):
            res.lines_blank += 1
            continue
        if len(rec) != len(header):
            res.rows_bad_length += 1
            if len(res.bad_samples) < 3:
                res.bad_samples.append(f"полей {len(rec)}, ожидалось {len(header)}: {rec[:3]}")
            continue
        res.rows.append(rec)
    return res


def _unwrap(line: str) -> str:
    """Снять внешнюю обёртку записи: кавычки вокруг неё и ';' в конце."""
    s = line[:-1] if line.endswith(";") else line
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    elif s.endswith('"') and not s.startswith('"'):
        s = s[:-1]                      # потерянная открывающая кавычка обёртки
    return s.replace('""', '"')


_STRAY = re.compile(r'(?<![$\n])"(?![$\n]|$)')


def read_wrapped(path: str, fmt: dict) -> ReadResult:
    """Формат cbonds: разделитель '$', запись обёрнута в кавычки,
    в конце ';', в значениях бывают переносы строк и одиночные кавычки."""
    text = _read_text(path, fmt["encoding"])
    lines = text.replace("\r\n", "\n").split("\n")
    res = ReadResult(header=[], rows=[], lines_total=len(lines))
    delim = fmt["delimiter"]
    expected = fmt.get("expected_columns")

    def parse(record_text: str):
        cleaned, n = _STRAY.subn("", record_text)
        res.stray_quotes += n
        return next(csv.reader([cleaned], delimiter=delim, quotechar='"'))

    buf, count, need = [], 0, None
    for line in lines:
        if _is_blank(line):
            res.lines_blank += 1
            continue
        piece = _unwrap(line)
        if not res.header and need is None:
            res.header = [h.strip() for h in next(csv.reader([piece], delimiter=delim))]
            need = (expected or len(res.header)) - 1   # столько разделителей в записи
            continue
        buf.append(piece)
        count += piece.count(delim)
        if count >= need:
            rec = parse("\n".join(buf))
            if len(rec) == len(res.header):
                res.rows.append(rec)
            else:
                res.rows_bad_length += 1
                if len(res.bad_samples) < 3:
                    res.bad_samples.append(f"полей {len(rec)}, ожидалось {len(res.header)}")
            buf, count = [], 0
    if buf:                                  # хвост без завершения
        res.rows_bad_length += 1
        res.bad_samples.append("незавершённая запись в конце файла")
    return res


def read_file(path: str, fmt: dict) -> ReadResult:
    if fmt.get("record_wrapped_in_quotes"):
        return read_wrapped(path, fmt)
    return read_plain(path, fmt)
