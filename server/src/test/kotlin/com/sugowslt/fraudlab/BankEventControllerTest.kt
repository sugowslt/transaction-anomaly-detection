package com.sugowslt.fraudlab

import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.time.OffsetDateTime
import java.util.UUID
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import org.springframework.core.io.ClassPathResource
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.datasource.DriverManagerDataSource
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator
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
            assertEquals("{\"거래금액\":5000000,\"거래시간대\":6,\"자금구분\":\"0\",\"매체구분\":\"2\"}", received)
            val response = """{"riskScore":0.62,"alert":true,"threshold":0.29,"modelVersion":"bank-a1b2c3d4e5f6"}"""
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
                "{\"거래금액\":5000000,\"거래시간대\":6,\"자금구분\":\"0\",\"매체구분\":\"2\"}",
            )

            assertEquals(201, response.statusCode.value())
            assertTrue(response.body!!.contains("bank-a1b2c3d4e5f6"))
            val alerts = controller.alerts(20)
            assertEquals(1, alerts.size)
            assertEquals("5000000.00", alerts.single().amount.toPlainString())
            assertEquals(6, alerts.single().timeBucket)
            assertTrue(alerts.single().alert)
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
