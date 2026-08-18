#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_elm327.py — заглушка адаптера ELM327 для проверки программы без автомобиля.

Изображает блок парктроника на идентификаторе 0x776 и отвечает на запросы UDS:
чтение ошибок, чтение идентификаторов данных, стирание памяти.
С ключом --glitch имитирует плавающий контакт: один из датчиков периодически
пропадает, что позволяет проверить режим shake, не выходя из дома.

Запуск:
    python mock_elm327.py 35003
    python mock_elm327.py 35003 --glitch
"""

import math
import random
import socket
import sys
import threading
import time

MODULE_ID = 0x776                     # блок парктроника, автосборка
BODY_ID = 0x70A                       # блок кузова: требует ручного разрешения
BODY_RX = 0x774
RESPONDING = {0x776, 0x710, 0x7E0, 0x70A}

GLITCH = "--glitch" in sys.argv
START = time.time()

DATA_DIDS = {0x1001, 0x1002, 0x1003, 0x1004}
INFO_DIDS = {
    0xF187: b"5Q0919294B",
    0xF189: b"0110",
    0xF18A: b"VAG",
    0xF190: b"XW8ZZZ61ZJG000000",
    0xF191: b"5Q0919294",
    0xF197: b"ParkAssist",
}

BODY_NAME = b"PARKHILFE 4.0"
BODY_PART = b"6RU919283A"
BODY_DTC = bytes([0x59, 0x02, 0x19]) + bytes([
    0x10, 0x7B, 0x13, 0x2F,     # обрыв цепи, активна
    0x10, 0x7C, 0x11, 0x28,     # замыкание на массу, была ранее
    0x10, 0x7D, 0x87, 0x2F,     # нет сообщений от узла
])

DTC_STORE = [
    (bytes([0xB1, 0x0A, 0x13]), 0x2F),    # обрыв цепи, активна сейчас
    (bytes([0xB1, 0x0B, 0x11]), 0x28),    # замыкание на массу, была ранее
]


def sensor_value(index):
    """Имитация расстояния. С ключом --glitch третий датчик периодически пропадает."""
    elapsed = time.time() - START
    if GLITCH and index == 3 and (elapsed % 7.0) < 1.2:
        return None
    base = 60 + 40 * math.sin(elapsed / 3.0 + index)
    return max(0, int(base + random.randint(-3, 3)))


def frames_for(payload):
    """Разбивает полезную нагрузку на кадры ISO-TP."""
    response_id = MODULE_ID + 8
    if len(payload) <= 7:
        return [f"{response_id:03X}{len(payload):02X}{payload.hex().upper()}"]
    out = [f"{response_id:03X}1{len(payload):03X}{payload[:6].hex().upper()}"]
    rest, counter = payload[6:], 1
    while rest:
        chunk, rest = rest[:7], rest[7:]
        out.append(f"{response_id:03X}2{counter:X}{chunk.hex().upper()}")
        counter = (counter + 1) & 0x0F
    return out


def handle_uds(request):
    """Формирует ответ блока на запрос UDS."""
    global DTC_STORE
    service = request[0]

    if service == 0x3E:
        return bytes([0x7E, 0x00])

    if service == 0x22 and len(request) >= 3:
        did = (request[1] << 8) | request[2]
        if did in INFO_DIDS:
            return bytes([0x62, request[1], request[2]]) + INFO_DIDS[did]
        if did in DATA_DIDS:
            value = sensor_value(did & 0x0F)
            if value is None:
                return bytes([0x7F, 0x22, 0x22])
            return bytes([0x62, request[1], request[2]]) + value.to_bytes(2, "big")
        return bytes([0x7F, 0x22, 0x31])

    if service == 0x19 and len(request) >= 3 and request[1] == 0x02:
        payload = bytes([0x59, 0x02, 0xFF])
        for code, status in DTC_STORE:
            payload += code + bytes([status])
        return payload

    if service == 0x14:
        DTC_STORE = []
        return bytes([0x54])

    return bytes([0x7F, service, 0x11])


def obd_reply(cmd):
    """Ответы двигателя на стандартные запросы OBD-II."""
    if cmd == "0100":
        return ["7E80641 00BE3FA813".replace(" ", "")]
    if cmd == "0120":
        return ["7E80641209021B015".replace(" ", "")]
    if cmd == "0140":
        return ["7E80641407AD0C000".replace(" ", "")]
    if cmd == "0101":
        return ["7E8064101 8103 0303".replace(" ", "")]
    if cmd == "0105":
        return ["7E80341055A".replace(" ", "")]
    if cmd == "010C":
        return ["7E804410C0FA0".replace(" ", "")]
    if cmd == "010D":
        return ["7E80341 0D00".replace(" ", "")]
    if cmd == "0111":
        return ["7E8034111 33".replace(" ", "")]
    if cmd == "0142":
        return ["7E8044142 3138".replace(" ", "")]
    if cmd.startswith("01"):
        return ["NO DATA"]
    if cmd == "03":
        return ["7E8064302 0301 0420".replace(" ", "")]
    if cmd == "07":
        return ["7E80447010113".replace(" ", "")]
    if cmd == "0A":
        return ["7E8034A00".replace(" ", "")]
    if cmd == "04":
        return ["7E80144".replace(" ", "")]
    if cmd.startswith("0200"):
        pid = cmd[4:6]
        table = {"04": "7E80542000445", "05": "7E8054200055A",
                 "0C": "7E806420000C0BB8", "0D": "7E80542000D1E",
                 "11": "7E8054200112B"}
        return [table.get(pid, "NO DATA")]
    if cmd == "0902":
        return ["7E810144902015857",
                "7E8218385A5A5A363150",
                "7E8225A4A473030303030"]
    return ["NO DATA"]


def parse_request(text):
    token = text.replace(" ", "")
    if len(token) % 2:
        token = token[:-1]
    try:
        return bytes.fromhex(token)
    except ValueError:
        return b""


BODY_NAME = b"PARKHILFE 4.0"
BODY_PART = b"6RU919283A"
BODY_DTC = bytes([0x59, 0x02, 0x19]) + bytes([
    0x10, 0x7B, 0x13, 0x2F,     # обрыв цепи, активна
    0x10, 0x7C, 0x11, 0x28,     # замыкание на массу, была ранее
    0x10, 0x7D, 0x87, 0x2F,     # нет сообщений от узла
])


def body_payload(request):
    """Ответ блока кузовной электроники."""
    if not request:
        return b""
    service = request[0]
    if service == 0x3E:
        return bytes([0x7E, 0x00])
    if service == 0x10:
        return bytes([0x50, request[1] if len(request) > 1 else 0x03])
    if service == 0x22 and len(request) >= 3:
        did = (request[1] << 8) | request[2]
        if did == 0xF197:
            return bytes([0x62, request[1], request[2]]) + BODY_NAME
        if did == 0xF187:
            return bytes([0x62, request[1], request[2]]) + BODY_PART
        return bytes([0x7F, 0x22, 0x31])
    if service == 0x19 and len(request) >= 2 and request[1] == 0x01:
        # число ошибок: короткий ответ, влезает в один кадр
        total = (len(BODY_DTC) - 3) // 4
        return bytes([0x59, 0x01, 0xFF, 0x01, total >> 8, total & 0xFF])
    if service == 0x19:
        return BODY_DTC
    return bytes([0x7F, service, 0x11])


def split_frames(payload, response_id):
    """Разбивает ответ на первый кадр и продолжение."""
    if len(payload) <= 7:
        return [f"{response_id:03X}{len(payload):02X}{payload.hex().upper()}"], []
    first = [f"{response_id:03X}1{len(payload):03X}{payload[:6].hex().upper()}"]
    rest, counter, tail = payload[6:], 1, []
    while rest:
        chunk, rest = rest[:7], rest[7:]
        tail.append(f"{response_id:03X}2{counter:X}{chunk.hex().upper()}")
        counter = (counter + 1) & 0x0F
    return first, tail


def handle(conn):
    header = 0x7E0
    caf = 1
    cra = None          # заданный адрес приёма
    pending = []
    buf = b""
    while True:
        try:
            data = conn.recv(1024)
        except OSError:
            return
        if not data:
            return
        buf += data
        while b"\r" in buf:
            line, buf = buf.split(b"\r", 1)
            cmd = line.decode("ascii", "replace").strip().upper()
            if not cmd:
                continue

            if cmd.startswith("ATSH"):
                try:
                    header = int(cmd[4:], 16)
                except ValueError:
                    pass
                out = ["OK"]
            elif cmd == "ATI":
                out = ["ELM327 v1.5"]
            elif cmd == "ATRV":
                out = ["12.6V"]
            elif cmd.startswith("ATCRA"):
                suffix = cmd[5:].strip()
                try:
                    cra = int(suffix, 16) if suffix else None
                except ValueError:
                    cra = None
                out = ["OK"]
            elif cmd == "ATAR":
                cra = None
                out = ["OK"]
            elif cmd.startswith("ATCM"):
                # нулевая маска снимает адресный фильтр
                if cmd[4:].strip("0") == "":
                    cra = None
                out = ["OK"]
            elif cmd.startswith("ATCAF"):
                caf = 1 if cmd.endswith("1") else 0
                out = ["OK"]
            elif cmd.startswith("AT"):
                out = ["OK"]
            elif header == BODY_ID and caf == 0 and cmd.startswith("3000"):
                # получено разрешение на продолжение
                out = pending or ["NO DATA"]
                pending = []
            elif header == BODY_ID:
                request = parse_request(cmd)
                if caf == 0 and request:
                    request = request[1:1 + request[0]]
                payload = body_payload(request)
                if not payload:
                    out = ["NO DATA"]
                else:
                    first, tail = split_frames(payload, BODY_RX)
                    if caf == 1 and cra == BODY_RX:
                        # адаптеру указан адрес ответа — он сам ведёт обмен
                        out, pending = first + tail, []
                    else:
                        # адрес не указан: приходит только первый кадр
                        out, pending = first, tail
            elif cmd.startswith("01") or cmd in ("03", "07", "0A", "04") \
                    or cmd.startswith("0200") or cmd == "0902":
                out = obd_reply(cmd)
            elif header in RESPONDING:
                request = parse_request(cmd)
                if not request:
                    out = ["NO DATA"]
                elif header == MODULE_ID:
                    out = frames_for(handle_uds(request))
                else:
                    out = [f"{header + 8:03X}027E00"]
            else:
                out = ["NO DATA"]

            conn.sendall(("\r".join(out) + "\r\r>").encode())


def main():
    port = 35000
    for argument in sys.argv[1:]:
        if argument.isdigit():
            port = int(argument)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    suffix = "  (с имитацией плавающего контакта)" if GLITCH else ""
    print(f"мок ELM327 слушает 127.0.0.1:{port}{suffix}", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
