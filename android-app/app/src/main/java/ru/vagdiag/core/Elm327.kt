package ru.vagdiag.core

/**
 * Elm327 — обмен командами с адаптером и сборка ответов.
 *
 * Здесь собраны особенности поведения дешёвых адаптеров, выясненные
 * опытным путём на живом автомобиле. Каждая закрывает конкретную поломку,
 * не убирайте их, не проверив.
 */
class Elm327(private val transport: Transport) {

    /** Полный обмен с адаптером: пригодится, когда результат странный. */
    val rawLog = mutableListOf<Pair<String, String>>()

    /** Команды, которые адаптер не понял. Он отвечает на них знаком вопроса. */
    val unsupported = mutableSetOf<String>()

    private var currentHeader: Int = -1
    private var emptyStreak = 0
    private var recoveries = 0

    val title: String get() = transport.title

    // -- жизненный цикл -----------------------------------------------------

    fun connect() {
        transport.open()
        Thread.sleep(300)
        drain()
    }

    fun close() = transport.close()

    /** Приводит адаптер в предсказуемое состояние. */
    fun init() {
        command("ATZ", timeoutMs = 3000)
        Thread.sleep(500)
        listOf(
            "ATE0",     // без эха команд
            "ATL0",     // без лишних переводов строки
            "ATS0",     // без пробелов в ответе
            "ATH1",     // показывать идентификаторы кадров
            "ATSP6",    // CAN 11 бит, 500 кбит/с
            "ATCAF1",   // автосборка кадров
            "ATAT0",    // предсказуемые тайминги
            "ATST32",   // ожидание ответа около 200 мс
        ).forEach { command(it) }
        currentHeader = -1
        acceptAll()
    }

    fun identify(): Pair<String, String> {
        val version = command("ATI").joinToString(" ").ifBlank { "неизвестен" }
        val voltage = command("ATRV").joinToString(" ").ifBlank { "—" }
        return version to voltage
    }

    /**
     * Принимать кадры с любым идентификатором.
     *
     * Фильтр снимается обнулением маски, а не командой ATCRA без параметра:
     * дешёвые адаптеры её не понимают и отвечают знаком вопроса, молча
     * оставляя прежнюю настройку.
     */
    fun acceptAll() {
        command("ATCF000")
        command("ATCM000")
        command("ATFCSM0")   // вернуть автоматическое управление потоком
    }

    /**
     * Включает один из способов заставить адаптер дочитать длинный ответ.
     *
     * Способы перебираются по очереди, потому что дешёвые адаптеры
     * поддерживают их выборочно и предсказать заранее нельзя.
     * Так же устроена версия для компьютера.
     */
    fun applyMfStrategy(name: String, txId: Int, rxId: Int) {
        when (name) {
            // Только указание адреса ответа, кадр разрешения адаптер
            // формирует сам.
            "cra" -> command("ATCRA%03X".format(rxId))
            // Адрес ответа плюс кадр разрешения, заданный вручную:
            // «шли всё сразу, без пауз».
            "cra_fc" -> {
                command("ATCRA%03X".format(rxId))
                command("ATFCSH%03X".format(txId))
                command("ATFCSD300000")
                command("ATFCSM1")
            }
        }
    }

    /**
     * Режим сырых кадров: адаптер перестаёт собирать многокадровые ответы
     * сам, и кадр разрешения отправляет уже программа.
     */
    fun rawFrames(on: Boolean) {
        if (on) {
            command("ATCAF0")
            command("ATSTFF")   // длинное окно ожидания
        } else {
            command("ATCAF1")
            command("ATST32")
        }
        currentHeader = -1
    }

    /** Забыть запомненный заголовок: после смены режима его надо задать заново. */
    fun forgetHeader() {
        currentHeader = -1
    }

    fun setHeader(canId: Int) {
        if (currentHeader != canId) {
            command("ATSH%03X".format(canId))
            currentHeader = canId
        }
    }

    // -- обмен --------------------------------------------------------------

    /**
     * Отправляет команду и читает ответ до знака приглашения.
     *
     * Перед отправкой чистится буфер: иначе ответ на предыдущую команду
     * читается как ответ на текущую, и данные разных блоков перемешиваются.
     */
    fun command(text: String, timeoutMs: Long = 1200): List<String> {
        drain(30)
        transport.write((text + "\r").toByteArray(Charsets.US_ASCII))

        val builder = StringBuilder()
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val chunk = transport.read()
            if (chunk.isNotEmpty()) {
                builder.append(String(chunk, Charsets.US_ASCII))
                if (builder.contains('>')) break
            } else {
                Thread.sleep(10)
            }
        }

        val raw = builder.toString()
        rawLog += text to raw
        if (rawLog.size > 4000) rawLog.subList(0, 1000).clear()

        if (raw.trimStart().startsWith("?")) unsupported += text

        // Самовосстановление. Дешёвые адаптеры перестают отвечать после
        // нескольких десятков команд подряд. Раньше это выглядело как
        // «блоки молчат» и вело к неверным выводам.
        if (raw.isBlank() && !text.startsWith("AT")) {
            emptyStreak++
            if (emptyStreak >= 3 && recoveries < 5) {
                emptyStreak = 0
                recoveries++
                recover()
            }
        } else {
            emptyStreak = 0
        }

        return raw.replace(">", "").split('\r', '\n')
            .map { it.trim() }
            .filter { it.isNotEmpty() && !it.equals("SEARCHING...", true) }
    }

    private fun recover() {
        rawLog += "[самовосстановление]" to "адаптер перезапускается"
        runCatching {
            transport.write("ATZ\r".toByteArray())
            Thread.sleep(1200)
            drain()
            listOf("ATE0", "ATL0", "ATS0", "ATH1", "ATSP6",
                   "ATCAF1", "ATAT0", "ATST32", "ATCF000", "ATCM000")
                .forEach {
                    transport.write((it + "\r").toByteArray())
                    Thread.sleep(80)
                }
            drain()
            currentHeader = -1
        }
    }

    private fun drain(millis: Long = 150) {
        val deadline = System.currentTimeMillis() + millis
        while (System.currentTimeMillis() < deadline) {
            if (transport.read().isEmpty()) break
        }
    }
}

/**
 * Разбор кадров ISO-TP.
 *
 * Длинный ответ приходит несколькими кадрами: первый несёт заявленную
 * длину, последующие — продолжение. Дешёвые адаптеры дочитывают такие
 * ответы только у двигателя, поэтому здесь важны обе возможности:
 * собрать целое и понять, что пришла лишь часть.
 */
object IsoTp {

    data class Frame(val canId: Int, val payload: ByteArray)

    /** Разбирает строку вида 7E8064100BE3FA813 на идентификатор и байты. */
    fun parseLine(line: String): Frame? {
        val token = line.replace(" ", "").trim().uppercase()
        if (token.length < 5) return null
        val canId = token.substring(0, 3).toIntOrNull(16) ?: return null
        var body = token.substring(3)
        if (body.length % 2 == 1) body = body.dropLast(1)
        val bytes = runCatching {
            ByteArray(body.length / 2) {
                body.substring(it * 2, it * 2 + 2).toInt(16).toByte()
            }
        }.getOrNull() ?: return null
        return Frame(canId, bytes)
    }

    /** Собирает полезную нагрузку. Кадры чужих блоков отбрасываются. */
    fun assemble(lines: List<String>): Pair<Int?, ByteArray> {
        var source: Int? = null
        var single: ByteArray? = null
        var first: Pair<Int, ByteArray>? = null
        val rest = mutableListOf<ByteArray>()

        for (line in lines) {
            val frame = parseLine(line) ?: continue
            if (frame.payload.isEmpty()) continue
            if (source == null) source = frame.canId
            else if (frame.canId != source) continue

            val type = (frame.payload[0].toInt() and 0xF0) shr 4
            when (type) {
                0x0 -> {
                    val length = frame.payload[0].toInt() and 0x0F
                    single = frame.payload.copyOfRange(1, minOf(1 + length, frame.payload.size))
                }
                0x1 -> {
                    val length = ((frame.payload[0].toInt() and 0x0F) shl 8) or
                            (frame.payload[1].toInt() and 0xFF)
                    first = length to frame.payload.copyOfRange(2, frame.payload.size)
                }
                0x2 -> rest += frame.payload.copyOfRange(1, frame.payload.size)
            }
        }

        single?.let { return source to it }
        first?.let { (length, head) ->
            var data = head
            rest.forEach { data += it }
            return source to data.copyOf(minOf(length, data.size))
        }
        return source to ByteArray(0)
    }

    /** Заявленная длина ответа из заголовка. Работает и на обрезанном ответе. */
    fun declaredLength(lines: List<String>): Int? {
        for (line in lines) {
            val frame = parseLine(line) ?: continue
            if (frame.payload.isEmpty()) continue
            val type = (frame.payload[0].toInt() and 0xF0) shr 4
            if (type == 0x1) {
                return ((frame.payload[0].toInt() and 0x0F) shl 8) or
                        (frame.payload[1].toInt() and 0xFF)
            }
            if (type == 0x0) return frame.payload[0].toInt() and 0x0F
        }
        return null
    }

    /** Собирает всё пришедшее, даже если ответ оборван на первом кадре. */
    fun partial(lines: List<String>): ByteArray {
        var source: Int? = null
        var data = ByteArray(0)
        for (line in lines) {
            val frame = parseLine(line) ?: continue
            if (frame.payload.isEmpty()) continue
            if (source == null) source = frame.canId else if (frame.canId != source) continue
            when ((frame.payload[0].toInt() and 0xF0) shr 4) {
                0x0 -> {
                    val length = frame.payload[0].toInt() and 0x0F
                    data += frame.payload.copyOfRange(1, minOf(1 + length, frame.payload.size))
                }
                0x1 -> data += frame.payload.copyOfRange(2, frame.payload.size)
                0x2 -> data += frame.payload.copyOfRange(1, frame.payload.size)
            }
        }
        return data
    }

    /** True, если пришёл только первый кадр из нескольких. */
    fun isTruncated(lines: List<String>): Boolean {
        val declared = declaredLength(lines) ?: return false
        var collected = 0
        var hasFirst = false
        for (line in lines) {
            val frame = parseLine(line) ?: continue
            if (frame.payload.isEmpty()) continue
            when ((frame.payload[0].toInt() and 0xF0) shr 4) {
                0x1 -> { hasFirst = true; collected += frame.payload.size - 2 }
                0x2 -> collected += frame.payload.size - 1
            }
        }
        return hasFirst && collected < declared
    }

    private val negative = listOf(
        "NO DATA", "CAN ERROR", "BUS INIT", "UNABLE TO CONNECT", "BUS BUSY",
        "FB ERROR", "DATA ERROR", "STOPPED", "BUFFER FULL", "ERROR", "?"
    )

    fun isNegative(lines: List<String>): Boolean {
        if (lines.isEmpty()) return true
        val joined = lines.joinToString(" ").uppercase()
        return negative.any { joined.contains(it) }
    }
}
