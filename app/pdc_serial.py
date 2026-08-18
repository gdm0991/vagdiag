#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdc_serial.py — работа через последовательный порт.

Зачем. Адаптеры ELM327 бывают трёх видов: Wi-Fi, USB и Bluetooth.
Первый общается по сети, два других в Windows видны как COM-порт.
Без этого модуля программа поддерживала бы только треть адаптеров.

Внешних библиотек нет намеренно: pyserial потребовал бы установки
и сломал переносимость пакета. На Windows порт открывается напрямую
через системные вызовы, на Linux и macOS — штатными средствами Python.

Класс SerialChannel намеренно повторяет поведение сетевого сокета
(sendall, recv, settimeout, close). Благодаря этому вся остальная
программа не знает, по проводу она работает или по сети.
"""

import os
import sys
import time

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    class DCB(ctypes.Structure):
        _fields_ = [
            ("DCBlength", wintypes.DWORD), ("BaudRate", wintypes.DWORD),
            ("fBits", wintypes.DWORD),
            ("wReserved", wintypes.WORD), ("XonLim", wintypes.WORD),
            ("XoffLim", wintypes.WORD), ("ByteSize", wintypes.BYTE),
            ("Parity", wintypes.BYTE), ("StopBits", wintypes.BYTE),
            ("XonChar", ctypes.c_char), ("XoffChar", ctypes.c_char),
            ("ErrorChar", ctypes.c_char), ("EofChar", ctypes.c_char),
            ("EvtChar", ctypes.c_char), ("wReserved1", wintypes.WORD),
        ]

    class COMMTIMEOUTS(ctypes.Structure):
        _fields_ = [
            ("ReadIntervalTimeout", wintypes.DWORD),
            ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
            ("ReadTotalTimeoutConstant", wintypes.DWORD),
            ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
            ("WriteTotalTimeoutConstant", wintypes.DWORD),
        ]


class SerialError(OSError):
    """Ошибка работы с последовательным портом."""


class SerialChannel:
    """
    Последовательный порт с интерфейсом сетевого сокета.

    Поддерживает те же методы, что использует остальная программа
    для работы по сети, поэтому подменяется без единой правки в других
    модулях.
    """

    def __init__(self, port, baudrate=38400, timeout=2.0):
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout = timeout
        self.handle = None
        self.fd = None
        self._open()

    # -- открытие ----------------------------------------------------------
    def _open(self):
        if IS_WINDOWS:
            self._open_windows()
        else:
            self._open_posix()

    def _open_windows(self):
        name = self.port
        if not name.startswith("\\\\.\\"):
            name = "\\\\.\\" + name          # нужно для портов выше COM9
        handle = ctypes.windll.kernel32.CreateFileW(
            name, GENERIC_READ | GENERIC_WRITE, 0, None,
            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if handle == INVALID_HANDLE or handle is None:
            raise SerialError(f"не удалось открыть порт {self.port}")
        self.handle = handle

        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        if not ctypes.windll.kernel32.GetCommState(handle, ctypes.byref(dcb)):
            raise SerialError("не удалось прочитать настройки порта")
        dcb.BaudRate = self.baudrate
        dcb.ByteSize = 8
        dcb.Parity = 0                      # без контроля чётности
        dcb.StopBits = 0                    # один стоповый бит
        dcb.fBits = 0x0001                  # разрешить работу порта
        if not ctypes.windll.kernel32.SetCommState(handle, ctypes.byref(dcb)):
            raise SerialError("не удалось применить настройки порта")

        timeouts = COMMTIMEOUTS(1, 0, 50, 0, 200)
        ctypes.windll.kernel32.SetCommTimeouts(handle, ctypes.byref(timeouts))

    def _open_posix(self):
        import termios
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(self.fd)
        except termios.error:
            return                          # псевдотерминал настроек не имеет

        speed = getattr(termios, f"B{self.baudrate}", termios.B38400)
        attrs[0] = 0                        # без обработки входного потока
        attrs[1] = 0                        # без обработки выходного
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0                        # неканонический режим, без эха
        attrs[4] = attrs[5] = speed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 1
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    # -- интерфейс сокета --------------------------------------------------
    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        if IS_WINDOWS:
            written = wintypes.DWORD(0)
            ok = ctypes.windll.kernel32.WriteFile(
                self.handle, data, len(data), ctypes.byref(written), None)
            if not ok:
                raise SerialError("ошибка записи в порт")
        else:
            os.write(self.fd, data)

    def recv(self, size=4096):
        deadline = time.time() + (self.timeout or 0.1)
        while True:
            chunk = self._read_once(size)
            if chunk:
                return chunk
            if time.time() >= deadline:
                raise TimeoutError("порт молчит")
            time.sleep(0.01)

    def _read_once(self, size):
        if IS_WINDOWS:
            buffer = ctypes.create_string_buffer(size)
            read = wintypes.DWORD(0)
            ok = ctypes.windll.kernel32.ReadFile(
                self.handle, buffer, size, ctypes.byref(read), None)
            if not ok:
                raise SerialError("ошибка чтения из порта")
            return buffer.raw[:read.value]
        try:
            return os.read(self.fd, size) or b""
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise SerialError(str(exc))

    def close(self):
        try:
            if IS_WINDOWS and self.handle:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            elif self.fd is not None:
                os.close(self.fd)
        except OSError:
            pass
        finally:
            self.handle = None
            self.fd = None


def list_ports():
    """
    Возвращает список доступных последовательных портов.

    На Windows читается ветка реестра, где система перечисляет порты —
    так видно и USB-переходники, и профили Bluetooth.
    """
    found = []
    if IS_WINDOWS:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DEVICEMAP\SERIALCOMM")
            index = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, index)
                    found.append(value)
                    index += 1
                except OSError:
                    break
        except OSError:
            pass
        if not found:
            found = [f"COM{n}" for n in range(1, 17)]
        return found

    for folder, prefixes in (("/dev", ("ttyUSB", "ttyACM", "rfcomm", "cu.")),):
        try:
            for name in sorted(os.listdir(folder)):
                if name.startswith(prefixes):
                    found.append(os.path.join(folder, name))
        except OSError:
            pass
    return found
