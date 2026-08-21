package ru.vagdiag.core

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothSocket
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.UUID

/**
 * Канал связи с адаптером.
 *
 * Wi-Fi и Bluetooth различаются только способом открытия соединения,
 * дальше обе разновидности выглядят одинаково — поток байт туда и обратно.
 * Поэтому весь остальной код работает с этим интерфейсом и не знает,
 * по какому каналу общается.
 */
interface Transport {
    fun open()
    fun write(data: ByteArray)
    /** Читает то, что успело прийти. Пустой массив означает тишину. */
    fun read(): ByteArray
    fun close()
    val title: String
}

/**
 * Адаптер ELM327 с Wi-Fi. Держит открытую точку доступа, к которой
 * подключается телефон, и принимает соединение на фиксированном порту.
 */
class WifiTransport(
    private val host: String = "192.168.0.10",
    private val port: Int = 35000,
) : Transport {

    private var socket: Socket? = null
    private var input: InputStream? = null
    private var output: OutputStream? = null

    override val title: String get() = "Wi-Fi $host:$port"

    override fun open() {
        val created = Socket()
        // Небольшой таймаут подключения: если телефон не в сети адаптера,
        // ждать полминуты бессмысленно, лучше сразу сказать об этом.
        created.connect(InetSocketAddress(host, port), 5000)
        created.soTimeout = 200
        socket = created
        input = created.getInputStream()
        output = created.getOutputStream()
    }

    override fun write(data: ByteArray) {
        output?.write(data)
        output?.flush()
    }

    override fun read(): ByteArray {
        val stream = input ?: return ByteArray(0)
        return try {
            val available = stream.available()
            if (available <= 0) {
                // Пробуем прочитать один байт с таймаутом сокета,
                // иначе цикл ожидания крутился бы вхолостую.
                val single = stream.read()
                if (single < 0) ByteArray(0) else byteArrayOf(single.toByte())
            } else {
                val buffer = ByteArray(available)
                val count = stream.read(buffer)
                if (count <= 0) ByteArray(0) else buffer.copyOf(count)
            }
        } catch (_: Exception) {
            ByteArray(0)
        }
    }

    override fun close() {
        runCatching { socket?.close() }
        socket = null
        input = null
        output = null
    }
}

/**
 * Адаптер ELM327 с Bluetooth.
 *
 * Ради этого и написано отдельное приложение: Termux к профилю
 * последовательного порта доступа не имеет, а обычное приложение имеет.
 * Bluetooth-адаптеры распространены не меньше, чем Wi-Fi, и до сих пор
 * оставались за бортом.
 */
class BluetoothTransport(private val device: BluetoothDevice) : Transport {

    private var socket: BluetoothSocket? = null
    private var input: InputStream? = null
    private var output: OutputStream? = null

    // Аннотация вешается на геттер: у свойства нет поля,
    // на само свойство Kotlin её не пропускает.
    @get:SuppressLint("MissingPermission")
    override val title: String get() = "Bluetooth ${runCatching { device.name }.getOrNull() ?: ""}"

    @SuppressLint("MissingPermission")
    override fun open() {
        // Стандартный идентификатор профиля последовательного порта.
        // Все адаптеры ELM327 используют именно его.
        val sppUuid = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
        val created = device.createRfcommSocketToServiceRecord(sppUuid)
        runCatching { BluetoothAdapter.getDefaultAdapter()?.cancelDiscovery() }
        created.connect()
        socket = created
        input = created.inputStream
        output = created.outputStream
    }

    override fun write(data: ByteArray) {
        output?.write(data)
        output?.flush()
    }

    override fun read(): ByteArray {
        val stream = input ?: return ByteArray(0)
        return try {
            val available = stream.available()
            if (available <= 0) return ByteArray(0)
            val buffer = ByteArray(available)
            val count = stream.read(buffer)
            if (count <= 0) ByteArray(0) else buffer.copyOf(count)
        } catch (_: Exception) {
            ByteArray(0)
        }
    }

    override fun close() {
        runCatching { socket?.close() }
        socket = null
        input = null
        output = null
    }
}

/**
 * Адаптер ELM327 с разъёмом USB, подключённый к телефону через кабель OTG.
 *
 * Внутри такого адаптера стоит преобразователь USB↔UART — CH340, FTDI,
 * CP210x или PL2303. Android умеет отдавать приложению «сырое» USB-устройство,
 * но не знает, как разговаривать с этими микросхемами, поэтому драйвер берётся
 * из библиотеки usb-serial-for-android.
 *
 * Скорость обмена у дешёвых адаптеров не одна и та же: встречаются 38400,
 * 9600 и 115200. Угадывать за пользователя нельзя, поэтому канал сам
 * перебирает скорости и оставляет ту, на которой адаптер отозвался.
 */
class UsbSerialTransport(
    private val manager: android.hardware.usb.UsbManager,
    private val device: android.hardware.usb.UsbDevice,
    private val fixedBaud: Int? = null,
) : Transport {

    private var port: com.hoho.android.usbserial.driver.UsbSerialPort? = null
    private var connection: android.hardware.usb.UsbDeviceConnection? = null
    private var baud: Int = 0

    override val title: String
        get() = "USB " + (runCatching { device.productName }.getOrNull() ?: "адаптер") +
                if (baud > 0) ", $baud бод" else ""

    override fun open() {
        val driver = com.hoho.android.usbserial.driver.UsbSerialProber
            .getDefaultProber().probeDevice(device)
            ?: throw java.io.IOException("Микросхема этого адаптера не поддерживается")

        val link = manager.openDevice(device)
            ?: throw java.io.IOException("Android не дал доступ к устройству")
        connection = link

        val serial = driver.ports.firstOrNull()
            ?: throw java.io.IOException("У устройства нет последовательного порта")
        serial.open(link)
        port = serial

        val candidates = fixedBaud?.let { listOf(it) } ?: listOf(38400, 9600, 115200, 57600)
        for (speed in candidates) {
            serial.setParameters(
                speed, 8,
                com.hoho.android.usbserial.driver.UsbSerialPort.STOPBITS_1,
                com.hoho.android.usbserial.driver.UsbSerialPort.PARITY_NONE)
            // Пустая строка сбрасывает недочитанную команду, ATI просит
            // адаптер представиться. Осмысленный ответ означает,
            // что скорость угадана.
            runCatching { serial.purgeHwBuffers(true, true) }
            serial.write("\r".toByteArray(), 300)
            Thread.sleep(150)
            val trash = ByteArray(512)
            runCatching { serial.read(trash, 200) }
            serial.write("ATI\r".toByteArray(), 500)
            Thread.sleep(400)
            val buffer = ByteArray(512)
            val size = runCatching { serial.read(buffer, 800) }.getOrDefault(0)
            val answer = String(buffer, 0, maxOf(size, 0))
            if (answer.contains("ELM", true) || answer.contains("OBD", true) ||
                answer.contains(">")) {
                baud = speed
                return
            }
        }
        close()
        throw java.io.IOException(
            "Адаптер молчит на скоростях 38400, 9600, 115200 и 57600. " +
            "Проверьте кабель OTG и питание адаптера.")
    }

    override fun write(data: ByteArray) {
        port?.write(data, 1000)
    }

    override fun read(): ByteArray {
        val serial = port ?: return ByteArray(0)
        val buffer = ByteArray(1024)
        val size = runCatching { serial.read(buffer, 120) }.getOrDefault(0)
        return if (size > 0) buffer.copyOf(size) else ByteArray(0)
    }

    override fun close() {
        runCatching { port?.close() }
        runCatching { connection?.close() }
        port = null
        connection = null
    }
}
