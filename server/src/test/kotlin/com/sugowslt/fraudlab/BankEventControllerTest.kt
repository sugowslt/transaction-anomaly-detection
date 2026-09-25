package com.sugowslt.fraudlab

import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.time.OffsetDateTime
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import org.springframework.core.io.ClassPathResource
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.datasource.DriverManagerDataSource
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator
import org.springframework.web.server.ResponseStatusException
import tools.jackson.databind.json.JsonMapper

class BankEventControllerTest {
    @TempDir
    lateinit var reports: java.nio.file.Path

    @Test
    fun `stores scored transaction and returns alert history without identifiers`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val repository = BankDecisionRepository(JdbcTemplate(dataSource))
        val mapper = JsonMapper.builder().build()
        writeMonitoringReference()
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/score/bank") { exchange ->
            val received = exchange.requestBody.readAllBytes().toString(StandardCharsets.UTF_8)
            assertEquals("{\"거래금액\":5000000.25,\"거래시간대\":6,\"자금구분\":\"0\",\"매체구분\":\"2\"}", received)
            val response = """{"riskScore":0.62,"alert":true,"threshold":0.62,"modelVersion":"bank-a1b2c3d4e5f6"}"""
                .toByteArray(StandardCharsets.UTF_8)
            exchange.responseHeaders.add("Content-Type", "application/json")
            exchange.sendResponseHeaders(200, response.size.toLong())
            exchange.responseBody.use { it.write(response) }
        }
        server.start()
        try {
            val controller = BankEventController(
                "http://127.0.0.1:${server.address.port}",
                repository,
                BankMonitoringService(repository, mapper, reports.toString()),
                mapper,
            )
            val response = controller.scoreAndStore(
                "{\"거래금액\":5000000.25,\"거래시간대\":6,\"자금구분\":\"0\",\"매체구분\":\"2\"}",
            )

            assertEquals(201, response.statusCode.value())
            assertTrue(response.body!!.contains("bank-a1b2c3d4e5f6"))
            val alerts = controller.alerts(20)
            assertEquals(1, alerts.size)
            assertEquals("5000000.25", alerts.single().amount.toPlainString())
            assertEquals(6, alerts.single().timeBucket)
            assertTrue(alerts.single().alert)
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `same idempotency key replays saved decision and conflicting body is rejected`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val jdbc = JdbcTemplate(dataSource)
        val repository = BankDecisionRepository(jdbc)
        val mapper = JsonMapper.builder().build()
        val modelCalls = AtomicInteger()
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/score/bank") { exchange ->
            modelCalls.incrementAndGet()
            exchange.requestBody.readAllBytes()
            val response = """{"riskScore":0.62,"alert":true,"threshold":0.29,"modelVersion":"bank-v1"}"""
                .toByteArray(StandardCharsets.UTF_8)
            exchange.sendResponseHeaders(200, response.size.toLong())
            exchange.responseBody.use { it.write(response) }
        }
        server.start()
        try {
            val controller = BankEventController(
                "http://127.0.0.1:${server.address.port}",
                repository,
                BankMonitoringService(repository, mapper, reports.toString()),
                mapper,
            )
            val body = """{"거래금액":5000000,"거래시간대":6,"자금구분":"0","매체구분":"2"}"""
            val first = controller.scoreAndStore(body, "transfer-1")
            val replay = controller.scoreAndStore(body, "transfer-1")
            val conflict = controller.scoreAndStore(body.replace("5000000", "6000000"), "transfer-1")
            val invalidKey = controller.scoreAndStore(body, "invalid key")

            assertEquals(201, first.statusCode.value())
            assertEquals(200, replay.statusCode.value())
            assertEquals(first.body, replay.body)
            assertEquals(409, conflict.statusCode.value())
            assertEquals(400, invalidKey.statusCode.value())
            assertEquals(1, modelCalls.get())
            assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `schema adds idempotency columns to existing decision table`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val jdbc = JdbcTemplate(dataSource)
        val repository = BankDecisionRepository(jdbc)
        val legacyDecision = repository.save(java.math.BigDecimal("10000"), 9, "0", "2", 0.01, false, 0.29, "bank-v1")
        jdbc.execute("ALTER TABLE bank_decision DROP COLUMN request_hash")
        jdbc.execute("ALTER TABLE bank_decision DROP COLUMN request_key")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)

        repository.save(java.math.BigDecimal("30000"), 9, "0", "2", 0.01, false, 0.29, "bank-v1", requestKey = "transfer-1", requestHash = "hash")
        assertEquals("hash", repository.findByRequestKey("transfer-1")?.requestHash)
        assertEquals(legacyDecision.id, jdbc.queryForObject("SELECT id FROM bank_decision WHERE request_key IS NULL", String::class.java))
        assertEquals(2, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
    }

    @Test
    fun `invalid bank input is rejected before calling model`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val jdbc = JdbcTemplate(dataSource)
        val mapper = JsonMapper.builder().build()
        val repository = BankDecisionRepository(jdbc)
        val controller = BankEventController(
            "http://127.0.0.1:1",
            repository,
            BankMonitoringService(repository, mapper, reports.toString()),
            mapper,
        )
        val valid = """{"거래금액":5000000,"거래시간대":6,"자금구분":"0","매체구분":"2"}"""
        val invalid = listOf(
            "{",
            "[]",
            valid.replace(",\"매체구분\":\"2\"", ""),
            valid.replace("\"매체구분\":\"2\"", "\"매체구분\":\"2\",\"계좌번호\":\"123\""),
            valid.replace("5000000", "-1"),
            valid.replace("5000000", "100000000000000000"),
            valid.replace("5000000", "99999999999999999.99"),
            valid.replace("5000000", "100.005"),
            valid.replace("5000000", "100.000000000000001"),
            valid.replace("\"거래시간대\":6", "\"거래시간대\":7"),
            valid.replace("\"자금구분\":\"0\"", "\"자금구분\":\"11\""),
            valid.replace("\"매체구분\":\"2\"", "\"매체구분\":2"),
        )

        invalid.forEach { assertEquals(400, controller.scoreAndStore(it).statusCode.value(), it) }
        assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
    }

    @Test
    fun `invalid model response is rejected without saving a decision`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val jdbc = JdbcTemplate(dataSource)
        val repository = BankDecisionRepository(jdbc)
        val mapper = JsonMapper.builder().build()
        val modelResponse = AtomicReference("")
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/score/bank") { exchange ->
            exchange.requestBody.readAllBytes()
            val response = modelResponse.get().toByteArray(StandardCharsets.UTF_8)
            exchange.sendResponseHeaders(200, response.size.toLong())
            exchange.responseBody.use { it.write(response) }
        }
        server.start()
        try {
            val controller = BankEventController(
                "http://127.0.0.1:${server.address.port}",
                repository,
                BankMonitoringService(repository, mapper, reports.toString()),
                mapper,
            )
            val body = """{"거래금액":5000000,"거래시간대":6,"자금구분":"0","매체구분":"2"}"""
            listOf(
                "not-json",
                """{"riskScore":0.5,"alert":true,"modelVersion":"bank-v1"}""",
                """{"riskScore":1.5,"alert":true,"threshold":0.3,"modelVersion":"bank-v1"}""",
                """{"riskScore":0.5,"alert":"true","threshold":0.3,"modelVersion":"bank-v1"}""",
                """{"riskScore":0.1,"alert":true,"threshold":0.3,"modelVersion":"bank-v1"}""",
                """{"riskScore":0.5,"alert":true,"threshold":0.3,"modelVersion":""}""",
            ).forEach { response ->
                modelResponse.set(response)
                assertEquals(502, controller.scoreAndStore(body).statusCode.value(), response)
            }
            assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `model rejection of valid input returns gateway error without exposing its response`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val jdbc = JdbcTemplate(dataSource)
        val repository = BankDecisionRepository(jdbc)
        val mapper = JsonMapper.builder().build()
        val modelStatus = AtomicInteger(400)
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/score/bank") { exchange ->
            exchange.requestBody.readAllBytes()
            val response = """{"error":"internal model detail"}""".toByteArray(StandardCharsets.UTF_8)
            exchange.sendResponseHeaders(modelStatus.get(), response.size.toLong())
            exchange.responseBody.use { it.write(response) }
        }
        server.start()
        try {
            val controller = BankEventController(
                "http://127.0.0.1:${server.address.port}",
                repository,
                BankMonitoringService(repository, mapper, reports.toString()),
                mapper,
            )
            val body = """{"거래금액":5000000,"거래시간대":6,"자금구분":"0","매체구분":"2"}"""
            listOf(400, 500).forEach { status ->
                modelStatus.set(status)
                val response = controller.scoreAndStore(body)
                assertEquals(502, response.statusCode.value())
                assertEquals("""{"error":"Model service failed"}""", response.body)
            }
            assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_decision", Int::class.java))
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `history contains alerts only`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val repository = BankDecisionRepository(JdbcTemplate(dataSource))
        repository.save(java.math.BigDecimal("30000"), 9, "0", "2", 0.01, false, 0.29, "bank-version")
        repository.save(java.math.BigDecimal("5000000"), 6, "0", "2", 0.62, true, 0.29, "bank-version")

        val alerts = repository.findAlerts(20)

        assertEquals(1, alerts.size)
        assertTrue(alerts.single().alert)
    }

    @Test
    fun `decision history filters and paginates without repeating rows after a new insert`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val repository = BankDecisionRepository(JdbcTemplate(dataSource))
        val mapper = JsonMapper.builder().build()
        val controller = BankEventController(
            "http://127.0.0.1:1",
            repository,
            BankMonitoringService(repository, mapper, reports.toString()),
            mapper,
        )
        val createdAt = OffsetDateTime.parse("2026-09-24T12:00:00Z")
        repeat(5) { index ->
            repository.save(java.math.BigDecimal("30000"), 9, "0", "2", 0.2, index % 2 == 0, 0.29, "bank-v1", createdAt)
        }
        val expected = repository.findDecisions(20, null, null).map { it.id }
        val first = controller.decisions(2)
        repository.save(java.math.BigDecimal("5000000"), 6, "0", "2", 0.8, true, 0.29, "bank-v1", createdAt.plusSeconds(1))
        val second = controller.decisions(2, beforeCreatedAt = first.nextCursor!!.createdAt.toString(), beforeId = first.nextCursor.id)
        val third = controller.decisions(2, beforeCreatedAt = second.nextCursor!!.createdAt.toString(), beforeId = second.nextCursor.id)

        assertEquals(expected, (first.items + second.items + third.items).map { it.id })
        assertEquals(null, third.nextCursor)
        assertEquals(4, controller.decisions(20, alert = true).items.size)
        assertEquals(2, controller.decisions(20, alert = false).items.size)
        val filteredFirst = controller.decisions(1, alert = true)
        val filteredSecond = controller.decisions(
            1, alert = true,
            beforeCreatedAt = filteredFirst.nextCursor!!.createdAt.toString(),
            beforeId = filteredFirst.nextCursor.id,
        )
        assertTrue(filteredSecond.items.single().alert)
        assertFalse(filteredSecond.items.single().id == filteredFirst.items.single().id)
        assertEquals(400, assertThrows(ResponseStatusException::class.java) { controller.decisions(0) }.statusCode.value())
        assertEquals(400, assertThrows(ResponseStatusException::class.java) {
            controller.decisions(20, beforeCreatedAt = createdAt.toString())
        }.statusCode.value())
        assertEquals(400, assertThrows(ResponseStatusException::class.java) {
            controller.decisions(20, beforeCreatedAt = "bad-date", beforeId = expected.first())
        }.statusCode.value())
        assertEquals(400, assertThrows(ResponseStatusException::class.java) {
            controller.decisions(20, beforeCreatedAt = createdAt.toString(), beforeId = "bad-id")
        }.statusCode.value())
    }

    @Test
    fun `monitoring compares recent decisions and groups alert rates by model version`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val repository = BankDecisionRepository(JdbcTemplate(dataSource))
        repeat(20) { repository.save(java.math.BigDecimal("10000"), 0, "0", "2", 0.62, true, 0.29, "bank-v2") }
        repeat(10) { repository.save(java.math.BigDecimal("10000"), 0, "0", "2", 0.01, false, 0.29, "bank-v1") }
        writeMonitoringReference()

        val snapshot = BankMonitoringService(
            repository,
            JsonMapper.builder().build(),
            reports.toString(),
        ).snapshot()

        assertEquals(30, snapshot.sampleSize)
        assertTrue(snapshot.ready)
        assertEquals("ref-current", snapshot.referenceVersion)
        assertEquals(2, snapshot.referenceVersions.size)
        assertEquals("기존 기준", snapshot.referenceVersions.first().changeReason)
        assertEquals(100, snapshot.referenceVersions.first().reference["rows"].intValue())
        assertTrue(snapshot.referenceVersions.last().current)
        assertEquals("DRIFT", snapshot.drift.first { it.feature == "거래금액" }.status)
        val channel = snapshot.drift.first { it.feature == "매체구분" }
        assertEquals(listOf("2", "7"), channel.distribution.map { it.label })
        assertEquals(0.8, channel.distribution.first().referenceShare, 1e-9)
        assertEquals(1.0, channel.distribution.first().observedShare, 1e-9)
        assertEquals(0.2, channel.score!!, 1e-9)
        assertEquals(2, snapshot.modelVersions.size)
        assertEquals(1.0, snapshot.modelVersions.first { it.modelVersion == "bank-v2" }.alertRate)
        assertEquals(0.0, snapshot.modelVersions.first { it.modelVersion == "bank-v1" }.alertRate)
    }

    @Test
    fun `monitoring waits for minimum sample size`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val repository = BankDecisionRepository(JdbcTemplate(dataSource))
        repository.save(java.math.BigDecimal("10000"), 0, "0", "2", 0.62, true, 0.29, "bank-v1")
        writeMonitoringReference()

        val snapshot = BankMonitoringService(
            repository,
            JsonMapper.builder().build(),
            reports.toString(),
        ).snapshot()

        assertFalse(snapshot.ready)
        assertTrue(snapshot.drift.all { it.status == "INSUFFICIENT_DATA" })
    }

    @Test
    fun `monitoring has no largest shift before any decision is recorded`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val repository = BankDecisionRepository(JdbcTemplate(dataSource))
        writeMonitoringReference()

        val snapshot = BankMonitoringService(repository, JsonMapper.builder().build(), reports.toString()).snapshot()

        assertEquals(0, snapshot.sampleSize)
        assertTrue(snapshot.drift.all { it.score == null && it.largestShift == null })
        assertTrue(snapshot.drift.all { it.status == "INSUFFICIENT_DATA" })
    }

    @Test
    fun `monitoring groups recent decisions by active UTC date`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val repository = BankDecisionRepository(JdbcTemplate(dataSource))
        val firstDate = OffsetDateTime.parse("2026-09-21T12:00:00Z")
        val secondDate = OffsetDateTime.parse("2026-09-23T12:00:00Z")
        repeat(10) { repository.save(java.math.BigDecimal("10000"), 0, "0", "2", 0.2, false, 0.29, "bank-v1", firstDate) }
        repeat(5) { repository.save(java.math.BigDecimal("10000"), 0, "0", "2", 0.8, true, 0.29, "bank-v1", firstDate) }
        repeat(15) { repository.save(java.math.BigDecimal("10000"), 0, "0", "2", 0.4, false, 0.29, "bank-v1", secondDate) }
        writeMonitoringReference()

        val timeline = BankMonitoringService(
            repository,
            JsonMapper.builder().build(),
            reports.toString(),
        ).snapshot().timeline

        assertEquals(2, timeline.size)
        assertEquals("2026-09-21", timeline.first().date.toString())
        assertEquals(15, timeline.first().decisions)
        assertEquals(5, timeline.first().alerts)
        assertEquals(1.0 / 3.0, timeline.first().alertRate, 1e-9)
        assertEquals(0.4, timeline.first().averageRiskScore, 1e-9)
        assertEquals("2026-09-23", timeline.last().date.toString())
        assertEquals(0, timeline.last().alerts)
    }

    private fun writeMonitoringReference() {
        Files.writeString(
            reports.resolve("bank_baseline.json"),
            """{
              "monitoring_reference": {
                "source": "test reference",
                "rows": 100,
                "version": "ref-current",
                "change_reason": "학습 표본 갱신",
                "amount_bands": {
                  "upper_bounds": [100000, 1000000, 5000000],
                  "proportions": [0.5, 0.4, 0.09, 0.01]
                },
                "categories": {
                  "거래시간대": {"0": 0.25, "3": 0.25, "6": 0.25, "9": 0.25},
                  "자금구분": {"0": 0.7, "1": 0.3},
                  "매체구분": {"0": 0.8, "1": 0.2}
                },
                "category_labels": {
                  "자금구분": {"0": "0", "1": "1"},
                  "매체구분": {"0": "2", "1": "7"}
                }
              },
              "monitoring_reference_history": [{
                "source": "old test reference",
                "rows": 100,
                "version": "ref-previous",
                "change_reason": "기존 기준",
                "amount_bands": {"upper_bounds": [100000, 1000000, 5000000], "proportions": [0.5, 0.4, 0.09, 0.01]},
                "categories": {"거래시간대": {"0": 1.0}},
                "category_labels": {"자금구분": {"0": "0"}}
              }]
            }""".trimIndent(),
        )
    }
}
