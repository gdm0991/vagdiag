#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdc_obd.py — универсальная диагностика по стандарту OBD-II.

Отдельный слой от диагностики блоков VAG. Работает на любом автомобиле
с 2001 года выпуска, потому что стандартные режимы OBD-II обязательны
по закону об выбросах. Здесь живут: коды неисправностей двигателя,
живые параметры, готовность систем самодиагностики и VIN.

В отличие от блоков кузова, двигатель отвечает с адреса «запрос плюс
восемь», а такую пару адаптер обслуживает штатно — поэтому длинные
ответы здесь читаются целиком, без ухищрений.
"""

from pdc_core import assemble_iso_tp, is_negative

ENGINE_TX = 0x7E0

# ---------------------------------------------------------------------------
# Живые параметры. Ключ — номер параметра, значение — как его разобрать.
# ---------------------------------------------------------------------------
def _u8(data, index=0):
    return data[index] if len(data) > index else 0


def _u16(data):
    return (data[0] << 8) | data[1] if len(data) >= 2 else 0


PIDS = {
    0x04: ("Нагрузка на двигатель", "%",
           lambda d: round(_u8(d) * 100 / 255, 1)),
    0x05: ("Температура охлаждающей жидкости", "°C",
           lambda d: _u8(d) - 40),
    0x0C: ("Обороты двигателя", "об/мин",
           lambda d: round(_u16(d) / 4)),
    0x0D: ("Скорость автомобиля", "км/ч",
           lambda d: _u8(d)),
    0x0F: ("Температура впускного воздуха", "°C",
           lambda d: _u8(d) - 40),
    0x10: ("Массовый расход воздуха", "г/с",
           lambda d: round(_u16(d) / 100, 2)),
    0x11: ("Положение дроссельной заслонки", "%",
           lambda d: round(_u8(d) * 100 / 255, 1)),
    0x2F: ("Уровень топлива", "%",
           lambda d: round(_u8(d) * 100 / 255, 1)),
    0x42: ("Напряжение бортсети", "В",
           lambda d: round(_u16(d) / 1000, 2)),
    0x46: ("Температура за бортом", "°C",
           lambda d: _u8(d) - 40),
    0x5C: ("Температура масла", "°C",
           lambda d: _u8(d) - 40),
    0x33: ("Атмосферное давление", "кПа",
           lambda d: _u8(d)),
    0x0B: ("Давление во впускном коллекторе", "кПа",
           lambda d: _u8(d)),
    0x1F: ("Время работы после запуска", "с",
           lambda d: _u16(d)),
}

# Системы самодиагностики: какой бит за что отвечает в ответе на запрос 0101
READINESS_BITS = [
    (2, 0x01, "Катализатор"),
    (2, 0x02, "Подогрев катализатора"),
    (2, 0x04, "Система улавливания паров топлива"),
    (2, 0x08, "Система вторичного воздуха"),
    (2, 0x20, "Кислородные датчики"),
    (2, 0x40, "Подогрев кислородных датчиков"),
    (2, 0x80, "Система рециркуляции отработавших газов"),
]

DTC_LETTERS = "PCBU"


def decode_obd_dtc(high, low):
    """
    Переводит два байта в привычный код вида P0301.

    Первые два бита старшего байта дают букву системы, следующие два —
    первую цифру, остальное — три шестнадцатеричные цифры.
    """
    letter = DTC_LETTERS[(high >> 6) & 0x03]
    digit = (high >> 4) & 0x03
    return f"{letter}{digit}{high & 0x0F:X}{low:02X}"


def _request(elm, payload_hex, read_timeout=3.0):
    """Отправляет стандартный запрос двигателю и возвращает разобранный ответ."""
    elm.set_header(ENGINE_TX)
    lines = elm.cmd(payload_hex, read_timeout=read_timeout)
    if is_negative(lines):
        return None
    _, data = assemble_iso_tp(lines)
    return data or None


def read_dtcs(elm, mode=0x03):
    """
    Читает коды неисправностей двигателя.

    Режимы: 0x03 — сохранённые, 0x07 — неподтверждённые,
    0x0A — постоянные, которые не стираются до устранения причины.
    """
    data = _request(elm, f"{mode:02X}")
    if not data or data[0] != mode + 0x40:
        return []
    body = data[1:]
    # В большинстве ответов первым идёт число кодов, но не всегда.
    # Определяем по чётности остатка: коды идут парами байт.
    if len(body) % 2 == 1:
        body = body[1:]
    codes = []
    for offset in range(0, len(body) - 1, 2):
        high, low = body[offset], body[offset + 1]
        if high == 0 and low == 0:
            continue
        codes.append(decode_obd_dtc(high, low))
    return codes


def clear_dtcs(elm):
    """Стирает коды двигателя и гасит лампу неисправности."""
    data = _request(elm, "04")
    return bool(data) and data[0] == 0x44


def read_pid(elm, pid):
    """Читает один живой параметр. Возвращает (название, значение, единица)."""
    if pid not in PIDS:
        return None
    title, unit, convert = PIDS[pid]
    data = _request(elm, f"01{pid:02X}", read_timeout=2.0)
    if not data or len(data) < 3 or data[0] != 0x41:
        return None
    try:
        value = convert(data[2:])
    except (IndexError, ZeroDivisionError, TypeError):
        return None
    return title, value, unit


def supported_pids(elm):
    """
    Спрашивает у двигателя, какие параметры он вообще отдаёт.

    Ответ на запрос 0100 — битовая маска: каждый бит означает поддержку
    одного номера параметра. Спрашивать неподдерживаемые бессмысленно,
    а на дешёвом адаптере ещё и вредно — он не любит лишних команд.
    """
    supported = set()
    for base in (0x00, 0x20, 0x40):
        data = _request(elm, f"01{base:02X}", read_timeout=2.5)
        if not data or len(data) < 6 or data[0] != 0x41:
            continue
        mask = int.from_bytes(data[2:6], "big")
        for bit in range(32):
            if mask & (1 << (31 - bit)):
                supported.add(base + bit + 1)
    return supported


def read_readiness(elm):
    """
    Готовность систем самодиагностики и состояние лампы неисправности.
    Нужно перед прохождением техосмотра: непройденные тесты его завалят.
    """
    data = _request(elm, "0101", read_timeout=2.5)
    if not data or len(data) < 6 or data[0] != 0x41:
        return None
    payload = data[2:]
    mil = bool(payload[0] & 0x80)
    count = payload[0] & 0x7F

    checks = []
    for index, bit, title in READINESS_BITS:
        supported = bool(payload[1] & bit) if index == 2 else False
        # Бит в третьем байте означает поддержку, в четвёртом — незавершённость
        supported = bool(payload[1] & bit)
        incomplete = bool(payload[2] & bit)
        if not supported:
            state = "не поддерживается"
        elif incomplete:
            state = "не завершено"
        else:
            state = "готово"
        checks.append({"title": title, "state": state})

    return {"mil": mil, "count": count, "checks": checks}


def read_vin(elm):
    """Читает VIN автомобиля стандартным запросом."""
    data = _request(elm, "0902", read_timeout=4.0)
    if not data or data[0] != 0x49:
        return ""
    body = data[3:] if len(data) > 3 else b""
    text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
    return text[-17:] if len(text) >= 17 else text


def read_freeze_frame(elm):
    """
    Стоп-кадр: значения параметров в момент возникновения ошибки.
    Помогает понять, при каких условиях появляется неисправность.
    """
    result = []
    for pid in (0x04, 0x05, 0x0C, 0x0D, 0x11):
        data = _request(elm, f"0200{pid:02X}", read_timeout=2.5)
        if not data or len(data) < 4 or data[0] != 0x42:
            continue
        title, unit, convert = PIDS[pid]
        try:
            value = convert(data[3:])
        except (IndexError, ZeroDivisionError, TypeError):
            continue
        result.append({"title": title, "value": value, "unit": unit})
    return result
