#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_smoke.py — автотесты на заглушке адаптера.

Запуск:  python tests/test_smoke.py

Зачем. При доработках легко починить одно и сломать другое, причём
незаметно: программа продолжает работать, просто выдаёт неверные данные.
Эти тесты гоняют разбор ответов и обмен с заглушкой без автомобиля
и ловят такие поломки сразу.
"""

import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "app"))

from pdc_core import (assemble_iso_tp, declared_length, describe_status,  # noqa: E402
                      failure_text, format_dtc, is_truncated,
                      partial_payload, split_id_and_data, Elm327, UdsClient)
import pdc_codes                                                          # noqa: E402
import pdc_obd                                                            # noqa: E402

PASSED = []
FAILED = []


def check(title, condition, detail=""):
    if condition:
        PASSED.append(title)
        print(f"  [ок]     {title}")
    else:
        FAILED.append(f"{title} {detail}")
        print(f"  [ОШИБКА] {title} {detail}")


# ---------------------------------------------------------------------------
def test_frame_parsing():
    print("\nРазбор кадров")

    can_id, payload = split_id_and_data("774027E00")
    check("идентификатор кадра", can_id == 0x774, f"получено {can_id}")
    check("данные одиночного кадра", payload == bytes([0x02, 0x7E, 0x00]))

    single = ["77403590299"]
    _, data = assemble_iso_tp(single)
    check("сборка одиночного кадра", data == bytes([0x59, 0x02, 0x99]))
    check("одиночный кадр не обрезан", not is_truncated(single))

    first_only = ["7741027590219107B13"]
    check("обрыв длинного ответа замечен", is_truncated(first_only))
    check("заявленная длина прочитана", declared_length(first_only) == 0x27,
          f"получено {declared_length(first_only)}")
    partial = partial_payload(first_only)
    check("частичные данные извлечены", partial[:3] == bytes([0x59, 0x02, 0x19]))

    full = ["774101062F197504152", "774214B48494C464520", "77422342E30"]
    _, data = assemble_iso_tp(full)
    check("сборка многокадрового ответа", len(data) == 0x10, f"длина {len(data)}")
    text = "".join(chr(b) for b in data[3:])
    check("текст собран верно", text == "PARKHILFE 4.0", repr(text))


def test_dtc_decoding():
    print("\nРасшифровка кодов")

    code = bytes([0x10, 0x7B, 0x13])
    check("формат кода", format_dtc(code) == "P107B-13", format_dtc(code))
    check("тип отказа: обрыв", failure_text(code) == "обрыв цепи")
    check("тип отказа: замыкание на массу",
          failure_text(bytes([0x10, 0x7C, 0x11])) == "замыкание цепи на массу")
    check("тип отказа: нет связи",
          failure_text(bytes([0x10, 0x7D, 0x87])) == "нет сообщений от узла")

    marks = describe_status(0x2F)
    check("состояние: активна", "активна сейчас" in marks, marks)
    check("состояние: была ранее", "была ранее" in describe_status(0x28))

    check("стандартный код OBD-II",
          pdc_obd.decode_obd_dtc(0x03, 0x01) == "P0301",
          pdc_obd.decode_obd_dtc(0x03, 0x01))
    check("описание стандартного кода",
          "цилиндре 1" in pdc_codes.describe("P0301"))
    check("код производителя опознан",
          "производителя" in pdc_codes.describe("P1234"))
    check("опасный код отмечен", pdc_codes.is_serious("P0301"))


def test_live_exchange():
    print("\nОбмен с заглушкой")

    mock = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "app", "mock_elm327.py"), "35999"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    try:
        elm = Elm327("127.0.0.1", 35999, timeout=4.0)
        elm.connect()
        elm.init()
        version, voltage = elm.identify()
        check("адаптер отвечает", "ELM327" in version, version)
        check("напряжение получено", "V" in voltage, voltage)

        elm.accept_all()
        uds = UdsClient(elm, 0x776)
        check("блок на связи", uds.tester_present())

        name = uds.read_did(0xF197)
        text = "".join(chr(b) for b in name)
        check("имя блока прочитано", text == "ParkAssist", repr(text))

        faults = uds.read_dtcs()
        check("ошибки прочитаны", len(faults) == 2, f"получено {len(faults)}")
        if faults:
            code, status = faults[0]
            check("первый код разобран", format_dtc(code) == "B310A-13",
                  format_dtc(code))

        codes = pdc_obd.read_dtcs(elm, 0x03)
        check("коды двигателя прочитаны", "P0301" in codes, str(codes))
        readiness = pdc_obd.read_readiness(elm)
        check("готовность систем прочитана",
              readiness is not None and len(readiness["checks"]) == 7)

        elm.close()
    finally:
        mock.terminate()


def main():
    print("=" * 62)
    print("  АВТОТЕСТЫ")
    print("=" * 62)
    test_frame_parsing()
    test_dtc_decoding()
    test_live_exchange()

    print("\n" + "=" * 62)
    print(f"  Пройдено: {len(PASSED)}   Провалено: {len(FAILED)}")
    print("=" * 62)
    for item in FAILED:
        print(f"  провал: {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
