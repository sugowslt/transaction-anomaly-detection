package com.sugowslt.fraudlab

import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import java.math.BigDecimal
import java.nio.file.Files
import java.time.LocalDate
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger
import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import org.springframework.core.io.ClassPathResource
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.datasource.DriverManagerDataSource
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator
import tools.jackson.databind.json.JsonMapper
import java.nio.file.Path

class BankClosedWindowControllerTest {
    private val mapper = JsonMapper.builder().build()
    @TempDir lateinit var reports: Path
    private val request = """{"date":"20240502","timeBucket":9}"""
    private fun database(): JdbcTemplate {
        val source = DriverManagerDataSource("jdbc:h2:mem:${UUID.randomUUID()};DB_CLOSE_DELAY=-1", "sa", "")
        ResourceDatabasePopulator(ClassPathResource("schema.sql")).execute(source)
        return JdbcTemplate(source)
    }
    private fun seed(jdbc: JdbcTemplate) {
        for ((id, amount) in listOf("a" to 100, "b" to 200)) {
            jdbc.update("""INSERT INTO bank_decision(id,created_at,amount,time_bucket,fund_type,channel,risk_score,alert,threshold,model_version)
                VALUES(?,CURRENT_TIMESTAMP,?,9,'0','2',0.1,FALSE,0.3,'test')""", id, amount)
            jdbc.update("""INSERT INTO bank_graph_event(decision_id,transaction_date,transaction_hour,source_account,destination_account,
                source_institution,destination_institution,history_count,history_window_start,history_status,context_status,evidence_type,
                feature_schema_version,prior_summary,observed_context,graph_context,feature_snapshot)
                VALUES(?,'2024-05-02',9,'A','B','10','20',0,'2024-02-02','complete','cold_start','test','test','{}','{}','{}','{}')""", id)
        }
        Files.writeString(reports.resolve("bank_closed_window.json"), """{"model_version":"window-test","threshold_policy":{"threshold":0.5}}""")
    }
    private fun model(calls: AtomicInteger, mutate: (String) -> String = { it }): HttpServer {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/score/bank/window") { exchange ->
            calls.incrementAndGet()
            val input = mapper.readTree(exchange.requestBody.readAllBytes())
            assertEquals(2, input["events"].size())
            val observations = BankWindowObservations.calculate(listOf(100, 200).map {
                BankContextualTransaction("A", "B", "10", "20", "0", BigDecimal(it), 9, "2", LocalDate.of(2024, 5, 2))
            })
            val response = mutate(mapper.writeValueAsString(mapOf("modelVersion" to "window-test", "featureSchemaVersion" to "bank-closed-window-v1",
                "threshold" to 0.5, "observationMode" to "closed_three_hour_bucket_including_self_and_peers",
                "items" to observations.mapIndexed { i, observed -> mapOf("index" to i, "riskScore" to 0.7, "alert" to true, "windowSnapshot" to observed) }))).toByteArray()
            exchange.sendResponseHeaders(200, response.size.toLong())
            exchange.responseBody.use { it.write(response) }
        }
        server.start()
        return server
    }
    private fun controller(server: HttpServer, jdbc: JdbcTemplate, enabled: Boolean = true) =
        BankClosedWindowController("http://127.0.0.1:${server.address.port}", jdbc, mapper, enabled, reports.toString())

    private fun hybridReport() = Files.writeString(reports.resolve("bank_closed_window_hybrid.json"),
        """{"model_version":"hybrid-test","general_model_version":"window-test","specialist_model_version":"head-test",
           "general_threshold":0.5,"specialist_threshold":0.7,"decision_policy":"general_or_concurrent_specialist_v1"}""")
    private fun hybridResponse(text: String): String {
        val node = mapper.readTree(text) as tools.jackson.databind.node.ObjectNode
        node.put("modelVersion", "hybrid-test").put("generalModelVersion", "window-test").put("specialistModelVersion", "head-test")
            .put("specialistThreshold", 0.7).put("decisionPolicy", "general_or_concurrent_specialist_v1")
        for (item in node["items"].values()) (item as tools.jackson.databind.node.ObjectNode)
            .put("riskScore", 0.1).put("generalAlert", false).put("concurrentScore", 0.9).put("concurrentAlert", true)
        return mapper.writeValueAsString(node)
    }

    @Test
    fun `specialist alert below general threshold is saved and replayed with both scores`() {
        val jdbc = database(); seed(jdbc); hybridReport()
        val calls = AtomicInteger(); val server = model(calls, ::hybridResponse)
        try {
            val controller = controller(server, jdbc)
            val created = controller.review(request)
            assertEquals(201, created.statusCode.value())
            val result = mapper.readTree(created.body!!)
            assertEquals("hybrid-test", result["modelVersion"].stringValue())
            assertFalse(result["items"][0]["generalAlert"].booleanValue())
            assertTrue(result["items"][0]["concurrentAlert"].booleanValue())
            assertTrue(result["items"][0]["alert"].booleanValue())
            assertEquals(0.9, result["items"][0]["concurrentScore"].doubleValue())
            assertEquals(created.body, controller.review(request).body)
            assertEquals(1, calls.get())
        } finally { server.stop(0) }
    }

    @Test
    fun `forged specialist policy score threshold and component decisions never close a window`() {
        val changes = listOf<(String) -> String>(
            { it.replace("head-test", "forged-head") },
            { it.replace("general_or_concurrent_specialist_v1", "forged-policy") },
            { it.replace("\"specialistThreshold\":0.7", "\"specialistThreshold\":0.8") },
            { it.replace("\"concurrentScore\":0.9", "\"concurrentScore\":1.9") },
            { it.replace("\"concurrentAlert\":true", "\"concurrentAlert\":false") },
            { it.replace("\"generalAlert\":false", "\"generalAlert\":true") },
            { it.replace("\"alert\":true", "\"alert\":false") },
        )
        for (change in changes) {
            val jdbc = database(); seed(jdbc); hybridReport()
            val server = model(AtomicInteger()) { change(hybridResponse(it)) }
            try {
                assertEquals(502, controller(server, jdbc).review(request).statusCode.value())
                assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_closed_window_review", Int::class.java))
            } finally { server.stop(0) }
        }
    }

    @Test
    fun `review uses ledger rows saves immutable scores and replays without another inference`() {
        val jdbc = database(); seed(jdbc)
        val calls = AtomicInteger(); val server = model(calls)
        try {
            val controller = controller(server, jdbc)
            val created = controller.review(request)
            assertEquals(201, created.statusCode.value())
            assertEquals(2, mapper.readTree(created.body!!)["observedRows"].intValue())
            assertEquals(2, mapper.readTree(created.body!!)["alertCount"].intValue())
            assertEquals(created.body, controller.lookup("20240502", 9).body)
            assertEquals(created.body, controller.review(request).body)
            assertEquals(1, calls.get())
            assertTrue(BankGraphEventRepository(jdbc, BankDecisionRepository(jdbc)).windowClosed(LocalDate.of(2024, 5, 2), 9))
            assertFalse(created.body!!.contains("sourceAccount"))
        } finally { server.stop(0) }
    }

    @Test
    fun `forged observations version index and outcome never close the observation window`() {
        val changes = listOf<(String) -> String>(
            { it.replace("window-test", "other-model") },
            { it.replace("\"alert\":true", "\"alert\":false") },
            { it.replace("\"index\":0", "\"index\":4294967296") },
            { text -> val node = mapper.readTree(text); node["items"][0]["windowSnapshot"].let { (it as tools.jackson.databind.node.ObjectNode).put("window_pair_amount_log", 0.0) }; mapper.writeValueAsString(node) },
        )
        for (mutate in changes) {
            val jdbc = database(); seed(jdbc)
            val server = model(AtomicInteger(), mutate)
            try {
                assertEquals(502, controller(server, jdbc).review(request).statusCode.value())
                assertEquals(0, jdbc.queryForObject("SELECT COUNT(*) FROM bank_closed_window_review", Int::class.java))
            } finally { server.stop(0) }
        }
    }

    @Test
    fun `unended empty disabled and client supplied data do not call the model`() {
        val jdbc = database()
        val calls = AtomicInteger(); val server = model(calls)
        try {
            val controller = controller(server, jdbc)
            assertEquals(503, controller(server, jdbc, false).review(request).statusCode.value())
            assertEquals(400, controller.review("""{"date":"20240502","timeBucket":9,"events":[]}""").statusCode.value())
            assertEquals(400, controller.review("""{"date":"20240230","timeBucket":9}""").statusCode.value())
            assertEquals(400, controller.lookup("20240502", 7).statusCode.value())
            assertEquals(409, controller.review("""{"date":"20990101","timeBucket":9}""").statusCode.value())
            assertEquals(404, controller.review(request).statusCode.value())
            assertEquals(0, calls.get())
        } finally { server.stop(0) }
    }
}
