package com.sugowslt.fraudlab

import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Files
import java.nio.file.Path
import java.time.Duration
import org.springframework.beans.factory.annotation.Value
import org.springframework.http.MediaType
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

@RestController
@RequestMapping("/api")
class FraudController(
    @Value("\${fraud.model-url}") private val modelUrl: String,
    @Value("\${fraud.reports-dir}") private val reportsDir: String,
) {
    private val client = HttpClient.newBuilder()
        .connectTimeout(Duration.ofSeconds(2))
        .build()

    @GetMapping("/metrics", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun metrics(): ResponseEntity<String> = readReport("card_baseline.json")

    @GetMapping("/demo", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun demo(): ResponseEntity<String> = readReport("demo_transactions.json")

    @GetMapping("/bank/metrics", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun bankMetrics(): ResponseEntity<String> = readReport("bank_baseline.json")

    @GetMapping("/bank/demo", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun bankDemo(): ResponseEntity<String> = readReport("bank_demo_transactions.json")

    @GetMapping("/threshold-tradeoff", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun thresholdTradeoff(): ResponseEntity<String> = readReport("threshold_tradeoff.json")

    @GetMapping("/model-health", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun modelHealth(): ResponseEntity<String> {
        val request = HttpRequest.newBuilder(URI.create(modelUrl.trimEnd('/') + "/health"))
            .timeout(Duration.ofSeconds(4))
            .GET()
            .build()
        return forward(request)
    }

    @PostMapping("/score", consumes = [MediaType.APPLICATION_JSON_VALUE], produces = [MediaType.APPLICATION_JSON_VALUE])
    fun score(@RequestBody transaction: String): ResponseEntity<String> {
        return forwardScore("/score", transaction)
    }

    @PostMapping("/bank/score", consumes = [MediaType.APPLICATION_JSON_VALUE], produces = [MediaType.APPLICATION_JSON_VALUE])
    fun bankScore(@RequestBody transaction: String): ResponseEntity<String> =
        forwardScore("/score/bank", transaction)

    private fun forwardScore(path: String, transaction: String): ResponseEntity<String> {
        val request = HttpRequest.newBuilder(URI.create(modelUrl.trimEnd('/') + path))
            .timeout(Duration.ofSeconds(4))
            .header("Content-Type", MediaType.APPLICATION_JSON_VALUE)
            .POST(HttpRequest.BodyPublishers.ofString(transaction))
            .build()
        return forward(request)
    }

    private fun readReport(name: String): ResponseEntity<String> {
        val path = Path.of(reportsDir, name)
        if (!Files.isRegularFile(path)) {
            return ResponseEntity.status(404).contentType(MediaType.APPLICATION_JSON)
                .body("{\"error\":\"Report not found\"}")
        }
        return ResponseEntity.ok().contentType(MediaType.APPLICATION_JSON).body(Files.readString(path))
    }

    private fun forward(request: HttpRequest): ResponseEntity<String> = try {
        val response = client.send(request, HttpResponse.BodyHandlers.ofString())
        ResponseEntity.status(response.statusCode()).contentType(MediaType.APPLICATION_JSON).body(response.body())
    } catch (exception: InterruptedException) {
        Thread.currentThread().interrupt()
        unavailable()
    } catch (exception: java.io.IOException) {
        unavailable()
    }

    private fun unavailable(): ResponseEntity<String> =
        ResponseEntity.status(503).contentType(MediaType.APPLICATION_JSON)
            .body("{\"error\":\"Model service unavailable\"}")
}
