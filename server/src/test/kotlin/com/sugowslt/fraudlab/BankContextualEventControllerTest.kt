package com.sugowslt.fraudlab

import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import java.math.BigDecimal
import java.nio.charset.StandardCharsets
import java.time.LocalDate
import java.time.temporal.ChronoUnit
import java.time.format.DateTimeFormatter
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test
import org.springframework.core.io.ClassPathResource
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.datasource.DriverManagerDataSource
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator
import tools.jackson.databind.json.JsonMapper

class BankContextualEventControllerTest {
    private val mapper = JsonMapper.builder().build()

    @Test
    fun `v1 low scores retain amount history and temporal support limitations on replay`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankContextualEventRepository(jdbc, decisions)
        val server = fakeModel(CopyOnWriteArrayList())
        try {
            val controller = controller(server, decisions, contextual)
            val cold = mapper.readTree(controller.scoreAndStore(input("A", "B", "20240101", 100), "cold-support").body!!)
            assertEquals("insufficient_context", cold["supportAssessment"]["status"].stringValue())
            val warm = mapper.readTree(controller.scoreAndStore(input("A", "B", "20240102", 100), "warm-support").body!!)
            assertEquals("within_observed_support", warm["supportAssessment"]["status"].stringValue())
            assertFalse(warm["supportAssessment"]["reasons"].values().any { it.stringValue() == "research_candidate" })
            val outsideBody = input("A", "B", "20240103", 500000001)
            val outside = controller.scoreAndStore(outsideBody, "outside-support")
            val limited = mapper.readTree(outside.body!!)
            assertEquals("outside_fit_support", limited["supportAssessment"]["status"].stringValue())
            assertEquals(500000001, limited["decision"]["amount"].intValue())
            val future = mapper.readTree(controller.scoreAndStore(input("A", "B", "20250101", 100), "future-support").body!!)
            assertEquals("outside_temporal_validation", future["supportAssessment"]["status"].stringValue())
            assertEquals(outside.body, controller.scoreAndStore(outsideBody, "outside-support").body)
            assertEquals(outside.body, controller.evidence(limited["decision"]["id"].stringValue()).body)
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `same account identifiers at different institutions do not share history or chronology`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankContextualEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received)
        try {
            val controller = controller(server, decisions, contextual)
            assertEquals(201, controller.scoreAndStore(input("A", "B", "20240601", 900, "99", "99"), "foreign-source").statusCode.value())
            val first = controller.scoreAndStore(input("A", "B", "20240501", 100), "local-first")
            assertEquals(201, first.statusCode.value())
            assertEquals("cold_start", mapper.readTree(first.body!!)["contextStatus"].stringValue())
            assertEquals(201, controller.scoreAndStore(input("A", "B", "20240502", 200, "10", "99"), "foreign-recipient").statusCode.value())
            val third = controller.scoreAndStore(input("A", "B", "20240503", 300), "local-next")
            assertEquals(201, third.statusCode.value())
            val request = mapper.readTree(received.last())
            val history = request["history"].values().toList()
            assertEquals(listOf(100, 200), history.map { it["거래금액"].intValue() }.sorted())
            assertTrue(history.all { it["출금금융회사일련번호"].stringValue() == "10" })
            val summary = request["priorSummary"]
            assertEquals(2, summary["senderCount"].intValue())
            assertEquals(300, summary["senderAmountSum"].intValue())
            assertEquals(2, summary["distinctRecipients"].intValue())
            assertEquals(1, summary["recipientCount"].intValue())
            assertEquals("20240501", summary["lastRecipientDate"].stringValue())
            val evidence = mapper.readTree(third.body!!)
            assertEquals(2.0, evidence["observedContext"]["distinctRecipients90d"].doubleValue())
            assertEquals(1.0, evidence["observedContext"]["recipient90dCount"].doubleValue())
            assertEquals(2.0, evidence["observedContext"]["daysSinceLastRecipient"].doubleValue())
            assertEquals(409, controller.scoreAndStore(input("A", "C", "20240502", 400), "local-backdate").statusCode.value())
            assertEquals(4, received.size)
            assertEquals(4, jdbc.queryForObject("SELECT COUNT(*) FROM bank_contextual_event", Int::class.java))
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `contextual scoring uses only earlier same-sender transactions within ninety days`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankContextualEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received)
        try {
            val controller = controller(server, decisions, contextual)
            assertEquals(201, controller.scoreAndStore(input("A", "B", "20240101", 100), "a-old").statusCode.value())
            assertEquals(201, controller.scoreAndStore(input("A", "B", "20240430", 200), "a-one").statusCode.value())
            assertEquals(201, controller.scoreAndStore(input("A", "C", "20240430", 300), "a-same-day").statusCode.value())
            assertEquals(201, controller.scoreAndStore(input("X", "B", "20240430", 900), "other-account").statusCode.value())
            val response = controller.scoreAndStore(input("A", "D", "20240501", 400), "a-next-day")

            assertEquals(201, response.statusCode.value())
            val finalRequest = mapper.readTree(received.last())
            assertEquals("20240501", finalRequest["transaction"]["거래일자"].stringValue())
            val history = finalRequest["history"].values().toList()
            assertEquals(2, history.size)
            assertEquals(listOf(200, 300), history.map { it["거래금액"].intValue() }.sorted())
            assertTrue(history.all { it["출금계좌일련번호"].stringValue() == "A" })
            assertTrue(history.all { it["거래일자"].stringValue() == "20240430" })
            assertFalse(received.last().contains("이상거래여부"))
            val decisionId = mapper.readTree(response.body!!)["decision"]["id"].stringValue()
            val evidence = mapper.readTree(controller.evidence(decisionId).body!!)
            assertEquals(2, evidence["historyCount"].intValue())
            assertEquals(3, evidence["priorSummary"]["senderCount"].intValue())
            assertEquals(600, evidence["priorSummary"]["senderAmountSum"].intValue())
            assertEquals("local_history_unverified", evidence["historyStatus"].stringValue())
            assertEquals("bank-context-v1", evidence["featureSchemaVersion"].stringValue())
            assertEquals(kotlin.math.ln1p(2.0), evidence["featureSnapshot"]["sender_90d_count_log"].doubleValue())
            assertEquals(2.0, evidence["observedContext"]["sender90dCount"].doubleValue())
            assertEquals(5, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
            assertEquals(5, jdbc.queryForObject("SELECT COUNT(*) FROM bank_contextual_event", Int::class.java))
            val firstPage = controller.evidencePage(2)
            assertEquals(5L, firstPage.totalCount)
            assertEquals(5L, firstPage.totalAlerts)
            val secondPage = controller.evidencePage(2, cursor = firstPage.nextCursor!!)
            val thirdPage = controller.evidencePage(2, cursor = secondPage.nextCursor!!)
            assertEquals(5, (firstPage.items + secondPage.items + thirdPage.items).map { it.decision.id }.toSet().size)
            assertEquals(null, thirdPage.nextCursor)
            assertFalse(response.body!!.contains("출금계좌일련번호"))
            assertFalse(response.body!!.contains("입금계좌일련번호"))
            assertTrue(controller.evidencePage(2, alert = false).items.isEmpty())
            assertEquals(5L, controller.evidencePage(2, alert = false).totalCount)
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `cold start is explicit and late sender events are rejected before model call`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankContextualEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received)
        try {
            val controller = controller(server, decisions, contextual)
            val first = controller.scoreAndStore(input("A", "B", "20240502", 100), "first")
            assertEquals(201, first.statusCode.value())
            assertEquals("cold_start", mapper.readTree(first.body!!)["historyStatus"].stringValue())
            assertEquals("cold_start", mapper.readTree(first.body!!)["contextStatus"].stringValue())

            val late = controller.scoreAndStore(input("A", "C", "20240501", 500), "late")
            assertEquals(409, late.statusCode.value())
            assertEquals(1, received.size)
            assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `idempotency replay does not rescore or add duplicate history`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankContextualEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received)
        try {
            val controller = controller(server, decisions, contextual)
            val body = input("A", "B", "20240502", 100)
            val first = controller.scoreAndStore(body, "transfer-1")
            val replay = controller.scoreAndStore(body, "transfer-1")
            val conflict = controller.scoreAndStore(input("A", "B", "20240502", 200), "transfer-1")
            assertEquals(201, first.statusCode.value())
            assertEquals(200, replay.statusCode.value())
            assertEquals(first.body, replay.body)
            assertEquals(409, conflict.statusCode.value())
            assertEquals(1, received.size)
            assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM bank_contextual_event", Int::class.java))
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `same sender requests serialize history read model call and save`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankContextualEventRepository(jdbc, decisions)
        val firstReachedModel = CountDownLatch(1)
        val secondReachedModel = CountDownLatch(1)
        val secondTaskStarted = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val received = CopyOnWriteArrayList<String>()
        val modelWorkers = Executors.newFixedThreadPool(2)
        val requestWorkers = Executors.newFixedThreadPool(2)
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.executor = modelWorkers
        server.createContext("/score/bank/contextual") { exchange ->
            val request = exchange.requestBody.readAllBytes().toString(StandardCharsets.UTF_8)
            received += request
            if (received.size == 1) {
                firstReachedModel.countDown()
                releaseFirst.await(5, TimeUnit.SECONDS)
            } else {
                secondReachedModel.countDown()
            }
            val response = modelResponse(request)
            exchange.sendResponseHeaders(200, response.size.toLong())
            exchange.responseBody.use { it.write(response) }
        }
        server.start()
        try {
            val controller = controller(server, decisions, contextual)
            val first = requestWorkers.submit<Int> {
                controller.scoreAndStore(input("A", "B", "20240501", 100), "concurrent-1").statusCode.value()
            }
            assertTrue(firstReachedModel.await(5, TimeUnit.SECONDS))
            val second = requestWorkers.submit<Int> {
                secondTaskStarted.countDown()
                controller.scoreAndStore(input("A", "C", "20240502", 200), "concurrent-2").statusCode.value()
            }
            assertTrue(secondTaskStarted.await(5, TimeUnit.SECONDS))
            assertFalse(secondReachedModel.await(200, TimeUnit.MILLISECONDS))
            releaseFirst.countDown()
            assertEquals(201, first.get(10, TimeUnit.SECONDS))
            assertEquals(201, second.get(10, TimeUnit.SECONDS))
            assertEquals(2, received.size)
            assertEquals(1, mapper.readTree(received.last())["history"].values().count())
        } finally {
            releaseFirst.countDown()
            requestWorkers.shutdownNow()
            server.stop(0)
            modelWorkers.shutdownNow()
        }
    }

    @Test
    fun `contextual model evidence mismatch is rejected without saving`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankContextualEventRepository(jdbc, decisions)
        val calls = AtomicInteger()
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/score/bank/contextual") { exchange ->
            calls.incrementAndGet()
            val request = exchange.requestBody.readAllBytes().toString(StandardCharsets.UTF_8)
            val body = modelResponse(request).toString(StandardCharsets.UTF_8)
                .replace("\"historyCount\":0", "\"historyCount\":1")
                .toByteArray(StandardCharsets.UTF_8)
            exchange.sendResponseHeaders(200, body.size.toLong())
            exchange.responseBody.use { it.write(body) }
        }
        server.start()
        try {
            val result = controller(server, decisions, contextual).scoreAndStore(input("A", "B", "20240502", 100), "mismatch")
            assertEquals(502, result.statusCode.value())
            assertEquals(1, calls.get())
            assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `model observations must match server history and the complete feature schema`() {
        listOf(
            "\"senderLifetimeCount\":0.0" to "\"senderLifetimeCount\":1.0",
            "\"sender90dMeanAmount\":0.0" to "\"sender90dMeanAmount\":100.0",
            "\"amount_log\"" to "\"unexpected_feature\"",
        ).forEachIndexed { index, (before, after) ->
            val jdbc = database()
            val decisions = BankDecisionRepository(jdbc)
            val contextual = BankContextualEventRepository(jdbc, decisions)
            val server = fakeModel(CopyOnWriteArrayList()) { it.replace(before, after) }
            try {
                val result = controller(server, decisions, contextual).scoreAndStore(input("A", "B", "20240502", 100), "invalid-$index")
                assertEquals(502, result.statusCode.value())
                assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
                assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_contextual_event", Int::class.java))
            } finally {
                server.stop(0)
            }
        }
    }

    @Test
    fun `invalid transaction and cursor inputs are rejected without a model call`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankContextualEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received)
        try {
            val controller = controller(server, decisions, contextual)
            val valid = input("A", "B", "20240502", 100)
            listOf(
                valid.replace("20240502", "20240230"),
                valid.replace("\"거래시간대\":9", "\"거래시간대\":9.5"),
                valid.replace("\"거래금액\":100", "\"거래금액\":1e100"),
                valid.replace("\"거래금액\":100", "\"거래금액\":0.001"),
                valid.replace("\"매체구분\":\"2\"", "\"매체구분\":\"9\""),
                valid.dropLast(1) + ",\"history\":[]}",
            ).forEachIndexed { index, body ->
                assertEquals(400, controller.scoreAndStore(body, "bad-$index").statusCode.value())
            }
            assertThrows(org.springframework.web.server.ResponseStatusException::class.java) { controller.evidencePage(0) }
            assertThrows(org.springframework.web.server.ResponseStatusException::class.java) { controller.evidencePage(10, cursor = "invalid-cursor") }
            assertEquals(0, received.size)
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `database evidence failure rolls back the decision as one transaction`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankContextualEventRepository(jdbc, decisions)
        val transaction = BankContextualTransaction("A", "B", "10", "20", "0", BigDecimal("100"), 9, "2", LocalDate.of(2024, 5, 2))
        val summary = contextual.priorSummary(transaction)
        assertThrows(org.springframework.dao.DataIntegrityViolationException::class.java) {
            contextual.save(transaction, 0.4, true, 0.3, "test-v1", "atomic-failure", "hash", -1,
                transaction.transactionDate.minusDays(90), "cold_start", "cold_start",
                "observed transaction history; not a causal explanation", summary, mapper.writeValueAsString(summary.modelInput()),
                "bank-context-v1", "{}", emptyMap(), "{}", emptyMap())
        }
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_contextual_event", Int::class.java))
    }

    @Test
    fun `oversized history is rejected without truncation or scoring`() {
        listOf(20_001 to false, 12_000 to true).forEach { (rows, longIdentifiers) ->
            val jdbc = database()
            val decisions = BankDecisionRepository(jdbc)
            val contextual = BankContextualEventRepository(jdbc, decisions)
            val sender = if (longIdentifiers) "A".repeat(64) else "A"
            val destination = if (longIdentifiers) "B".repeat(64) else "B"
            val institution = if (longIdentifiers) "I".repeat(64) else "10"
            jdbc.update(
                """INSERT INTO bank_decision (id, created_at, amount, time_bucket, fund_type, channel, risk_score, alert, threshold, model_version)
                   SELECT '00000000-0000-0000-0000-' || LPAD(CAST(X AS VARCHAR), 12, '0'), CURRENT_TIMESTAMP, 100, 9, '0', '2', 0.4, TRUE, 0.3, 'test-v1'
                   FROM SYSTEM_RANGE(1, ?)""".trimIndent(), rows)
            jdbc.update(
                """INSERT INTO bank_contextual_event (decision_id, transaction_date, source_account, destination_account, source_institution, destination_institution,
                   history_count, history_window_start, history_status, context_status, evidence_type, feature_schema_version, prior_summary, observed_context, feature_snapshot)
                   SELECT id, DATE '2024-05-01', ?, ?, ?, ?, 0, DATE '2024-02-01', 'cold_start', 'cold_start',
                   'observed transaction history; not a causal explanation', 'bank-context-v1', '{}', '{}', '{}' FROM bank_decision""".trimIndent(),
                sender, destination, institution, institution)
            val received = CopyOnWriteArrayList<String>()
            val server = fakeModel(received)
            try {
                val body = input(sender, destination, "20240502", 100)
                    .replace("\"출금금융회사일련번호\":\"10\"", "\"출금금융회사일련번호\":\"$institution\"")
                    .replace("\"입금금융회사일련번호\":\"20\"", "\"입금금융회사일련번호\":\"$institution\"")
                val response = controller(server, decisions, contextual).scoreAndStore(body, "overflow-$rows")
                assertEquals(413, response.statusCode.value())
                assertTrue(response.body!!.contains(if (longIdentifiers) "request size" else "20000 transactions"))
                assertEquals(0, received.size)
                assertEquals(rows, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
            } finally {
                server.stop(0)
            }
        }
    }

    @Test
    fun `model failure and inconsistent alert are rejected without storing a result`() {
        listOf(503 to { body: String -> body }, 200 to { body: String -> body.replace("\"alert\":true", "\"alert\":false") })
            .forEachIndexed { index, (status, transform) ->
                val jdbc = database()
                val decisions = BankDecisionRepository(jdbc)
                val contextual = BankContextualEventRepository(jdbc, decisions)
                val server = fakeModel(CopyOnWriteArrayList(), status, transform)
                try {
                    val response = controller(server, decisions, contextual).scoreAndStore(input("A", "B", "20240502", 100), "failure-$index")
                    assertEquals(502, response.statusCode.value())
                    assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
                } finally {
                    server.stop(0)
                }
            }
    }

    private fun database(): JdbcTemplate {
        val source = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(source)
        return JdbcTemplate(source)
    }

    private fun controller(server: HttpServer, decisions: BankDecisionRepository, contextual: BankContextualEventRepository) =
        BankContextualEventController("http://127.0.0.1:${server.address.port}", decisions, contextual, mapper)

    private fun fakeModel(received: MutableList<String>, status: Int = 200, transform: (String) -> String = { it }): HttpServer {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/score/bank/contextual") { exchange ->
            val request = exchange.requestBody.readAllBytes().toString(StandardCharsets.UTF_8)
            received += request
            val response = transform(modelResponse(request).toString(StandardCharsets.UTF_8)).toByteArray(StandardCharsets.UTF_8)
            exchange.responseHeaders.add("Content-Type", "application/json")
            exchange.sendResponseHeaders(status, response.size.toLong())
            exchange.responseBody.use { it.write(response) }
        }
        server.start()
        return server
    }

    private fun modelResponse(request: String): ByteArray {
        val input = mapper.readTree(request)
        val historyCount = input["history"].values().count()
        val senderCount = input["priorSummary"]["senderCount"].longValue()
        val date = LocalDate.parse(input["transaction"]["거래일자"].stringValue(), DateTimeFormatter.BASIC_ISO_DATE)
        val window = date.minusDays(90).format(DateTimeFormatter.BASIC_ISO_DATE)
        val status = if (senderCount == 0L) "cold_start" else "history_provided"
        val history = input["history"].values().toList()
        val summary = input["priorSummary"]
        fun age(row: tools.jackson.databind.JsonNode) = ChronoUnit.DAYS.between(
            LocalDate.parse(row["거래일자"].stringValue(), DateTimeFormatter.BASIC_ISO_DATE), date).toDouble()
        val recent7 = history.filter { age(it) <= 7 }
        val recent30 = history.filter { age(it) <= 30 }
        val recipient = input["transaction"]["입금계좌일련번호"].stringValue()
        val bank = input["transaction"]["입금금융회사일련번호"].stringValue()
        val sameRecipient = history.filter { it["입금계좌일련번호"].stringValue() == recipient && it["입금금융회사일련번호"].stringValue() == bank }
        fun mean(rows: List<tools.jackson.databind.JsonNode>) = if (rows.isEmpty()) 0.0 else rows.sumOf { it["거래금액"].doubleValue() } / rows.size
        val observed = mapOf(
            "sender7dCount" to recent7.size.toDouble(), "sender30dCount" to recent30.size.toDouble(), "sender90dCount" to historyCount.toDouble(),
            "sender30dMeanAmount" to mean(recent30), "sender90dMeanAmount" to mean(history),
            "sender90dMaxAmount" to (history.maxOfOrNull { it["거래금액"].doubleValue() } ?: 0.0),
            "distinctRecipients90d" to history.map { it["입금금융회사일련번호"].stringValue() to it["입금계좌일련번호"].stringValue() }.toSet().size.toDouble(),
            "recipient90dCount" to sameRecipient.size.toDouble(), "recipientBank90dCount" to history.count { it["입금금융회사일련번호"].stringValue() == bank }.toDouble(),
            "senderLifetimeCount" to senderCount.toDouble(), "senderLifetimeMeanAmount" to (if (senderCount == 0L) 0.0 else summary["senderAmountSum"].doubleValue() / senderCount),
            "senderLifetimeMaxAmount" to summary["senderAmountMax"].doubleValue(), "distinctRecipientsLifetime" to summary["distinctRecipients"].doubleValue(),
            "recipientLifetimeCount" to summary["recipientCount"].doubleValue(),
            "daysSinceLastSender" to (history.minOfOrNull { age(it) } ?: 91.0), "daysSinceLastRecipient" to (sameRecipient.minOfOrNull { age(it) } ?: 91.0),
        )
        val features = listOf(
            "amount_log", "time_bucket", "fund_code", "channel_code", "same_institution", "day_of_week",
            "sender_7d_count_log", "sender_30d_count_log", "sender_90d_count_log",
            "sender_30d_mean_amount_log", "sender_90d_mean_amount_log", "sender_90d_max_amount_log",
            "amount_to_30d_mean", "amount_to_90d_mean", "amount_to_90d_max", "sender_90d_distinct_recipients_log",
            "recipient_90d_count_log", "recipient_bank_90d_count_log", "same_channel_90d_share", "same_fund_90d_share", "same_time_90d_share",
            "days_since_sender", "days_since_recipient", "sender_all_count_log", "sender_all_mean_amount_log", "sender_all_max_amount_log",
            "amount_to_all_mean", "amount_to_all_max", "sender_all_distinct_recipients_log", "recipient_all_count_log", "recipient_bank_all_count_log",
            "same_channel_all_share", "same_fund_all_share", "days_since_first_sender", "days_since_last_sender_all", "days_since_last_recipient_all",
        ).associateWith { if (it == "sender_90d_count_log") kotlin.math.ln1p(historyCount.toDouble()) else 0.0 }
        return mapper.writeValueAsString(mapOf(
            "riskScore" to 0.4,
            "alert" to true,
            "threshold" to 0.3,
            "modelVersion" to "test-v1",
            "featureSchemaVersion" to "bank-context-v1",
            "historyCount" to historyCount,
            "historyWindowStart" to window,
            "contextStatus" to status,
            "evidenceType" to "observed transaction history; not a causal explanation",
            "observedContext" to observed,
            "featureSnapshot" to features,
        )).toByteArray(StandardCharsets.UTF_8)
    }

    private fun input(source: String, destination: String, date: String, amount: Int, sourceInstitution: String = "10", destinationInstitution: String = "20") =
        """{"출금계좌일련번호":"$source","입금계좌일련번호":"$destination","출금금융회사일련번호":"$sourceInstitution","입금금융회사일련번호":"$destinationInstitution","자금구분":"0","거래금액":$amount,"거래시간대":9,"매체구분":"2","거래일자":"$date"}"""
}
