package com.sugowslt.fraudlab

import java.math.BigDecimal
import java.time.LocalDate
import java.time.LocalDateTime
import java.util.UUID
import kotlin.random.Random
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.springframework.core.io.ClassPathResource
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.datasource.DriverManagerDataSource
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator

class BankGraphAggregationTest {
    @Test
    fun `twenty four nonzero observations match explicit multi account transfers`() {
        val jdbc = database()
        val events = listOf(
            event("X", "A", "2024-02-01T09:00", 100),
            event("Y", "A", "2024-04-01T09:00", 200),
            event("X", "A", "2024-04-24T09:00", 300),
            event("X", "A", "2024-05-01T00:00", 400),
            event("A", "B", "2024-04-30T09:00", 500),
            event("A", "B", "2024-05-01T06:00", 600),
            event("A", "C", "2024-05-01T00:00", 700),
            event("C", "B", "2024-04-24T09:00", 800),
            event("B", "Q", "2024-02-01T09:00", 900),
            event("B", "Q", "2024-05-01T00:00", 1000),
            event("A", "B", "2024-05-01T09:00", 9999), // Unknown order in the current bucket.
            event("R", "B", "2024-05-02T00:00", 9999), // Future day.
            event("P", "A", "2024-02-01T06:00", 9999), // Before the ninety-day lower bucket.
        )
        events.forEach { store(jdbc, it) }
        val target = event("A", "B", "2024-05-01T09:00", 1234)
        val actual = repository(jdbc).graphContext(target)
        val expected = mapOf<String, Any>(
            "asOfDate" to "20240501", "asOfHour" to 9,
            "senderDayCount" to 2L, "senderDayAmount" to 1300.0, "senderDayDistinctRecipients" to 2L, "senderPairDayCount" to 1L,
            "sender24hCount" to 3L, "sender24hAmount" to 1800.0,
            "senderInflow7dCount" to 2L, "senderInflow30dCount" to 3L, "senderInflow90dCount" to 4L,
            "senderInflow90dAmount" to 1000.0, "senderInflow90dDistinctSources" to 2L,
            "senderDayInflowCount" to 1L, "senderDayInflowAmount" to 400.0,
            "recipientInflow7dCount" to 3L, "recipientInflow30dCount" to 3L, "recipientInflow90dCount" to 3L,
            "recipientInflow90dAmount" to 1900.0, "recipientInflow90dDistinctSources" to 2L,
            "recipientDayInflowCount" to 1L, "recipientDayInflowAmount" to 600.0,
            "recipientOutflow90dCount" to 2L, "recipientOutflow90dAmount" to 1900.0,
            "recipientDayOutflowCount" to 1L, "recipientDayOutflowAmount" to 1000.0,
        )
        assertEquals(expected, actual)
        assertTrue(actual.filterKeys { it !in setOf("asOfDate", "asOfHour") }.values.all { (it as Number).toDouble() > 0 })
        assertEquals(expected, reference(target, events))
    }

    @Test
    fun `all eight bucket starts use inclusive rolling boundaries across leap and year changes`() {
        for (day in listOf(LocalDate.of(2024, 1, 1), LocalDate.of(2024, 3, 1))) {
            for (hour in 0..21 step 3) {
                val jdbc = database()
                val now = day.atTime(hour, 0)
                val target = event("A", "B", now, 99)
                val events = buildList {
                    for (windowDays in listOf(1L, 7L, 30L, 90L)) {
                        val boundary = now.minusDays(windowDays)
                        add(event("X", "A", boundary, windowDays.toInt()))
                        add(event("X", "A", boundary.minusHours(3), windowDays.toInt() * 10))
                        add(event("A", "B", boundary, windowDays.toInt() * 100))
                        add(event("B", "Q", boundary, windowDays.toInt() * 1000))
                    }
                    add(event("A", "B", now.minusHours(3), 2))
                    add(event("A", "B", now, 20000))
                    add(event("A", "B", now.plusHours(3), 30000))
                }
                events.forEach { store(jdbc, it) }
                assertEquals(reference(target, events), repository(jdbc).graphContext(target), "target=$now")
            }
        }
    }

    @Test
    fun `independent raw reducer agrees for mixed flows self transfers zero values and repeated partners`() {
        val jdbc = database()
        val now = LocalDateTime.of(2024, 6, 1, 12, 0)
        val random = Random(7321)
        val nodes = listOf("A", "B", "C", "X", "Y", "Q")
        val events = (1..250).map {
            event(nodes[random.nextInt(nodes.size)], nodes[random.nextInt(nodes.size)],
                now.toLocalDate().minusDays(random.nextInt(-1, 93).toLong()).atTime(random.nextInt(8) * 3, 0),
                random.nextInt(0, 1000))
        }
        events.forEach { store(jdbc, it) }
        for (sender in nodes) for (recipient in nodes) {
            val target = event(sender, recipient, now, 123)
            assertEquals(reference(target, events), repository(jdbc).graphContext(target), "$sender -> $recipient")
        }
    }

    @Test
    fun `same raw account identifiers in different institutions remain distinct graph nodes and partners`() {
        val jdbc = database()
        val time = "2024-04-30T09:00"
        val events = listOf(
            event("A", "B", time, 100),
            event("A", "B", time, 9999).copy(sourceInstitution = "99"),
            event("X", "A", time, 9999).copy(destinationInstitution = "99"),
            event("X", "A", time, 200),
            event("X", "A", time, 250).copy(sourceInstitution = "41"),
            event("B", "Q", time, 9999).copy(sourceInstitution = "99"),
            event("B", "Q", time, 300),
            event("Y", "B", time, 9999).copy(destinationInstitution = "99"),
            event("X", "B", time, 400),
            event("A", "B", time, 500).copy(destinationInstitution = "99"),
        )
        events.forEach { store(jdbc, it) }
        val target = event("A", "B", "2024-05-01T09:00", 123)
        val repository = repository(jdbc)
        val graph = repository.graphContext(target)
        assertEquals(reference(target, events), graph)
        assertEquals(2L, graph["sender24hCount"])
        assertEquals(600.0, graph["sender24hAmount"])
        assertEquals(2L, graph["senderInflow90dDistinctSources"])
        assertEquals(3L, graph["recipientInflow90dDistinctSources"])
        val prior = repository.priorSummary(target)
        assertEquals(2L, prior.senderCount)
        assertEquals(BigDecimal("600.00"), prior.senderAmountSum)
        assertEquals(2L, prior.distinctRecipients)
        assertEquals(1L, prior.recipientCount)
        val history = repository.history(target, 20_000)
        assertEquals(2, history.size)
        assertTrue(history.all { it.sourceInstitution == "10" })
    }

    private fun repository(jdbc: JdbcTemplate) = BankGraphEventRepository(jdbc, BankDecisionRepository(jdbc))

    private fun database(): JdbcTemplate {
        val source = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(source)
        return JdbcTemplate(source)
    }

    private fun bank(account: String): String = when (account) {
        "A" -> "10"; "B" -> "20"; "C" -> "30"; "X" -> "40"; "Y" -> "50"; "Q" -> "60"; else -> "70"
    }

    private fun event(source: String, target: String, time: String, amount: Int) = event(source, target, LocalDateTime.parse(time), amount)
    private fun event(source: String, target: String, time: LocalDateTime, amount: Int) = BankContextualTransaction(
        source, target, bank(source), bank(target), "0", BigDecimal.valueOf(amount.toLong()), time.hour, "2", time.toLocalDate())

    private fun store(jdbc: JdbcTemplate, transaction: BankContextualTransaction) {
        val decision = BankDecisionRepository(jdbc).save(transaction.amount, transaction.timeBucket, transaction.fundType, transaction.channel, 0.4, true, 0.3, "fixture")
        jdbc.update(
            """INSERT INTO bank_graph_event (decision_id, transaction_date, transaction_hour, source_account, destination_account,
               source_institution, destination_institution, history_count, history_window_start, history_status, context_status,
               evidence_type, feature_schema_version, prior_summary, observed_context, graph_context, feature_snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'cold_start', 'cold_start', 'fixture', 'fixture', '{}', '{}', '{}', '{}')""".trimIndent(),
            decision.id, transaction.transactionDate, transaction.timeBucket, transaction.sourceAccount, transaction.destinationAccount,
            transaction.sourceInstitution, transaction.destinationInstitution, transaction.transactionDate.minusDays(90))
    }

    /** Mathematical definition over raw events; no production query/helper is reused. */
    private fun reference(target: BankContextualTransaction, events: List<BankContextualTransaction>): Map<String, Any> {
        fun time(event: BankContextualTransaction) = event.transactionDate.atTime(event.timeBucket, 0)
        val now = time(target)
        val past = events.filter { time(it) < now && time(it) >= now.minusDays(90) }
        val senderOut = past.filter { it.sourceAccount == target.sourceAccount && it.sourceInstitution == target.sourceInstitution }
        val senderIn = past.filter { it.destinationAccount == target.sourceAccount && it.destinationInstitution == target.sourceInstitution }
        val recipientIn = past.filter { it.destinationAccount == target.destinationAccount && it.destinationInstitution == target.destinationInstitution }
        val recipientOut = past.filter { it.sourceAccount == target.destinationAccount && it.sourceInstitution == target.destinationInstitution }
        fun day(rows: List<BankContextualTransaction>) = rows.filter { it.transactionDate == target.transactionDate }
        fun recent(rows: List<BankContextualTransaction>, days: Long) = rows.filter { time(it) >= now.minusDays(days) }
        fun amount(rows: List<BankContextualTransaction>) = rows.sumOf { it.amount }.toDouble()
        val daySenderOut = day(senderOut)
        return linkedMapOf<String, Any>(
            "asOfDate" to target.transactionDate.format(java.time.format.DateTimeFormatter.BASIC_ISO_DATE), "asOfHour" to target.timeBucket,
            "senderDayCount" to daySenderOut.size.toLong(), "senderDayAmount" to amount(daySenderOut),
            "senderDayDistinctRecipients" to daySenderOut.map { it.destinationInstitution to it.destinationAccount }.distinct().size.toLong(),
            "senderPairDayCount" to daySenderOut.count { it.destinationAccount == target.destinationAccount && it.destinationInstitution == target.destinationInstitution }.toLong(),
            "sender24hCount" to recent(senderOut, 1).size.toLong(), "sender24hAmount" to amount(recent(senderOut, 1)),
        ).apply {
            for ((prefix, incoming) in listOf("sender" to senderIn, "recipient" to recipientIn)) {
                for (days in listOf(7L, 30L, 90L)) put("${prefix}Inflow${days}dCount", recent(incoming, days).size.toLong())
                put("${prefix}Inflow90dAmount", amount(incoming))
                put("${prefix}Inflow90dDistinctSources", incoming.map { it.sourceInstitution to it.sourceAccount }.distinct().size.toLong())
                put("${prefix}DayInflowCount", day(incoming).size.toLong())
                put("${prefix}DayInflowAmount", amount(day(incoming)))
            }
            put("recipientOutflow90dCount", recipientOut.size.toLong())
            put("recipientOutflow90dAmount", amount(recipientOut))
            put("recipientDayOutflowCount", day(recipientOut).size.toLong())
            put("recipientDayOutflowAmount", amount(day(recipientOut)))
        }
    }
}
