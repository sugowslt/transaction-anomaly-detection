package com.sugowslt.fraudlab

import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import java.nio.charset.StandardCharsets
import java.util.UUID
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.springframework.core.io.ClassPathResource
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.datasource.DriverManagerDataSource
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator
import tools.jackson.databind.json.JsonMapper

class BankEventControllerTest {
    @Test
    fun `stores scored transaction and returns alert history without identifiers`() {
        val dataSource = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(dataSource)
        val repository = BankDecisionRepository(JdbcTemplate(dataSource))
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
                JsonMapper.builder().build(),
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
}
