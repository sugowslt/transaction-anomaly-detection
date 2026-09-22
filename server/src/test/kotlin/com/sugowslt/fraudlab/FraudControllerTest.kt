package com.sugowslt.fraudlab

import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir

class FraudControllerTest {
    @TempDir
    lateinit var reports: Path

    @Test
    fun `reads generated report and returns 404 when missing`() {
        Files.writeString(reports.resolve("card_baseline.json"), "{\"validation\":{\"rows\":10}}")
        val controller = FraudController("http://127.0.0.1:1", reports.toString())

        assertEquals(200, controller.metrics().statusCode.value())
        assertTrue(controller.metrics().body!!.contains("\"rows\":10"))
        assertEquals(404, controller.demo().statusCode.value())
    }

    @Test
    fun `forwards scoring request and model response`() {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        var receivedBody = ""
        server.createContext("/score") { exchange ->
            receivedBody = exchange.requestBody.readAllBytes().toString(StandardCharsets.UTF_8)
            val response = "{\"riskScore\":0.82,\"alert\":true}".toByteArray(StandardCharsets.UTF_8)
            exchange.responseHeaders.add("Content-Type", "application/json")
            exchange.sendResponseHeaders(200, response.size.toLong())
            exchange.responseBody.use { it.write(response) }
        }
        server.start()
        try {
            val controller = FraudController("http://127.0.0.1:" + server.address.port, reports.toString())
            val response = controller.score("{\"amount\":100}")

            assertEquals(200, response.statusCode.value())
            assertEquals("{\"amount\":100}", receivedBody)
            assertEquals("{\"riskScore\":0.82,\"alert\":true}", response.body)
        } finally {
            server.stop(0)
        }
    }
}
