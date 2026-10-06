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

class BankGraphEventControllerTest {
    private val mapper = JsonMapper.builder().build()

    @Test
    fun `unsupported amounts and dates keep the actual score but cannot claim observed support`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val server = fakeModel(CopyOnWriteArrayList())
        try {
            val controller = controller(server, decisions, contextual)
            val cases = listOf(
                Triple(input("A", "B", "20240502", 0), "outside_fit_support", listOf("amount_outside_fit_range", "cold_context")),
                Triple(input("X", "Y", "20240502", 500000001), "outside_fit_support", listOf("amount_outside_fit_range", "cold_context")),
                Triple(input("P", "Q", "20240502", 1), "insufficient_context", listOf("cold_context")),
                Triple(input("R", "S", "20240502", 500000000), "insufficient_context", listOf("cold_context")),
                Triple(input("R", "S", "20250101", 500000000), "outside_temporal_validation", listOf("date_outside_retrospective_evaluation")),
            )
            cases.forEachIndexed { index, (body, expectedStatus, expectedExtraReasons) ->
                val response = controller.scoreAndStore(body, "support-$index")
                assertEquals(201, response.statusCode.value())
                val evidence = mapper.readTree(response.body!!)
                assertEquals(0.4, evidence["decision"]["riskScore"].doubleValue())
                assertTrue(evidence["decision"]["alert"].booleanValue())
                assertEquals(expectedStatus, evidence["supportAssessment"]["status"].stringValue())
                assertEquals(listOf("synthetic_labels_only", "local_history_completeness_unverified", "research_candidate") + expectedExtraReasons,
                    evidence["supportAssessment"]["reasons"].values().map { it.stringValue() })
                assertEquals(response.body, controller.evidence(evidence["decision"]["id"].stringValue()).body)
            }
        } finally { server.stop(0) }
    }

    @Test
    fun `incorrect support labels are rejected without storing a successful result`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val server = fakeModel(CopyOnWriteArrayList()) { it.replace("\"status\":\"insufficient_context\"", "\"status\":\"within_observed_support\"") }
        try {
            val response = controller(server, decisions, contextual).scoreAndStore(input("A", "B", "20240502", 100), "fake-support")
            assertEquals(502, response.statusCode.value())
            assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
        } finally { server.stop(0) }
    }

    @Test
    fun `invalid score explanations cannot be stored as successful decisions`() {
        val mutations = listOf<(String) -> String>(
            { it.replace("exact-group-shapley-score-v1", "invented-explanation") },
            { it.replace("\"explainedScore\":0.4", "\"explainedScore\":0.5") },
            { it.replace("\"contribution\":0.2", "\"contribution\":0.3") },
            { it.replace("\"baselineScore\":0.2", "\"baselineScore\":1.2") },
            { it.replace("\"normalReferenceRows\":100", "\"normalReferenceRows\":0") },
            { it.replace("\"coalitionsEvaluated\":128", "\"coalitionsEvaluated\":127") },
            { it.replace("\"coalitionsEvaluated\":128", "\"coalitionsEvaluated\":4294967424") },
            { it.replace("\"reconstructionError\":0.0", "\"reconstructionError\":0.1") },
            { it.replace("\"group\":\"recipient_flow\"", "\"group\":\"sender_inflow\"") },
            { it.replace("\"features\":[\"amount_log\"", "\"features\":[\"amount_log\",\"amount_log\"") },
            { it.replace("\"features\":[\"amount_log\"", "\"features\":[\"unknown_feature\"") },
            { it.replace("\"method\":\"exact-group-shapley-score-v1\"", "\"ignoredMethod\":\"exact-group-shapley-score-v1\"") },
        )
        mutations.forEachIndexed { index, mutate ->
            val jdbc = database()
            val decisions = BankDecisionRepository(jdbc)
            val contextual = BankGraphEventRepository(jdbc, decisions)
            val server = fakeModel(CopyOnWriteArrayList(), transform = mutate)
            try {
                val response = controller(server, decisions, contextual).scoreAndStore(input("A", "B", "20240502", 100), "invalid-explanation-$index")
                assertEquals(502, response.statusCode.value())
                assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
                assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_graph_event", Int::class.java))
            } finally { server.stop(0) }
        }
    }

    @Test
    fun `negative score contributions reconstruct a stored result and survive list detail and replay`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received) { it.replace("\"baselineScore\":0.2", "\"baselineScore\":0.6").replace("\"contribution\":0.2", "\"contribution\":-0.2") }
        try {
            val controller = controller(server, decisions, contextual)
            val body = input("A", "B", "20240502", 100)
            val created = controller.scoreAndStore(body, "negative-explanation")
            assertEquals(201, created.statusCode.value())
            val node = mapper.readTree(created.body!!)
            val explanation = node["modelExplanation"]
            assertEquals(7, explanation["groups"].size())
            assertEquals(66, explanation["groups"].values().sumOf { it["features"].size() })
            assertEquals(-0.2, explanation["groups"][0]["contribution"].doubleValue())
            assertEquals(0.4, explanation["baselineScore"].doubleValue() + explanation["groups"].values().sumOf { it["contribution"].doubleValue() }, 1e-8)
            assertEquals(created.body, controller.evidence(node["decision"]["id"].stringValue()).body)
            assertEquals(created.body, controller.scoreAndStore(body, "negative-explanation").body)
            assertEquals(1, received.size)
            assertEquals(-0.2, controller.evidencePage(20).items.single().modelExplanation!!.groups[0].contribution)
        } finally { server.stop(0) }
    }

    @Test
    fun `legacy candidate records retain missing explanations during idempotent schema initialization`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val server = fakeModel(CopyOnWriteArrayList())
        try {
            val controller = controller(server, decisions, contextual)
            val created = controller.scoreAndStore(input("A", "B", "20240502", 100), "legacy-candidate")
            assertEquals(201, created.statusCode.value())
            val id = mapper.readTree(created.body!!)["decision"]["id"].stringValue()
            jdbc.update("UPDATE bank_graph_event SET model_explanation = NULL WHERE decision_id = ?", id)
            ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(jdbc.dataSource!!)
            val legacy = mapper.readTree(controller.evidence(id).body!!)
            assertTrue(legacy["modelExplanation"].isNull)
            assertEquals(0.4, legacy["decision"]["riskScore"].doubleValue())
            assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM bank_graph_event", Int::class.java))
        } finally { server.stop(0) }
    }

    @Test
    fun `candidate is disabled by default without calling the model or touching the v1 ledger`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received)
        try {
            val disabled = BankGraphEventController("http://127.0.0.1:${server.address.port}", decisions, contextual, mapper)
            assertEquals(mapOf("enabled" to false, "releaseReady" to false), disabled.capabilities())
            assertEquals(mapOf("enabled" to true, "releaseReady" to false), controller(server, decisions, contextual).capabilities())
            assertEquals(503, disabled.scoreAndStore(input("A", "B", "20240502", 100), "disabled").statusCode.value())
            assertEquals(0, received.size)
            assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
            assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_contextual_event", Int::class.java))
        } finally { server.stop(0) }
    }

    @Test
    fun `global ledger rejects late buckets from other senders and same bucket events cannot affect graph scores`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received)
        try {
            val controller = controller(server, decisions, contextual)
            assertEquals(201, controller.scoreAndStore(input("A", "B", "20240502", 100), "first-graph").statusCode.value())
            val earlierBucket = input("X", "B", "20240502", 200).replace("\"거래시간대\":9", "\"거래시간대\":6")
            assertEquals(409, controller.scoreAndStore(earlierBucket, "late-other-sender").statusCode.value())
            val sameBucket = controller.scoreAndStore(input("X", "B", "20240502", 200), "same-bucket")
            assertEquals(201, sameBucket.statusCode.value())
            assertEquals(0L, mapper.readTree(sameBucket.body!!)["graphContext"]["recipientDayInflowCount"].longValue())
            assertEquals("cold_start", mapper.readTree(sameBucket.body!!)["contextStatus"].stringValue())
            val laterBucket = input("Y", "B", "20240502", 300).replace("\"거래시간대\":9", "\"거래시간대\":12")
            val result = controller.scoreAndStore(laterBucket, "later-bucket")
            assertEquals(201, result.statusCode.value())
            val evidence = mapper.readTree(result.body!!)
            assertEquals(2L, evidence["graphContext"]["recipientDayInflowCount"].longValue())
            assertEquals("prior_day_cold_start", evidence["historyStatus"].stringValue())
            assertEquals("history_provided", evidence["contextStatus"].stringValue())
            assertEquals(0L, evidence["priorSummary"]["senderCount"].longValue())
            assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_contextual_event", Int::class.java))
            assertEquals(3, received.size)
        } finally { server.stop(0) }
    }

    @Test
    fun `model graph echo must match server observations and clients cannot inject graph state`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received) { it.replace("\"senderDayCount\":0", "\"senderDayCount\":1") }
        try {
            val controller = controller(server, decisions, contextual)
            assertEquals(502, controller.scoreAndStore(input("A", "B", "20240502", 100), "graph-mismatch").statusCode.value())
            assertEquals(400, controller.scoreAndStore(input("A", "B", "20240502", 100).dropLast(1) + ",\"graphContext\":{}}", "graph-injection").statusCode.value())
            assertEquals(1, received.size)
            assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
            assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_graph_event", Int::class.java))
        } finally { server.stop(0) }
    }

    @Test
    fun `previous day absence is distinct from same day sender graph context`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received)
        try {
            val controller = controller(server, decisions, contextual)
            val first = controller.scoreAndStore(input("A", "B", "20240502", 100).replace("\"거래시간대\":9", "\"거래시간대\":6"), "prior-bucket")
            assertEquals(201, first.statusCode.value())
            val later = controller.scoreAndStore(input("A", "C", "20240502", 200), "sender-graph")
            assertEquals(201, later.statusCode.value())
            val evidence = mapper.readTree(later.body!!)
            assertEquals(0L, evidence["priorSummary"]["senderCount"].longValue())
            assertEquals(0, evidence["historyCount"].intValue())
            assertEquals("prior_day_cold_start", evidence["historyStatus"].stringValue())
            assertEquals("history_provided", evidence["contextStatus"].stringValue())
            assertEquals(1L, evidence["graphContext"]["senderDayCount"].longValue())
            assertEquals(100.0, evidence["graphContext"]["senderDayAmount"].doubleValue())
        } finally { server.stop(0) }
    }

    @Test
    fun `contextual scoring uses only earlier same-sender transactions within ninety days`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
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
            assertEquals("bank-context-v2-candidate", evidence["featureSchemaVersion"].stringValue())
            assertEquals(kotlin.math.ln1p(2.0), evidence["featureSnapshot"]["sender_90d_count_log"].doubleValue())
            assertEquals(2.0, evidence["observedContext"]["sender90dCount"].doubleValue())
            assertEquals(5, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
            assertEquals(5, jdbc.queryForObject("SELECT COUNT(*) FROM bank_graph_event", Int::class.java))
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
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received)
        try {
            val controller = controller(server, decisions, contextual)
            val first = controller.scoreAndStore(input("A", "B", "20240502", 100), "first")
            assertEquals(201, first.statusCode.value())
            assertEquals("prior_day_cold_start", mapper.readTree(first.body!!)["historyStatus"].stringValue())
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
        val contextual = BankGraphEventRepository(jdbc, decisions)
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
            assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM bank_graph_event", Int::class.java))
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `different senders serialize global graph read model call and save`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val firstReachedModel = CountDownLatch(1)
        val secondReachedModel = CountDownLatch(1)
        val secondTaskStarted = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val received = CopyOnWriteArrayList<String>()
        val modelWorkers = Executors.newFixedThreadPool(2)
        val requestWorkers = Executors.newFixedThreadPool(2)
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.executor = modelWorkers
        server.createContext("/score/bank/contextual-v2") { exchange ->
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
                controller.scoreAndStore(input("X", "B", "20240502", 200), "concurrent-2").statusCode.value()
            }
            assertTrue(secondTaskStarted.await(5, TimeUnit.SECONDS))
            assertFalse(secondReachedModel.await(200, TimeUnit.MILLISECONDS))
            releaseFirst.countDown()
            assertEquals(201, first.get(10, TimeUnit.SECONDS))
            assertEquals(201, second.get(10, TimeUnit.SECONDS))
            assertEquals(2, received.size)
            assertEquals(0, mapper.readTree(received.last())["history"].values().count())
            assertEquals(1L, mapper.readTree(received.last())["graphContext"]["recipientInflow90dCount"].longValue())
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
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val calls = AtomicInteger()
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/score/bank/contextual-v2") { exchange ->
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
            val contextual = BankGraphEventRepository(jdbc, decisions)
            val server = fakeModel(CopyOnWriteArrayList()) { it.replace(before, after) }
            try {
                val result = controller(server, decisions, contextual).scoreAndStore(input("A", "B", "20240502", 100), "invalid-$index")
                assertEquals(502, result.statusCode.value())
                assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
                assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_graph_event", Int::class.java))
            } finally {
                server.stop(0)
            }
        }
    }

    @Test
    fun `invalid transaction and cursor inputs are rejected without a model call`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
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
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val transaction = BankContextualTransaction("A", "B", "10", "20", "0", BigDecimal("100"), 9, "2", LocalDate.of(2024, 5, 2))
        val summary = contextual.priorSummary(transaction)
        assertThrows(org.springframework.dao.DataIntegrityViolationException::class.java) {
            contextual.save(transaction, 0.4, true, 0.3, "test-v1", "atomic-failure", "hash", -1,
                transaction.transactionDate.minusDays(90), "cold_start", "cold_start",
                "observed transaction history; not a causal explanation", summary, mapper.writeValueAsString(summary.modelInput()),
                "bank-context-v2-candidate", "{}", emptyMap(), "{}", emptyMap(), "{}", protocolExplanation(emptyList()), "{}", BankSupportAssessment.expected(transaction, "cold_start"), "{}", emptyMap())
        }
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_graph_event", Int::class.java))
    }

    @Test
    fun `oversized history is rejected without truncation or scoring`() {
        listOf(20_001 to false, 12_000 to true).forEach { (rows, longIdentifiers) ->
            val jdbc = database()
            val decisions = BankDecisionRepository(jdbc)
            val contextual = BankGraphEventRepository(jdbc, decisions)
            val sender = if (longIdentifiers) "A".repeat(64) else "A"
            val destination = if (longIdentifiers) "B".repeat(64) else "B"
            val institution = if (longIdentifiers) "I".repeat(64) else "10"
            jdbc.update(
                """INSERT INTO bank_decision (id, created_at, amount, time_bucket, fund_type, channel, risk_score, alert, threshold, model_version)
                   SELECT '00000000-0000-0000-0000-' || LPAD(CAST(X AS VARCHAR), 12, '0'), CURRENT_TIMESTAMP, 100, 9, '0', '2', 0.4, TRUE, 0.3, 'test-v1'
                   FROM SYSTEM_RANGE(1, ?)""".trimIndent(), rows)
            jdbc.update(
                """INSERT INTO bank_graph_event (decision_id, transaction_date, transaction_hour, source_account, destination_account, source_institution, destination_institution,
                   history_count, history_window_start, history_status, context_status, evidence_type, feature_schema_version, prior_summary, observed_context, graph_context, feature_snapshot)
                   SELECT id, DATE '2024-05-01', 9, ?, ?, ?, ?, 0, DATE '2024-02-01', 'cold_start', 'cold_start',
                   'observed transaction history; not a causal explanation', 'bank-context-v2-candidate', '{}', '{}', '{}', '{}' FROM bank_decision""".trimIndent(),
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
                val contextual = BankGraphEventRepository(jdbc, decisions)
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

    @Test
    fun `closed research windows allow original idempotent replay but refuse new transactions`() {
        val jdbc = database()
        val decisions = BankDecisionRepository(jdbc)
        val contextual = BankGraphEventRepository(jdbc, decisions)
        val received = CopyOnWriteArrayList<String>()
        val server = fakeModel(received)
        try {
            val controller = controller(server, decisions, contextual)
            val body = input("A", "B", "20240502", 100)
            val created = controller.scoreAndStore(body, "before-close")
            assertEquals(201, created.statusCode.value())
            jdbc.update("INSERT INTO bank_closed_window_review VALUES('2024-05-02',9,CURRENT_TIMESTAMP,1,'test','{}')")
            assertEquals(created.body, controller.scoreAndStore(body, "before-close").body)
            assertEquals(409, controller.scoreAndStore(input("C", "D", "20240502", 200), "after-close").statusCode.value())
            assertEquals(1, received.size)
            assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
        } finally { server.stop(0) }
    }

    private fun database(): JdbcTemplate {
        val source = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(source)
        return JdbcTemplate(source)
    }

    private fun controller(server: HttpServer, decisions: BankDecisionRepository, contextual: BankGraphEventRepository) =
        BankGraphEventController("http://127.0.0.1:${server.address.port}", decisions, contextual, mapper, true)

    private fun fakeModel(received: MutableList<String>, status: Int = 200, transform: (String) -> String = { it }): HttpServer {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/score/bank/contextual-v2") { exchange ->
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
        val graph = input["graphContext"]
        val hasGraphHistory = graph.properties().any { it.key.endsWith("Count") && it.value.longValue() > 0 }
        val status = if (senderCount == 0L && !hasGraphHistory) "cold_start" else "history_provided"
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
        ) + listOf(
            "senderDayCount", "senderDayAmount", "senderDayDistinctRecipients", "senderPairDayCount", "sender24hCount", "sender24hAmount",
            "senderInflow7dCount", "senderInflow30dCount", "senderInflow90dCount", "senderInflow90dAmount", "senderInflow90dDistinctSources",
            "senderDayInflowCount", "senderDayInflowAmount", "recipientInflow7dCount", "recipientInflow30dCount", "recipientInflow90dCount",
            "recipientInflow90dAmount", "recipientInflow90dDistinctSources", "recipientDayInflowCount", "recipientDayInflowAmount",
            "recipientOutflow90dCount", "recipientOutflow90dAmount", "recipientDayOutflowCount", "recipientDayOutflowAmount",
        ).map { "graph_${it}_log" } + listOf(
            "graph_amount_to_sender_day_mean", "graph_amount_to_sender_inflow_90d_mean", "graph_amount_to_sender_day_inflow",
            "graph_recipient_net_90d_signed_log", "graph_sender_net_day_signed_log", "graph_sender_pair_day_share",
        )
        return mapper.writeValueAsString(mapOf(
            "riskScore" to 0.4,
            "alert" to true,
            "threshold" to 0.3,
            "modelVersion" to "test-v1",
            "featureSchemaVersion" to "bank-context-v2-candidate",
            "historyCount" to historyCount,
            "historyWindowStart" to window,
            "contextStatus" to status,
            "evidenceType" to "observed transaction history; not a causal explanation",
            "observedContext" to observed,
            "graphContext" to input["graphContext"],
            "modelExplanation" to protocolExplanation(features),
            "supportAssessment" to BankSupportAssessment.expected(
                BankContextualTransaction("A", "B", "10", "20", "0", input["transaction"]["거래금액"].decimalValue(), 9, "2", date), status),
            "featureSnapshot" to features.associateWith { if (it == "sender_90d_count_log") kotlin.math.ln1p(historyCount.toDouble()) else 0.0 },
        )).toByteArray(StandardCharsets.UTF_8)
    }

    /** Synthetic protocol fixture, not an explanation produced by a real model. */
    private fun protocolExplanation(features: Collection<String>) = BankModelExplanation(
        BankModelExplanationProtocol.METHOD, "model_score_0_to_1", 0.2, 0.4, 0.0, 128, 100,
        BankModelExplanationProtocol.GROUP_LABELS.map { (group, label) ->
            BankScoreGroup(group, label, if (group == "amount") 0.2 else 0.0, features.filter { BankModelExplanationProtocol.featureGroup(it) == group })
        },
        BankModelExplanationProtocol.INTERPRETATION,
    )

    private fun input(source: String, destination: String, date: String, amount: Int) =
        """{"출금계좌일련번호":"$source","입금계좌일련번호":"$destination","출금금융회사일련번호":"10","입금금융회사일련번호":"20","자금구분":"0","거래금액":$amount,"거래시간대":9,"매체구분":"2","거래일자":"$date"}"""
}
